"""Coinbase Exchange adapter (`api.exchange.coinbase.com`). Verified 2026-09-06."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.schema.tables import DailyPriceRow, VenueListingRow, rows_to_df

VENUE = "coinbase"


def fetch_listings(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "coinbase", "listings", "daily", lambda c: [c.get("/products")], ts=ts, force=force
    )


def parse_listings(env: Envelope) -> pl.DataFrame:
    ts = datetime.fromisoformat(env.fetched_at)
    rows = [
        VenueListingRow(
            ts=ts,
            venue=VENUE,
            market="spot",
            symbol=p["id"],
            base=p["base_currency"],
            quote=p["quote_currency"],
            status="TRADING"
            if p["status"] == "online" and not p["trading_disabled"]
            else p["status"],
            source="coinbase",
            fetched_at=ts,
            git_sha=env.git_sha,
        )
        for p in env.records[0].body
    ]
    df = rows_to_df(VenueListingRow, rows)
    if df.height < 200:
        raise SanityError("coinbase products: too few")
    return df


def fetch_klines_1d(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get(f"/products/{s}/candles", params={"granularity": 86400}) for s in symbols]

    return run_dataset(
        "coinbase", "klines_1d", "daily", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_klines_1d(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows: list[DailyPriceRow] = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        base = sym.split("-")[0]
        for k in rec.body:  # [time, low, high, open, close, volume]
            d = datetime.fromtimestamp(k[0], tz=UTC).date()
            if d >= fetched.date():
                continue
            rows.append(
                DailyPriceRow(
                    date=d,
                    venue=VENUE,
                    symbol=sym,
                    base=base,
                    open=float(k[3]),
                    high=float(k[2]),
                    low=float(k[1]),
                    close=float(k[4]),
                    volume_base=float(k[5]),
                    volume_quote=None,
                    source="coinbase",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(DailyPriceRow, rows)
