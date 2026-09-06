"""Kraken adapter (`api.kraken.com`). Verified 2026-09-06. Legacy asset codes normalised."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset, trim_book
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


def fetch_klines_1d(
    symbols: list[str],
    ts: datetime | None = None,
    force: bool = False,
    since: datetime | None = None,
) -> Path:
    """Daily OHLC (720 rows by default); with `since` (verified) only rows after that time."""

    def go(c) -> list[Record]:
        params = {"interval": 1440}
        if since is not None:
            params["since"] = int(since.timestamp())
        return [c.get("/0/public/OHLC", params={**params, "pair": s}) for s in symbols]

    return run_dataset(
        "kraken",
        "klines_1d",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "since": str(since) if since else None},
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


# ---------- hourly ----------
from monitor.compute.liquidity import benford_stats, depth_from_levels  # noqa: E402
from monitor.schema.tables import HourlyPriceRow, OrderBookDepthRow, TradeStatsRow  # noqa: E402


def fetch_books(
    symbols: list[str], ts: datetime | None = None, force: bool = False, freq: str = "hourly"
) -> Path:
    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            r = c.get("/0/public/Depth", params={"pair": s, "count": 300})
            res = r.body["result"]
            key = next(iter(res))
            b, a, _ = trim_book(res[key]["bids"], res[key]["asks"])
            r.body = {"result": {key: {"bids": b, "asks": a}}}
            recs.append(r)
        return recs

    return run_dataset(
        "kraken",
        "books",
        freq,
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "trimmed_pct": 0.025},
    )


def parse_books(env: Envelope, delta: float = 0.02) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        res = rec.body["result"]
        key = next(iter(res))
        bids = [(float(x[0]), float(x[1])) for x in res[key]["bids"]]
        asks = [(float(x[0]), float(x[1])) for x in res[key]["asks"]]
        if not bids or not asks:
            continue
        rows.append(
            OrderBookDepthRow(
                ts=datetime.fromisoformat(rec.fetched_at),
                venue=VENUE,
                symbol=sym,
                base=kraken_base(sym.removesuffix("USD")),
                **depth_from_levels(bids, asks, delta),
                source="kraken",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(OrderBookDepthRow, rows)


def fetch_trades(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    """500 recent trades (`count=500` verified), stored slim as [price, vol, time]."""

    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            r = c.get("/0/public/Trades", params={"pair": s, "count": 300})
            res = r.body["result"]
            key = next(k for k in res if k != "last")
            r.body = {
                "result": {key: [[t[0], t[1], t[2]] for t in res[key]], "last": res.get("last")}
            }
            recs.append(r)
        return recs

    return run_dataset(
        "kraken",
        "trades",
        "hourly",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "slim": ["price", "vol", "time"]},
    )


def parse_trades(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        res = rec.body["result"]
        key = next(k for k in res if k != "last")
        lst = res[key]
        if not lst:
            continue
        notionals = [float(t[0]) * float(t[1]) for t in lst]
        times = [float(t[2]) for t in lst]
        bs = benford_stats([float(t[1]) for t in lst])  # trade sizes in base units
        srt = sorted(notionals)
        rows.append(
            TradeStatsRow(
                ts=datetime.fromtimestamp(max(times), tz=UTC),
                venue=VENUE,
                symbol=sym,
                base=kraken_base(sym.removesuffix("USD")),
                n_trades=len(notionals),
                span_s=max(times) - min(times),
                notional_usd=sum(notionals),
                median_size_usd=srt[len(srt) // 2],
                benford_chi2=bs["chi2"],
                benford_p=bs["p"] if bs["p"] is not None else float("nan"),
                first_digit_shares=[f"{s:.4f}" for s in bs["shares"]],
                source="kraken",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(TradeStatsRow, rows)


def fetch_klines_1h(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get("/0/public/OHLC", params={"pair": s, "interval": 60}) for s in symbols]

    return run_dataset(
        "kraken", "klines_1h", "daily", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_klines_1h(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        res = rec.body["result"]
        key = next(k for k in res if k != "last")
        base = kraken_base(sym.removesuffix("USD"))
        for k in res[key]:
            t = datetime.fromtimestamp(k[0], tz=UTC)
            if t + timedelta(hours=1) > fetched:
                continue
            rows.append(
                HourlyPriceRow(
                    ts=t,
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
    return rows_to_df(HourlyPriceRow, rows)
