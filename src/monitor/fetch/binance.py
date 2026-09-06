"""Binance adapters (spot `api.binance.com`, USDT-M `fapi.binance.com`, COIN-M `dapi.binance.com`).
Endpoints and fields verified 2026-09-06 — docs/data_sources.md §1–2."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.fetch.symbols import split_multiplier
from monitor.schema.tables import DailyPriceRow, PerpSnapshotRow, VenueListingRow, rows_to_df

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
    def go(c) -> list[Record]:
        recs = [
            c.get("/fapi/v1/premiumIndex"),
            c.get("/fapi/v1/fundingInfo"),
            c.get("/fapi/v1/ticker/24hr"),
        ]
        for s in symbols:
            recs.append(c.get("/fapi/v1/openInterest", params={"symbol": s}))
        return recs

    return run_dataset(
        "binance_usdm", "perps", freq, go, ts=ts, force=force, meta={"symbols": symbols}
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
