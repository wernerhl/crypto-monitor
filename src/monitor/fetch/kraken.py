"""Kraken adapter (`api.kraken.com`). Verified 2026-09-06. Legacy asset codes normalised."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.fetch.symbols import kraken_base
from monitor.schema.tables import DailyPriceRow, VenueListingRow, rows_to_df

VENUE = "kraken"


def fetch_listings(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "kraken", "listings", "daily", lambda c: [c.get("/0/public/AssetPairs")], ts=ts, force=force
    )


def parse_listings(env: Envelope) -> pl.DataFrame:
    ts = datetime.fromisoformat(env.fetched_at)
    rows: list[VenueListingRow] = []
    for key, p in env.records[0].body["result"].items():
        if ".d" in key or "wsname" not in p:
            continue
        b, q = p["wsname"].split("/")
        rows.append(
            VenueListingRow(
                ts=ts,
                venue=VENUE,
                market="spot",
                symbol=p["altname"],
                base=kraken_base(b),
                quote=kraken_base(q),
                status="TRADING" if p.get("status") == "online" else str(p.get("status")),
                source="kraken",
                fetched_at=ts,
                git_sha=env.git_sha,
            )
        )
    df = rows_to_df(VenueListingRow, rows)
    if df.height < 300:
        raise SanityError("kraken pairs: too few")
    return df


def fetch_klines_1d(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get("/0/public/OHLC", params={"pair": s, "interval": 1440}) for s in symbols]

    return run_dataset(
        "kraken", "klines_1d", "daily", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_klines_1d(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows: list[DailyPriceRow] = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        res = rec.body["result"]
        key = next(k for k in res if k != "last")
        base = kraken_base(sym.removesuffix("USD"))
        for k in res[key]:  # [time, o, h, l, c, vwap, vol, count]
            d = datetime.fromtimestamp(k[0], tz=UTC).date()
            if d >= fetched.date():
                continue
            rows.append(
                DailyPriceRow(
                    date=d,
                    venue=VENUE,
                    symbol=sym,
                    base=base,
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume_base=float(k[6]),
                    volume_quote=float(k[6]) * float(k[5]),
                    source="kraken",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(DailyPriceRow, rows)
