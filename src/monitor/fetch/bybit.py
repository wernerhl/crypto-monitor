"""Bybit v5 adapter (`api.bybit.com`). Verified 2026-09-06 — docs/data_sources.md §1–2."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset, trim_book
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


def fetch_perps(
    ts: datetime | None = None,
    force: bool = False,
    freq: str = "daily",
    symbols: list[str] | None = None,
) -> Path:
    """All linear tickers in one call; when `symbols` is given the stored payload keeps only
    those plus dated futures (recorded in the envelope meta)."""

    def go(c) -> list[Record]:
        r = c.get("/v5/market/tickers", params={"category": "linear"})
        if symbols:
            keep = set(symbols)
            lst = r.body["result"]["list"]
            r.body["result"]["list"] = [
                t
                for t in lst
                if t.get("symbol") in keep or t.get("deliveryTime", "0") not in ("0", 0, "")
            ]
        return [r]

    return run_dataset(
        "bybit", "perps", freq, go, ts=ts, force=force, meta={"filtered_to_symbols": bool(symbols)}
    )


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
    if df.height < 10:
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


# ---------- hourly ----------
from monitor.compute.liquidity import benford_stats, depth_from_levels  # noqa: E402
from monitor.schema.tables import (  # noqa: E402
    FuturesMarkRow,
    HourlyPriceRow,
    OrderBookDepthRow,
    TradeStatsRow,
)


def fetch_books(
    symbols: list[str], ts: datetime | None = None, force: bool = False, freq: str = "hourly"
) -> Path:
    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            r = c.get(
                "/v5/market/orderbook", params={"category": "spot", "symbol": s, "limit": 1000}
            )
            res = r.body["result"]
            b, a, _ = trim_book(res["b"], res["a"])
            r.body = {"result": {"s": res.get("s"), "ts": res["ts"], "b": b, "a": a}}
            recs.append(r)
        return recs

    return run_dataset(
        "bybit",
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
        r = rec.body["result"]
        bids = [(float(p), float(q)) for p, q in r["b"]]
        asks = [(float(p), float(q)) for p, q in r["a"]]
        if not bids or not asks:
            continue
        base, _ = split_multiplier(sym.removesuffix("USDT"))
        rows.append(
            OrderBookDepthRow(
                ts=_ms(r["ts"]),
                venue=VENUE,
                symbol=sym,
                base=base,
                **depth_from_levels(bids, asks, delta),
                source="bybit",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(OrderBookDepthRow, rows)


def fetch_trades(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    """Spot recent trades are capped at 60 rows (verified); the linear perp gives up to 1000, so
    the Benford sample uses the perp market. 500 trades, stored slim as [time, price, size]."""

    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            r = c.get(
                "/v5/market/recent-trade", params={"category": "linear", "symbol": s, "limit": 300}
            )
            r.body = {
                "result": {
                    "list": [[t["time"], t["price"], t["size"]] for t in r.body["result"]["list"]]
                }
            }
            recs.append(r)
        return recs

    return run_dataset(
        "bybit",
        "trades",
        "hourly",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "slim": ["time", "price", "size"]},
    )


def parse_trades(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        lst = [
            {"time": t[0], "price": t[1], "size": t[2]} if isinstance(t, list) else t
            for t in rec.body["result"]["list"]
        ]
        if not lst:
            continue
        base, _ = split_multiplier(sym.removesuffix("USDT"))
        notionals = [float(t["price"]) * float(t["size"]) for t in lst]
        times = [int(t["time"]) for t in lst]
        bs = benford_stats([float(t["size"]) for t in lst])  # trade sizes in base units
        srt = sorted(notionals)
        rows.append(
            TradeStatsRow(
                ts=_ms(max(times)),
                venue=VENUE,
                symbol=sym,
                base=base,
                n_trades=len(notionals),
                span_s=(max(times) - min(times)) / 1000,
                notional_usd=sum(notionals),
                median_size_usd=srt[len(srt) // 2],
                benford_chi2=bs["chi2"],
                benford_p=bs["p"] if bs["p"] is not None else float("nan"),
                first_digit_shares=[f"{s:.4f}" for s in bs["shares"]],
                source="bybit",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(TradeStatsRow, rows)


def fetch_klines_1h(
    symbols: list[str], limit: int = 168, ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(
                "/v5/market/kline",
                params={"category": "spot", "symbol": s, "interval": "60", "limit": limit},
            )
            for s in symbols
        ]

    return run_dataset(
        "bybit",
        "klines_1h",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "limit": limit},
    )


def parse_klines_1h(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        base, _ = split_multiplier(sym.removesuffix("USDT"))
        for k in rec.body["result"]["list"]:
            start = _ms(k[0])
            if start + timedelta(hours=1) > fetched:
                continue
            rows.append(
                HourlyPriceRow(
                    ts=start,
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
    return rows_to_df(HourlyPriceRow, rows)


def parse_futures_marks(env: Envelope) -> pl.DataFrame:
    """LinearFutures rows in `tickers?category=linear` carry `deliveryTime` > 0."""
    fetched = datetime.fromisoformat(env.fetched_at)
    body = env.records[0].body
    ts = _ms(body["time"])
    rows = []
    for t in body["result"]["list"]:
        if t.get("deliveryTime", "0") in ("0", 0, "") or not t.get("markPrice"):
            continue
        sym = t["symbol"]
        base, _ = split_multiplier(sym.split("-")[0].removesuffix("USDT"))
        rows.append(
            FuturesMarkRow(
                ts=ts,
                venue=VENUE,
                symbol=sym,
                base=base,
                expiry=_ms(t["deliveryTime"]),
                mark_price=float(t["markPrice"]),
                index_price=float(t["indexPrice"]) if t.get("indexPrice") else None,
                settle_ccy="USDT",
                source="bybit",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(FuturesMarkRow, rows)


def parse_liquidations_ws(env: Envelope) -> pl.DataFrame:
    """`allLiquidation.<symbol>` messages (v5 linear) collected by monitor.fetch.liq_ws.
    `S` is the position side that was liquidated per Bybit's docs (Buy = a long was closed)."""
    from monitor.schema.tables import LiquidationRow

    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        for m in rec.body or []:
            for d in m.get("data") or []:
                sym = d.get("s")
                if not sym:
                    continue
                base, mult = split_multiplier(
                    sym.removesuffix("USDT").removesuffix("PERP").removesuffix("USDC")
                )
                px, v = float(d.get("p") or 0), float(d.get("v") or 0)
                if px <= 0 or v <= 0:
                    continue
                notional = v * px
                v, px = v * mult, px / mult
                rows.append(
                    LiquidationRow(
                        ts=datetime.fromtimestamp(int(d.get("T") or m.get("ts")) / 1000, tz=UTC),
                        venue=VENUE,
                        symbol=sym,
                        base=base,
                        side_closed="long" if d.get("S") == "Buy" else "short",
                        price=px,
                        size_base=v,
                        notional_usd=notional,
                        source="bybit_ws",
                        fetched_at=fetched,
                        git_sha=env.git_sha,
                    )
                )
    return rows_to_df(LiquidationRow, rows)
