"""Coinbase Exchange adapter (`api.exchange.coinbase.com`). Verified 2026-09-06."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
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


# ---------- hourly ----------
from monitor.compute.liquidity import benford_stats, depth_from_levels  # noqa: E402
from monitor.schema.tables import HourlyPriceRow, OrderBookDepthRow, TradeStatsRow  # noqa: E402


def fetch_books(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get(f"/products/{s}/book", params={"level": 2}) for s in symbols]

    return run_dataset(
        "coinbase", "books", "hourly", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_books(env: Envelope, delta: float = 0.02) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        b = rec.body
        bids = [(float(x[0]), float(x[1])) for x in b["bids"]]
        asks = [(float(x[0]), float(x[1])) for x in b["asks"]]
        if not bids or not asks:
            continue
        ts = (
            datetime.fromisoformat(b["time"].replace("Z", "+00:00"))
            if b.get("time")
            else datetime.fromisoformat(rec.fetched_at)
        )
        rows.append(
            OrderBookDepthRow(
                ts=ts,
                venue=VENUE,
                symbol=sym,
                base=sym.split("-")[0],
                **depth_from_levels(bids, asks, delta),
                source="coinbase",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(OrderBookDepthRow, rows)


def fetch_trades(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get(f"/products/{s}/trades", params={"limit": 1000}) for s in symbols]

    return run_dataset(
        "coinbase", "trades", "hourly", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_trades(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        lst = rec.body
        if not lst:
            continue
        notionals = [float(t["price"]) * float(t["size"]) for t in lst]
        times = [datetime.fromisoformat(t["time"].replace("Z", "+00:00")) for t in lst]
        bs = benford_stats([float(t["size"]) for t in lst])  # trade sizes in base units
        srt = sorted(notionals)
        rows.append(
            TradeStatsRow(
                ts=max(times),
                venue=VENUE,
                symbol=sym,
                base=sym.split("-")[0],
                n_trades=len(notionals),
                span_s=(max(times) - min(times)).total_seconds(),
                notional_usd=sum(notionals),
                median_size_usd=srt[len(srt) // 2],
                benford_chi2=bs["chi2"],
                benford_p=bs["p"] if bs["p"] is not None else float("nan"),
                first_digit_shares=[f"{s:.4f}" for s in bs["shares"]],
                source="coinbase",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(TradeStatsRow, rows)


def fetch_klines_1h(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get(f"/products/{s}/candles", params={"granularity": 3600}) for s in symbols]

    return run_dataset(
        "coinbase", "klines_1h", "daily", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_klines_1h(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        for k in rec.body:
            t = datetime.fromtimestamp(k[0], tz=UTC)
            if t + timedelta(hours=1) > fetched:
                continue
            rows.append(
                HourlyPriceRow(
                    ts=t,
                    venue=VENUE,
                    symbol=sym,
                    base=sym.split("-")[0],
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
    return rows_to_df(HourlyPriceRow, rows)
