"""CoinPaprika adapter — fallback aggregator (20 000 calls / month, no key).
Verified 2026-09-06. `tickers` has no circulating supply, so float is derived as
market_cap / price and flagged `supply_source = derived`."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.schema.tables import CoinMetaRow, MarketRow, rows_to_df

SOURCE = "coinpaprika"


def fetch_markets(limit: int = 500, ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        SOURCE,
        "markets",
        "daily",
        lambda c: [c.get("/tickers", params={"limit": limit})],
        ts=ts,
        force=force,
    )


def parse_markets(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows: list[MarketRow] = []
    for t in env.records[0].body:
        q = t["quotes"]["USD"]
        price, mcap = q.get("price"), q.get("market_cap")
        circ = (mcap / price) if price and mcap else None
        rows.append(
            MarketRow(
                as_of=fetched.date(),
                id=t["id"],
                symbol=t["symbol"].upper(),
                name=t["name"],
                rank=t.get("rank"),
                price_usd=price,
                market_cap_usd=mcap,
                fdv_usd=(price * t["total_supply"]) if price and t.get("total_supply") else None,
                volume_24h_usd=q.get("volume_24h"),
                circulating_supply=circ,
                total_supply=t.get("total_supply"),
                max_supply=t.get("max_supply"),
                supply_source="derived",
                source=SOURCE,
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    df = rows_to_df(MarketRow, rows)
    if df.height < 200:
        raise SanityError("coinpaprika tickers: too few rows")
    return df


def fetch_coin_meta(ids: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get(f"/coins/{i}") for i in ids]

    return run_dataset(SOURCE, "coin_meta", "daily", go, ts=ts, force=force, meta={"ids": ids})


def parse_coin_meta(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = [
        CoinMetaRow(
            id=b["id"],
            symbol=b["symbol"].upper(),
            name=b["name"],
            categories=[t["name"] for t in b.get("tags", [])],
            asset_platform_id=None if b.get("type") == "coin" else "token",
            source=SOURCE,
            fetched_at=fetched,
            git_sha=env.git_sha,
        )
        for rec in env.records
        for b in [rec.body]
        if isinstance(b, dict) and "id" in b
    ]
    return rows_to_df(CoinMetaRow, rows)
