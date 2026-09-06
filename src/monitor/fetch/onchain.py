"""On-chain adapters for BTC and ETH only (notes §5). Free sources verified 2026-09-06:
CoinMetrics community (`CapMVRVCur`, `SplyExNtv`, `FlowInExUSD`, `FlowOutExUSD`, `HashRate`,
`AdrActCnt`, `FeeTotNtv`, `SplyCur`; realised cap and SOPR are NOT in the community tier),
blockchain.info charts, mempool.space, ultrasound.money (unofficial) for ETH staking.
Optional keyed: beaconcha.in (`BEACONCHAIN_API_KEY`), Glassnode (`GLASSNODE_API_KEY`)."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset

CM_METRICS = [
    "CapMVRVCur",
    "CapMrktCurUSD",
    "SplyCur",
    "SplyExNtv",
    "SplyExUSD",
    "FlowInExUSD",
    "FlowOutExUSD",
    "HashRate",
    "AdrActCnt",
    "FeeTotNtv",
]


def fetch_coinmetrics(
    start: str = "2010-01-01", ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        recs = []
        for asset in ("btc", "eth"):
            r = c.get(
                "/timeseries/asset-metrics",
                params={
                    "assets": asset,
                    "metrics": ",".join(CM_METRICS),
                    "frequency": "1d",
                    "page_size": 10000,
                    "start_time": start,
                },
            )
            recs.append(r)
            nxt = r.body.get("next_page_url")
            n = 0
            while nxt and n < 10:  # community pages are 10 000 rows; history is < 20 000 rows
                r = c.get(nxt.replace(c.cfg["base"], ""))
                recs.append(r)
                nxt = r.body.get("next_page_url")
                n += 1
        return recs

    return run_dataset("coinmetrics", "asset_metrics", "daily", go, ts=ts, force=force)


def parse_coinmetrics(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        for d in rec.body.get("data", []):
            row = {
                "date": datetime.fromisoformat(d["time"].replace("Z", "+00:00")).date(),
                "asset": d["asset"].upper(),
            }
            for m in CM_METRICS:
                v = d.get(m)
                row[m] = float(v) if v not in (None, "") else None
                row[f"{m}_status"] = d.get(f"{m}-status")
            rows.append(row)
    schema = {"date": pl.Date, "asset": pl.Utf8}
    for m in CM_METRICS:
        schema[m] = pl.Float64
        schema[f"{m}_status"] = pl.Utf8
    df = pl.DataFrame(rows, schema=schema).with_columns(
        pl.lit("coinmetrics").alias("source"),
        pl.lit(fetched).alias("fetched_at"),
        pl.lit(env.git_sha).alias("git_sha"),
    )
    if df.filter(pl.col("asset") == "BTC").height < 5:
        raise SanityError("coinmetrics: too few BTC rows")
    return df


def fetch_btc_chain(ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(f"/{chart}", params={"timespan": "1year", "format": "json"})
            for chart in ("hash-rate", "n-unique-addresses", "transaction-fees-usd", "market-price")
        ]

    return run_dataset(
        "blockchain_info",
        "charts",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={
            "charts": ["hash-rate", "n-unique-addresses", "transaction-fees-usd", "market-price"]
        },
    )


def parse_btc_chain(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, chart in zip(env.records, env.meta["charts"], strict=True):
        for v in rec.body.get("values", []):
            rows.append(
                {
                    "date": datetime.fromtimestamp(v["x"], tz=UTC).date(),
                    "asset": "BTC",
                    "metric": chart,
                    "value": float(v["y"]),
                    "unit": rec.body.get("unit"),
                    "source": "blockchain_info",
                    "fetched_at": fetched,
                    "git_sha": env.git_sha,
                }
            )
    return pl.DataFrame(rows)


def fetch_mempool(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "mempool_space",
        "hashrate",
        "daily",
        lambda c: [c.get("/mining/hashrate/3d")],
        ts=ts,
        force=force,
    )


def fetch_eth_staking(ts: datetime | None = None, force: bool = False) -> Path:
    """ultrasound.money supply parts (unofficial, free) and beaconcha.in when a key exists."""

    def go(c) -> list[Record]:
        return [c.get("/fees/supply-parts")]

    p = run_dataset("ultrasound", "supply_parts", "daily", go, ts=ts, force=force)
    key = os.environ.get("BEACONCHAIN_API_KEY")
    if key:
        run_dataset(
            "beaconchain",
            "epoch",
            "daily",
            lambda c: [c.get("/epoch/latest", params={"apikey": key})],
            ts=ts,
            force=force,
        )
    return p


def parse_eth_staking(env: Envelope, eth_supply: float | None) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    b = env.records[0].body
    staked = float(b["beaconBalancesSum"]) / 1e9
    ratio = staked / eth_supply if eth_supply else None
    return pl.DataFrame(
        [
            {
                "date": fetched.date(),
                "asset": "ETH",
                "staked_eth": staked,
                "deposits_eth": float(b["beaconDepositsSum"]) / 1e9,
                "supply_eth": eth_supply,
                "staking_ratio": ratio,
                "slot": b.get("slot"),
                "source": "ultrasound (unofficial)",
                "fetched_at": fetched,
                "git_sha": env.git_sha,
            }
        ]
    )
