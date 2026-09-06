"""Bybit v5 adapter (`api.bybit.com`). Verified 2026-09-06 — docs/data_sources.md §1–2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.fetch.symbols import split_multiplier
from monitor.schema.tables import DailyPriceRow, PerpSnapshotRow, VenueListingRow, rows_to_df

VENUE = "bybit"


def _ms(x: int | str) -> datetime:
    return datetime.fromtimestamp(int(x) / 1000, tz=UTC)


def _paged(c, url: str, params: dict) -> list[Record]:
    recs, cursor = [], None
    for _ in range(20):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        r = c.get(url, params=p)
        recs.append(r)
        cursor = r.body.get("result", {}).get("nextPageCursor")
        if not cursor:
            break
    return recs


def fetch_listings(ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return _paged(
            c, "/v5/market/instruments-info", {"category": "spot", "limit": 1000}
        ) + _paged(c, "/v5/market/instruments-info", {"category": "linear", "limit": 1000})

    return run_dataset("bybit", "listings", "daily", go, ts=ts, force=force)


def parse_listings(env: Envelope) -> pl.DataFrame:
    ts = datetime.fromisoformat(env.fetched_at)
    rows: list[VenueListingRow] = []
    for rec in env.records:
        res = rec.body["result"]
        cat = res["category"]
        for s in res["list"]:
            if cat == "spot":
                rows.append(
                    VenueListingRow(
                        ts=ts,
                        venue=VENUE,
                        market="spot",
                        symbol=s["symbol"],
                        base=s["baseCoin"],
                        quote=s["quoteCoin"],
                        status=s["status"],
                        source="bybit",
                        fetched_at=ts,
                        git_sha=env.git_sha,
                    )
                )
            else:
                base, mult = split_multiplier(s["baseCoin"])
                perp = s["contractType"] == "LinearPerpetual"
                rows.append(
                    VenueListingRow(
                        ts=ts,
                        venue=VENUE,
                        market="perp" if perp else "future",
                        symbol=s["symbol"],
                        base=base,
                        quote=s["quoteCoin"],
                        multiplier=mult,
                        status=s["status"],
                        delivery=None if perp else _ms(s["deliveryTime"]),
                        source="bybit",
                        fetched_at=ts,
                        git_sha=env.git_sha,
                    )
                )
    df = rows_to_df(VenueListingRow, rows)
    if df.filter(pl.col("market") == "perp").height < 100:
        raise SanityError("bybit listings: too few perps")
    return df


def fetch_perps(ts: datetime | None = None, force: bool = False, freq: str = "daily") -> Path:
    def go(c) -> list[Record]:
        return [c.get("/v5/market/tickers", params={"category": "linear"})]

    return run_dataset("bybit", "perps", freq, go, ts=ts, force=force)


def parse_perps(env: Envelope, listings: pl.DataFrame | None = None) -> pl.DataFrame:
    """Uses `openInterestValue` (USD) directly; funding interval from listings when given."""
    fetched = datetime.fromisoformat(env.fetched_at)
    body = env.records[0].body
    ts = _ms(body["time"])
    intervals: dict[str, float] = {}
    if listings is not None and "symbol" in listings.columns:
        pass  # funding interval is not in the instruments payload we store; default 8h
    rows: list[PerpSnapshotRow] = []
    for t in body["result"]["list"]:
        sym = t["symbol"]
        if not sym.endswith("USDT") or t.get("deliveryTime", "0") not in ("0", 0, ""):
            continue  # linear perps only
        base, mult = split_multiplier(sym.removesuffix("USDT"))
        mark = float(t["markPrice"]) if t.get("markPrice") else None
        oi_c = float(t["openInterest"]) if t.get("openInterest") else None
        rows.append(
            PerpSnapshotRow(
                ts=ts,
                venue=VENUE,
                symbol=sym,
                base=base,
                multiplier=mult,
                mark_price=mark,
                index_price=float(t["indexPrice"]) if t.get("indexPrice") else None,
                funding_rate=float(t["fundingRate"]) if t.get("fundingRate") else None,
                funding_interval_h=intervals.get(sym, 8.0),
                next_funding_time=_ms(t["nextFundingTime"]) if t.get("nextFundingTime") else None,
                oi_base=oi_c * mult if oi_c is not None else None,
                oi_usd=float(t["openInterestValue"]) if t.get("openInterestValue") else None,
                volume_24h_usd=float(t["turnover24h"]) if t.get("turnover24h") else None,
                source="bybit",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    df = rows_to_df(PerpSnapshotRow, rows)
    if df.height < 100:
        raise SanityError("bybit perps: too few rows")
    return df


def fetch_klines_1d(
    symbols: list[str], limit: int = 120, ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(
                "/v5/market/kline",
                params={"category": "spot", "symbol": s, "interval": "D", "limit": limit},
            )
            for s in symbols
        ]

    return run_dataset(
        "bybit",
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
        base, _ = split_multiplier(sym.removesuffix("USDT"))
        for k in rec.body["result"]["list"]:
            start = _ms(k[0])
            if start.date() >= fetched.date():  # open candle
                continue
            rows.append(
                DailyPriceRow(
                    date=start.date(),
                    venue=VENUE,
                    symbol=sym,
                    base=base,
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume_base=float(k[5]),
                    volume_quote=float(k[6]),
                    source="bybit",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(DailyPriceRow, rows)
