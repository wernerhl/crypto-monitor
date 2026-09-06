"""OKX v5 adapter (`www.okx.com`). Verified 2026-09-06 — docs/data_sources.md §1–2.
Daily candles use `bar=1Dutc` (verified) so days align with Binance/Bybit UTC days."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.schema.tables import DailyPriceRow, PerpSnapshotRow, VenueListingRow, rows_to_df

VENUE = "okx"


def _ms(x: int | str) -> datetime:
    return datetime.fromtimestamp(int(x) / 1000, tz=UTC)


def fetch_listings(ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get("/api/v5/public/instruments", params={"instType": t})
            for t in ("SPOT", "SWAP", "FUTURES")
        ]

    return run_dataset("okx", "listings", "daily", go, ts=ts, force=force)


def parse_listings(env: Envelope) -> pl.DataFrame:
    ts = datetime.fromisoformat(env.fetched_at)
    rows: list[VenueListingRow] = []
    for rec in env.records:
        for s in rec.body["data"]:
            it = s["instType"]
            if it == "SPOT":
                rows.append(
                    VenueListingRow(
                        ts=ts,
                        venue=VENUE,
                        market="spot",
                        symbol=s["instId"],
                        base=s["baseCcy"],
                        quote=s["quoteCcy"],
                        status=s["state"],
                        source="okx",
                        fetched_at=ts,
                        git_sha=env.git_sha,
                    )
                )
            else:
                fam = s.get("instFamily") or "-".join(s["instId"].split("-")[:2])  # e.g. BTC-USDT
                parts = fam.split("-")
                if len(parts) < 2 or s.get("ctType") == "inverse":
                    continue  # unparseable family, or inverse contract (linear only for aggregation)
                base, quote = parts[0], parts[1]
                if not s.get("ctVal"):
                    continue  # pre-open instrument without contract value
                rows.append(
                    VenueListingRow(
                        ts=ts,
                        venue=VENUE,
                        market="perp" if it == "SWAP" else "future",
                        symbol=s["instId"],
                        base=base,
                        quote=quote,
                        multiplier=float(s["ctVal"]),
                        status=s["state"],
                        delivery=_ms(s["expTime"]) if s.get("expTime") else None,
                        source="okx",
                        fetched_at=ts,
                        git_sha=env.git_sha,
                    )
                )
    df = rows_to_df(VenueListingRow, rows)
    if df.filter(pl.col("market") == "perp").height < 100:
        raise SanityError("okx listings: too few swaps")
    return df


def fetch_perps(ts: datetime | None = None, force: bool = False, freq: str = "daily") -> Path:
    def go(c) -> list[Record]:
        return [
            c.get("/api/v5/public/open-interest", params={"instType": "SWAP"}),
            c.get("/api/v5/market/tickers", params={"instType": "SWAP"}),
            c.get("/api/v5/public/mark-price", params={"instType": "SWAP"}),
        ]

    return run_dataset("okx", "perps", freq, go, ts=ts, force=force)


def parse_perps(env: Envelope, listings: pl.DataFrame | None = None) -> pl.DataFrame:
    """OI in USD comes from `oiUsd`; base units from `oiCcy`. Funding is fetched per
    instrument in the hourly job (phase 3); here it is null."""
    fetched = datetime.fromisoformat(env.fetched_at)
    oi = {r["instId"]: r for r in env.records[0].body["data"]}
    tick = {r["instId"]: r for r in env.records[1].body["data"]}
    mark = {r["instId"]: r for r in env.records[2].body["data"]}
    rows: list[PerpSnapshotRow] = []
    for inst, o in oi.items():
        if not inst.endswith("-USDT-SWAP"):
            continue
        base = inst.split("-")[0]
        t, m = tick.get(inst, {}), mark.get(inst, {})
        last = float(t["last"]) if t.get("last") else None
        vol_usd = float(t["volCcy24h"]) * last if t.get("volCcy24h") and last else None
        rows.append(
            PerpSnapshotRow(
                ts=_ms(o["ts"]),
                venue=VENUE,
                symbol=inst,
                base=base,
                multiplier=1.0,
                mark_price=float(m["markPx"]) if m.get("markPx") else last,
                index_price=None,
                funding_rate=None,
                funding_interval_h=None,
                next_funding_time=None,
                oi_base=float(o["oiCcy"]),
                oi_usd=float(o["oiUsd"]),
                volume_24h_usd=vol_usd,
                source="okx",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    df = rows_to_df(PerpSnapshotRow, rows)
    if df.height < 100:
        raise SanityError("okx perps: too few rows")
    return df


def fetch_klines_1d(
    symbols: list[str], limit: int = 120, ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(
                "/api/v5/market/candles",
                params={"instId": s, "bar": "1Dutc", "limit": min(limit, 300)},
            )
            for s in symbols
        ]

    return run_dataset(
        "okx",
        "klines_1d",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "limit": limit},
    )


def parse_klines_1d(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows: list[DailyPriceRow] = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        base = sym.split("-")[0]
        for k in rec.body["data"]:
            if k[8] != "1":  # confirm flag: only closed candles
                continue
            rows.append(
                DailyPriceRow(
                    date=_ms(k[0]).date(),
                    venue=VENUE,
                    symbol=sym,
                    base=base,
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume_base=float(k[5]),
                    volume_quote=float(k[7]),
                    source="okx",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(DailyPriceRow, rows)
