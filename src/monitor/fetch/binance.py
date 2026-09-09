"""Binance adapters (spot `api.binance.com`, USDT-M `fapi.binance.com`, COIN-M `dapi.binance.com`).
Endpoints and fields verified 2026-09-06 — docs/data_sources.md §1–2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset, trim_book
from monitor.fetch.symbols import split_multiplier
from monitor.schema.tables import (
    DailyPriceRow,
    LiquidationRow,
    PerpSnapshotRow,
    VenueListingRow,
    rows_to_df,
)

VENUE = "binance"


def _ms(x: int | str) -> datetime:
    return datetime.fromtimestamp(int(x) / 1000, tz=UTC)


# ---------- listings ----------
def fetch_listings(ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        spot = c.get("/api/v3/exchangeInfo")
        return [spot]

    p1 = run_dataset("binance_spot", "listings", "daily", go, ts=ts, force=force)

    def go_fut(c) -> list[Record]:
        return [c.get("/fapi/v1/exchangeInfo")]

    run_dataset("binance_usdm", "listings", "daily", go_fut, ts=ts, force=force)
    return p1


def parse_listings(spot_env: Envelope, fut_env: Envelope, git_sha: str) -> pl.DataFrame:
    rows: list[VenueListingRow] = []
    ts = datetime.fromisoformat(spot_env.fetched_at)
    for s in spot_env.records[0].body["symbols"]:
        if not s.get("isSpotTradingAllowed", True):
            continue
        rows.append(
            VenueListingRow(
                ts=ts,
                venue=VENUE,
                market="spot",
                symbol=s["symbol"],
                base=s["baseAsset"],
                quote=s["quoteAsset"],
                status=s["status"],
                source="binance_spot",
                fetched_at=ts,
                git_sha=git_sha,
            )
        )
    tsf = datetime.fromisoformat(fut_env.fetched_at)
    for s in fut_env.records[0].body["symbols"]:
        if s["contractType"] not in ("PERPETUAL", "CURRENT_QUARTER", "NEXT_QUARTER"):
            continue
        base, mult = split_multiplier(s["baseAsset"])
        rows.append(
            VenueListingRow(
                ts=tsf,
                venue=VENUE,
                market="perp" if s["contractType"] == "PERPETUAL" else "future",
                symbol=s["symbol"],
                base=base,
                quote=s["quoteAsset"],
                multiplier=mult,
                status=s["status"],
                delivery=_ms(s["deliveryDate"]) if s["contractType"] != "PERPETUAL" else None,
                source="binance_usdm",
                fetched_at=tsf,
                git_sha=git_sha,
            )
        )
    df = rows_to_df(VenueListingRow, rows)
    if (
        df.filter(pl.col("market") == "spot").height < 500
        or df.filter(pl.col("market") == "perp").height < 100
    ):
        raise SanityError("binance listings: unexpectedly few symbols")
    return df


# ---------- perp snapshot: premiumIndex (all) + openInterest per symbol ----------
def fetch_perps(
    symbols: list[str], ts: datetime | None = None, force: bool = False, freq: str = "daily"
) -> Path:
    """premiumIndex / fundingInfo / ticker24h are full-market payloads (≈ 900 symbols); the
    stored copy keeps the requested symbols plus dated futures (basis), recorded in meta."""

    def go(c) -> list[Record]:
        keep = set(symbols)
        recs = [
            c.get("/fapi/v1/premiumIndex"),
            c.get("/fapi/v1/fundingInfo"),
            c.get("/fapi/v1/ticker/24hr"),
        ]
        for r in recs:
            r.body = [x for x in r.body if x.get("symbol") in keep or "_" in x.get("symbol", "")]
        for s in symbols:
            recs.append(c.get("/fapi/v1/openInterest", params={"symbol": s}))
        return recs

    return run_dataset(
        "binance_usdm",
        "perps",
        freq,
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "filtered_to_symbols": True},
    )


def parse_perps(env: Envelope, listings: pl.DataFrame | None = None) -> pl.DataFrame:
    prem = {r["symbol"]: r for r in env.records[0].body}
    finfo = {r["symbol"]: r for r in env.records[1].body}
    t24 = {r["symbol"]: r for r in env.records[2].body}
    fetched = datetime.fromisoformat(env.fetched_at)
    rows: list[PerpSnapshotRow] = []
    for rec in env.records[3:]:
        b = rec.body
        sym = b["symbol"]
        p = prem.get(sym)
        if p is None:
            continue
        base, mult = split_multiplier(sym.removesuffix("USDT").removesuffix("USDC"))
        mark = float(p["markPrice"])
        oi_contracts = float(b["openInterest"])
        rows.append(
            PerpSnapshotRow(
                ts=_ms(b["time"]),
                venue=VENUE,
                symbol=sym,
                base=base,
                multiplier=mult,
                mark_price=mark,
                index_price=float(p["indexPrice"]),
                funding_rate=float(p["lastFundingRate"]),
                funding_interval_h=float(finfo.get(sym, {}).get("fundingIntervalHours", 8)),
                next_funding_time=_ms(p["nextFundingTime"]) if p.get("nextFundingTime") else None,
                oi_base=oi_contracts * mult,
                oi_usd=oi_contracts * mark,
                volume_24h_usd=float(t24[sym]["quoteVolume"]) if sym in t24 else None,
                source="binance_usdm",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    df = rows_to_df(PerpSnapshotRow, rows)
    _sanity_perps(df, fetched)
    return df


def _sanity_perps(df: pl.DataFrame, fetched: datetime) -> None:
    if not df.height:
        raise SanityError("binance perps: no rows")
    if (df["oi_usd"] < 0).any() or (df["mark_price"] <= 0).any():
        raise SanityError("binance perps: negative OI or non-positive mark")
    age = (fetched - df["ts"].max()).total_seconds()
    if age > 3 * 3600:
        raise SanityError(f"binance perps: stale by {age / 3600:.1f} h")


# ---------- daily spot klines ----------
def fetch_klines_1d(
    symbols: list[str], limit: int = 120, ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get("/api/v3/klines", params={"symbol": s, "interval": "1d", "limit": limit})
            for s in symbols
        ]

    return run_dataset(
        "binance_spot",
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
        for k in rec.body:
            close_ts = _ms(k[6])
            if close_ts > fetched:  # skip the still-open candle
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
                    source="binance_spot",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(DailyPriceRow, rows)


# ---------- hourly: order books, trades, 1h klines, long/short ratio, dated futures ----------
from monitor.compute.liquidity import benford_stats, depth_from_levels  # noqa: E402
from monitor.schema.tables import (  # noqa: E402
    FuturesMarkRow,
    HourlyPriceRow,
    LongShortRow,
    OrderBookDepthRow,
    TradeStatsRow,
)


def fetch_books(
    symbols: list[str],
    majors: tuple[str, ...] = ("BTCUSDT", "ETHUSDT"),
    ts: datetime | None = None,
    force: bool = False,
    freq: str = "hourly",
) -> Path:
    """`api/v3/depth`: 5000 levels for the majors (weight 250), 1000 otherwise (weight 50).
    Stored trimmed to ±3 % of mid (`trim_book`); the envelope records `trimmed_pct`."""

    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            r = c.get("/api/v3/depth", params={"symbol": s, "limit": 5000 if s in majors else 1000})
            b, a, _ = trim_book(r.body["bids"], r.body["asks"])
            r.body = {"lastUpdateId": r.body["lastUpdateId"], "bids": b, "asks": a}
            recs.append(r)
        return recs

    return run_dataset(
        "binance_spot",
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
        b = rec.body
        bids = [(float(p), float(q)) for p, q in b["bids"]]
        asks = [(float(p), float(q)) for p, q in b["asks"]]
        if not bids or not asks:
            continue
        d = depth_from_levels(bids, asks, delta)
        base, _ = split_multiplier(sym.removesuffix("USDT"))
        rows.append(
            OrderBookDepthRow(
                ts=datetime.fromisoformat(rec.fetched_at),
                venue=VENUE,
                symbol=sym,
                base=base,
                **d,
                source="binance_spot",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(OrderBookDepthRow, rows)


def fetch_trades(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    """500 recent trades per symbol, stored slim as [time, price, qty, quoteQty]."""

    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            r = c.get("/api/v3/trades", params={"symbol": s, "limit": 300})
            r.body = [[t["time"], t["price"], t["qty"], t["quoteQty"]] for t in r.body]
            recs.append(r)
        return recs

    return run_dataset(
        "binance_spot",
        "trades",
        "hourly",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "slim": ["time", "price", "qty", "quoteQty"]},
    )


def _trade_stats(
    rec_ts: datetime,
    sym: str,
    base: str,
    notionals: list[float],
    times_ms: list[int],
    fetched: datetime,
    sha: str,
    source: str,
    sizes: list[float] | None = None,
) -> TradeStatsRow:
    bs = benford_stats(sizes if sizes is not None else notionals)  # notes: trade-size digits
    span = (max(times_ms) - min(times_ms)) / 1000 if len(times_ms) > 1 else 0.0
    srt = sorted(notionals)
    med = srt[len(srt) // 2] if srt else 0.0
    return TradeStatsRow(
        ts=rec_ts,
        venue=VENUE if source.startswith("binance") else source,
        symbol=sym,
        base=base,
        n_trades=len(notionals),
        span_s=span,
        notional_usd=sum(notionals),
        median_size_usd=med,
        benford_chi2=bs["chi2"],
        benford_p=bs["p"] if bs["p"] is not None else float("nan"),
        first_digit_shares=[f"{s:.4f}" for s in bs["shares"]],
        source=source,
        fetched_at=fetched,
        git_sha=sha,
    )


def parse_trades(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        base, _ = split_multiplier(sym.removesuffix("USDT"))
        body = [
            {"time": t[0], "price": t[1], "qty": t[2], "quoteQty": t[3]}
            if isinstance(t, list)
            else t
            for t in rec.body
        ]
        notionals = [float(t["quoteQty"]) for t in body]
        times = [int(t["time"]) for t in body]
        if notionals:
            rows.append(
                _trade_stats(
                    datetime.fromisoformat(rec.fetched_at),
                    sym,
                    base,
                    notionals,
                    times,
                    fetched,
                    env.git_sha,
                    "binance_spot",
                    [float(t["qty"]) for t in body],
                )
            )
    return rows_to_df(TradeStatsRow, rows)


def fetch_klines_1h(
    symbols: list[str], limit: int = 168, ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get("/api/v3/klines", params={"symbol": s, "interval": "1h", "limit": limit})
            for s in symbols
        ]

    return run_dataset(
        "binance_spot",
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
        for k in rec.body:
            if _ms(k[6]) > fetched:
                continue
            rows.append(
                HourlyPriceRow(
                    ts=_ms(k[0]),
                    venue=VENUE,
                    symbol=sym,
                    base=base,
                    open=float(k[1]),
                    high=float(k[2]),
                    low=float(k[3]),
                    close=float(k[4]),
                    volume_base=float(k[5]),
                    volume_quote=float(k[7]),
                    source="binance_spot",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(HourlyPriceRow, rows)


def fetch_long_short(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(
                "/futures/data/topLongShortPositionRatio",
                params={"symbol": s, "period": "1h", "limit": 1},
            )
            for s in symbols
        ]

    return run_dataset(
        "binance_usdm", "long_short", "hourly", go, ts=ts, force=force, meta={"symbols": symbols}
    )


def parse_long_short(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        for r in rec.body:
            base, _ = split_multiplier(sym.removesuffix("USDT"))
            rows.append(
                LongShortRow(
                    ts=_ms(r["timestamp"]),
                    venue=VENUE,
                    symbol=sym,
                    base=base,
                    long_share=float(r["longAccount"]),
                    source="binance_usdm",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(LongShortRow, rows)


def parse_futures_marks(perps_env: Envelope, listings: pl.DataFrame) -> pl.DataFrame:
    """Dated USDT-M contracts (`BTCUSDT_261225`) appear in `premiumIndex`; expiry from listings."""
    fetched = datetime.fromisoformat(perps_env.fetched_at)
    exp = {
        r["symbol"]: r["delivery"]
        for r in listings.filter((pl.col("venue") == VENUE) & (pl.col("market") == "future"))
        .select("symbol", "delivery")
        .to_dicts()
    }
    rows = []
    for p in perps_env.records[0].body:
        if "_" in p["symbol"] and p["symbol"] in exp and exp[p["symbol"]] is not None:
            base, _ = split_multiplier(p["symbol"].split("_")[0].removesuffix("USDT"))
            rows.append(
                FuturesMarkRow(
                    ts=_ms(p["time"]),
                    venue=VENUE,
                    symbol=p["symbol"],
                    base=base,
                    expiry=exp[p["symbol"]],
                    mark_price=float(p["markPrice"]),
                    index_price=float(p["indexPrice"]),
                    settle_ccy="USDT",
                    source="binance_usdm",
                    fetched_at=fetched,
                    git_sha=perps_env.git_sha,
                )
            )
    return rows_to_df(FuturesMarkRow, rows)


def fetch_coinm_marks(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "binance_coinm",
        "marks",
        "hourly",
        lambda c: [c.get("/dapi/v1/premiumIndex"), c.get("/dapi/v1/exchangeInfo")],
        ts=ts,
        force=force,
    )


def parse_coinm_marks(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    exp = {
        s["symbol"]: _ms(s["deliveryDate"])
        for s in env.records[1].body["symbols"]
        if s["contractType"] != "PERPETUAL"
    }
    rows = []
    for p in env.records[0].body:
        if p["symbol"] in exp:
            rows.append(
                FuturesMarkRow(
                    ts=_ms(p["time"]),
                    venue=VENUE,
                    symbol=p["symbol"],
                    base=p["pair"].removesuffix("USD"),
                    expiry=exp[p["symbol"]],
                    mark_price=float(p["markPrice"]),
                    index_price=float(p["indexPrice"]),
                    settle_ccy="COIN",
                    source="binance_coinm",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(FuturesMarkRow, rows)


def parse_liquidations_ws(env: Envelope) -> pl.DataFrame:
    """`!forceOrder@arr` messages collected by monitor.fetch.liq_ws. `o.S` is the side of the
    forced order: SELL closes a long, BUY closes a short. Quantity `q` is in base units for
    USDⓈ-M contracts; `ap` is the average fill price (falls back to `p`)."""
    from datetime import datetime

    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        for m in rec.body or []:
            o = m.get("o") or {}
            if not o or "s" not in o:
                continue
            sym = o["s"]
            px = float(o.get("ap") or o.get("p") or 0)
            q = float(o.get("z") or o.get("q") or 0)
            if px <= 0 or q <= 0:
                continue
            if "USD_" in sym:  # COIN-M (BTCUSD_PERP, ETHUSD_251226): q is in contracts
                base, mult = sym.split("USD_")[0], 1.0
                notional = q * (100.0 if base == "BTC" else 10.0)  # contract size in USD
                q = notional / px  # base units
            else:  # USDⓈ-M symbols carried on the same feed: q in base units (times multiplier)
                base, mult = split_multiplier(
                    sym.removesuffix("USDT").removesuffix("USDC").removesuffix("BUSD")
                )
                notional = q * px  # contract quantity × contract price, whatever the multiplier
                q, px = q * mult, px / mult  # base units and price per base unit
            rows.append(
                LiquidationRow(
                    ts=datetime.fromtimestamp(int(o.get("T") or m.get("E")) / 1000, tz=UTC),
                    venue="binance",
                    symbol=sym,
                    base=base,
                    side_closed="long" if o.get("S") == "SELL" else "short",
                    price=px,
                    size_base=q,
                    notional_usd=notional,
                    source="binance_ws",
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    return rows_to_df(LiquidationRow, rows)


def parse_liq_coverage(env: Envelope) -> pl.DataFrame:
    """Collector coverage for one venue-hour from the websocket envelope's meta: connected
    seconds / 3600 and the number of messages (work order 2, item 5)."""
    from datetime import datetime

    cov = (env.meta or {}).get("coverage") or {}
    hour = cov.get("hour") or (env.meta or {}).get("hour")
    schema = {
        "hour": pl.Datetime("us", "UTC"),
        "venue": pl.Utf8,
        "connected_share": pl.Float64,
        "n_messages": pl.Int64,
        "source": pl.Utf8,
        "fetched_at": pl.Datetime("us", "UTC"),
        "git_sha": pl.Utf8,
    }
    if not hour:
        return pl.DataFrame(schema=schema)
    n_msgs = int(cov.get("n_messages") or sum(len(r.body or []) for r in env.records))
    return pl.DataFrame(
        [
            {
                "hour": datetime.fromisoformat(hour),
                "venue": "binance",
                "connected_share": min(1.0, float(cov.get("connected_seconds") or 0.0) / 3600.0),
                "n_messages": n_msgs,
                "source": "binance_ws",
                "fetched_at": datetime.fromisoformat(env.fetched_at),
                "git_sha": env.git_sha,
            }
        ],
        schema=schema,
    )
