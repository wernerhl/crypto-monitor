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


# ---------- hourly ----------
from monitor.compute.liquidity import benford_stats, depth_from_levels  # noqa: E402
from monitor.schema.tables import (  # noqa: E402
    FuturesMarkRow,
    HourlyPriceRow,
    LiquidationRow,
    OrderBookDepthRow,
    TradeStatsRow,
)


def fetch_funding(inst_ids: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get("/api/v5/public/funding-rate", params={"instId": i}) for i in inst_ids]

    return run_dataset(
        "okx", "funding", "hourly", go, ts=ts, force=force, meta={"inst_ids": inst_ids}
    )


def parse_funding(env: Envelope) -> pl.DataFrame:
    """Returns instId, funding_rate, funding_interval_h (from fundingTime − prevFundingTime), next_funding_time."""
    rows = []
    for rec in env.records:
        for d in rec.body["data"]:
            interval = (
                (int(d["fundingTime"]) - int(d["prevFundingTime"])) / 3_600_000
                if d.get("prevFundingTime")
                else 8.0
            )
            rows.append(
                {
                    "symbol": d["instId"],
                    "funding_rate": float(d["fundingRate"]),
                    "funding_interval_h": interval,
                    "next_funding_time": _ms(d["fundingTime"]),
                }
            )
    return pl.DataFrame(
        rows,
        schema={
            "symbol": pl.Utf8,
            "funding_rate": pl.Float64,
            "funding_interval_h": pl.Float64,
            "next_funding_time": pl.Datetime("us", "UTC"),
        },
    )


def fetch_books(
    symbols: list[str],
    majors: tuple[str, ...] = ("BTC-USDT", "ETH-USDT"),
    ts: datetime | None = None,
    force: bool = False,
) -> Path:
    """`books-full` (5000 levels) for the majors, `books` (400) otherwise — 400 levels reach
    well past 2 % on Tier 1 alts (verified SOL-USDT)."""

    def go(c) -> list[Record]:
        return [
            c.get(
                "/api/v5/market/books-full" if s in majors else "/api/v5/market/books",
                params={"instId": s, "sz": 5000 if s in majors else 400},
            )
            for s in symbols
        ]

    return run_dataset("okx", "books", "hourly", go, ts=ts, force=force, meta={"symbols": symbols})


def parse_books(env: Envelope, delta: float = 0.02) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        d = rec.body["data"][0]
        bids = [(float(x[0]), float(x[1])) for x in d["bids"]]
        asks = [(float(x[0]), float(x[1])) for x in d["asks"]]
        if not bids or not asks:
            continue
        rows.append(
            OrderBookDepthRow(
                ts=_ms(d["ts"]),
                venue=VENUE,
                symbol=sym,
                base=sym.split("-")[0],
                **depth_from_levels(bids, asks, delta),
                source="okx",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(OrderBookDepthRow, rows)


def fetch_trades(symbols: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get("/api/v5/market/trades", params={"instId": s, "limit": 500}) for s in symbols]

    return run_dataset("okx", "trades", "hourly", go, ts=ts, force=force, meta={"symbols": symbols})


def parse_trades(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, sym in zip(env.records, env.meta["symbols"], strict=True):
        lst = rec.body["data"]
        if not lst:
            continue
        notionals = [float(t["px"]) * float(t["sz"]) for t in lst]
        times = [int(t["ts"]) for t in lst]
        bs = benford_stats([float(t["sz"]) for t in lst])  # trade sizes in base units
        srt = sorted(notionals)
        rows.append(
            TradeStatsRow(
                ts=_ms(max(times)),
                venue=VENUE,
                symbol=sym,
                base=sym.split("-")[0],
                n_trades=len(notionals),
                span_s=(max(times) - min(times)) / 1000,
                notional_usd=sum(notionals),
                median_size_usd=srt[len(srt) // 2],
                benford_chi2=bs["chi2"],
                benford_p=bs["p"] if bs["p"] is not None else float("nan"),
                first_digit_shares=[f"{s:.4f}" for s in bs["shares"]],
                source="okx",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(TradeStatsRow, rows)


def fetch_liquidations(
    ulys: list[str],
    since_ms: int | None = None,
    max_pages: int = 5,
    ts: datetime | None = None,
    force: bool = False,
) -> Path:
    """`liquidation-orders?instType=SWAP&state=filled&uly=X` returns the newest 100 filled
    orders; `after=<ts>` pages back (verified). We page until `since_ms` or `max_pages`."""

    def go(c) -> list[Record]:
        recs = []
        for u in ulys:
            after = None
            for _ in range(max_pages):
                params = {"instType": "SWAP", "state": "filled", "uly": u, "limit": 100}
                if after:
                    params["after"] = after
                r = c.get("/api/v5/public/liquidation-orders", params=params)
                recs.append(r)
                det = r.body["data"][0]["details"] if r.body.get("data") else []
                if not det:
                    break
                oldest = min(int(x["ts"]) for x in det)
                if since_ms is not None and oldest <= since_ms:
                    break
                after = str(oldest)
        return recs

    return run_dataset(
        "okx",
        "liquidations",
        "hourly",
        go,
        ts=ts,
        force=force,
        meta={"ulys": ulys, "since_ms": since_ms},
    )


def parse_liquidations(env: Envelope, ct_val: dict[str, float] | None = None) -> pl.DataFrame:
    """`sz` is in contracts; USD notional = sz × ctVal × bkPx for linear swaps (ctVal in base
    units from the instruments list; default 1 when unknown, flagged by `ct_val` absence)."""
    fetched = datetime.fromisoformat(env.fetched_at)
    ct_val = ct_val or {}
    rows = []
    for rec in env.records:
        for d in rec.body.get("data", []):
            inst = d["instId"]
            base = inst.split("-")[0]
            cv = ct_val.get(inst, 1.0)
            for x in d["details"]:
                px, sz = float(x["bkPx"]), float(x["sz"])
                rows.append(
                    LiquidationRow(
                        ts=_ms(x["ts"]),
                        venue=VENUE,
                        symbol=inst,
                        base=base,
                        side_closed=x["posSide"],
                        price=px,
                        size_base=sz * cv,
                        notional_usd=sz * cv * px,
                        source="okx",
                        fetched_at=fetched,
                        git_sha=env.git_sha,
                    )
                )
    return rows_to_df(LiquidationRow, rows)


def fetch_klines_1h(
    symbols: list[str], limit: int = 168, ts: datetime | None = None, force: bool = False
) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(
                "/api/v5/market/candles",
                params={"instId": s, "bar": "1H", "limit": min(limit, 300)},
            )
            for s in symbols
        ]

    return run_dataset(
        "okx",
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
        for k in rec.body["data"]:
            if k[8] != "1":
                continue
            rows.append(
                HourlyPriceRow(
                    ts=_ms(k[0]),
                    venue=VENUE,
                    symbol=sym,
                    base=sym.split("-")[0],
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
    return rows_to_df(HourlyPriceRow, rows)


def fetch_futures_marks(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "okx",
        "futures_marks",
        "hourly",
        lambda c: [
            c.get("/api/v5/public/mark-price", params={"instType": "FUTURES"}),
            c.get("/api/v5/public/instruments", params={"instType": "FUTURES"}),
        ],
        ts=ts,
        force=force,
    )


def parse_futures_marks(env: Envelope, index: dict[str, float] | None = None) -> pl.DataFrame:
    """Linear (USDT-settled) dated futures only, e.g. `BTC-USDT-261225`; index from perps when given."""
    fetched = datetime.fromisoformat(env.fetched_at)
    inst = {i["instId"]: i for i in env.records[1].body["data"]}
    index = index or {}
    rows = []
    for m in env.records[0].body["data"]:
        i = inst.get(m["instId"])
        if not i or i.get("ctType") != "linear" or not i.get("expTime") or "_" in m["instId"]:
            continue
        base = m["instId"].split("-")[0]
        rows.append(
            FuturesMarkRow(
                ts=_ms(m["ts"]),
                venue=VENUE,
                symbol=m["instId"],
                base=base,
                expiry=_ms(i["expTime"]),
                mark_price=float(m["markPx"]),
                index_price=index.get(base),
                settle_ccy=i.get("settleCcy", "USDT"),
                source="okx",
                fetched_at=fetched,
                git_sha=env.git_sha,
            )
        )
    return rows_to_df(FuturesMarkRow, rows)
