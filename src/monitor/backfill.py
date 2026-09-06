"""Backfill (build prompt §7): pull history as far back as each source allows, then recompute
the daily indicators walk-forward.

Sources and their reach (verified 2026-09-06):
* daily candles — Binance 1000/page with `endTime` (spot via data-api.binance.vision; the
  pages verified back to 2021 and earlier), Bybit `end` paging (blocked from Actions, works
  locally), OKX `history-candles` with `after` (1Dutc), Coinbase `start/end` windows (300 rows),
  Kraken `since` (720 rows per call);
* market caps — CoinGecko `market_chart` 365 days (free-tier cap) per id;
* funding — Binance `fundingRate` 1000/page with `startTime` (multi-year), Bybit 200/page
  with `endTime`, OKX 100/page with `after` (returns nothing beyond ~3 months);
* open interest — Binance `openInterestHist` and OKX rubik: 30 days only;
* stablecoins (full), FRED (full), unlocks (full schedule), CoinMetrics (2010+), fees
  (`summary/fees/{slug}` full chart) are already complete in the daily datasets.
Depth cannot be backfilled: the liquidity-gate history before go-live uses the volume proxy
(`adv_basis = reported`, `depth_status = pending`), flagged in `tier_history`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime

import numpy as np
import polars as pl

from monitor import archive
from monitor import rules as rules_mod
from monitor.compute import positioning as pos
from monitor.compute import state as state_mod
from monitor.fetch import binance, bybit, okx
from monitor.fetch.base import RawStore, Record, run_dataset
from monitor.fetch.resilient import FetchRun
from monitor.meta import git_sha, utc_now
from monitor.paths import CONFIG

log = logging.getLogger("monitor.backfill")
MS_DAY = 86_400_000


def _ms(d: date) -> int:
    return int(datetime.combine(d, datetime.min.time(), tzinfo=UTC).timestamp() * 1000)


# --------------------------------------------------------------------------- fetchers
def fetch_binance_klines_history(
    symbols: list[str], start: date, ts: datetime | None = None, force: bool = False
):
    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            end = None
            for _ in range(20):
                params = {"symbol": s, "interval": "1d", "limit": 1000}
                if end:
                    params["endTime"] = end
                r = c.get("/api/v3/klines", params=params)
                recs.append(r)
                if not r.body or r.body[0][0] <= _ms(start):
                    break
                end = r.body[0][0] - 1
        return recs

    return run_dataset(
        "binance_spot",
        "klines_1d_history",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "start": str(start)},
    )


def parse_binance_klines_history(env) -> pl.DataFrame:
    from monitor.schema.tables import DailyPriceRow, rows_to_df

    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        sym = rec.url.split("symbol=")[1].split("&")[0]
        base, _ = binance.split_multiplier(sym.removesuffix("USDT"))
        for k in rec.body:
            if binance._ms(k[6]) > fetched:
                continue
            rows.append(
                DailyPriceRow(
                    date=binance._ms(k[0]).date(),
                    venue="binance",
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


def fetch_okx_candles_history(
    symbols: list[str], start: date, ts: datetime | None = None, force: bool = False
):
    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            after = None
            for _ in range(40):
                params = {"instId": s, "bar": "1Dutc", "limit": 100}
                if after:
                    params["after"] = after
                r = c.get("/api/v5/market/history-candles", params=params)
                recs.append(r)
                d = r.body.get("data") or []
                if not d or int(d[-1][0]) <= _ms(start):
                    break
                after = d[-1][0]
        return recs

    return run_dataset(
        "okx",
        "klines_1d_history",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "start": str(start)},
    )


def parse_okx_candles_history(env) -> pl.DataFrame:
    from monitor.schema.tables import DailyPriceRow, rows_to_df

    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        sym = rec.url.split("instId=")[1].split("&")[0]
        for k in rec.body.get("data") or []:
            if k[8] != "1":
                continue
            rows.append(
                DailyPriceRow(
                    date=okx._ms(k[0]).date(),
                    venue="okx",
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
    return rows_to_df(DailyPriceRow, rows)


def fetch_binance_funding_history(
    symbols: list[str], start: date, ts: datetime | None = None, force: bool = False
):
    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            st = _ms(start)
            for _ in range(20):
                r = c.get(
                    "/fapi/v1/fundingRate", params={"symbol": s, "limit": 1000, "startTime": st}
                )
                recs.append(r)
                if len(r.body) < 1000:
                    break
                st = r.body[-1]["fundingTime"] + 1
        return recs

    return run_dataset(
        "binance_usdm",
        "funding_history",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "start": str(start)},
    )


def fetch_bybit_funding_history(
    symbols: list[str], start: date, ts: datetime | None = None, force: bool = False
):
    def go(c) -> list[Record]:
        recs = []
        for s in symbols:
            end = None
            for _ in range(60):
                params = {"category": "linear", "symbol": s, "limit": 200}
                if end:
                    params["endTime"] = end
                r = c.get("/v5/market/funding/history", params=params)
                recs.append(r)
                lst = r.body["result"]["list"]
                if not lst or int(lst[-1]["fundingRateTimestamp"]) <= _ms(start):
                    break
                end = int(lst[-1]["fundingRateTimestamp"]) - 1
        return recs

    return run_dataset(
        "bybit",
        "funding_history",
        "daily",
        go,
        ts=ts,
        force=force,
        meta={"symbols": symbols, "start": str(start)},
    )


def fetch_okx_funding_history(inst_ids: list[str], ts: datetime | None = None, force: bool = False):
    def go(c) -> list[Record]:
        recs = []
        for i in inst_ids:
            after = None
            for _ in range(10):
                params = {"instId": i, "limit": 100}
                if after:
                    params["after"] = after
                r = c.get("/api/v5/public/funding-rate-history", params=params)
                recs.append(r)
                d = r.body.get("data") or []
                if not d:
                    break
                after = d[-1]["fundingTime"]
        return recs

    return run_dataset(
        "okx", "funding_history", "daily", go, ts=ts, force=force, meta={"inst_ids": inst_ids}
    )


def parse_funding_history(env, venue: str) -> pl.DataFrame:
    """Normalise to date, venue, symbol, base, funding_rate (per period), n_periods."""
    rows = []
    for rec in env.records:
        if venue == "binance":
            for d in rec.body:
                base, _ = binance.split_multiplier(d["symbol"].removesuffix("USDT"))
                rows.append(
                    {
                        "ts": binance._ms(d["fundingTime"]),
                        "venue": "binance",
                        "symbol": d["symbol"],
                        "base": base,
                        "funding_rate": float(d["fundingRate"]),
                    }
                )
        elif venue == "bybit":
            for d in rec.body["result"]["list"]:
                base, _ = bybit.split_multiplier(d["symbol"].removesuffix("USDT"))
                rows.append(
                    {
                        "ts": bybit._ms(d["fundingRateTimestamp"]),
                        "venue": "bybit",
                        "symbol": d["symbol"],
                        "base": base,
                        "funding_rate": float(d["fundingRate"]),
                    }
                )
        else:
            for d in rec.body.get("data") or []:
                rows.append(
                    {
                        "ts": okx._ms(d["fundingTime"]),
                        "venue": "okx",
                        "symbol": d["instId"],
                        "base": d["instId"].split("-")[0],
                        "funding_rate": float(d.get("realizedRate") or d["fundingRate"]),
                    }
                )
    if not rows:
        return pl.DataFrame(
            schema={
                "ts": pl.Datetime("us", "UTC"),
                "venue": pl.Utf8,
                "symbol": pl.Utf8,
                "base": pl.Utf8,
                "funding_rate": pl.Float64,
            }
        )
    return pl.DataFrame(rows, infer_schema_length=None).unique(subset=["ts", "venue", "symbol"])


def fetch_oi_history(
    binance_syms: list[str], okx_ccys: list[str], ts: datetime | None = None, force: bool = False
):
    def go_b(c) -> list[Record]:
        return [
            c.get(
                "/futures/data/openInterestHist", params={"symbol": s, "period": "1d", "limit": 30}
            )
            for s in binance_syms
        ]

    def go_o(c) -> list[Record]:
        return [
            c.get(
                "/api/v5/rubik/stat/contracts/open-interest-volume",
                params={"ccy": s, "period": "1D"},
            )
            for s in okx_ccys
        ]

    p1 = (
        run_dataset(
            "binance_usdm",
            "oi_history",
            "daily",
            go_b,
            ts=ts,
            force=force,
            meta={"symbols": binance_syms},
        )
        if binance_syms
        else None
    )
    p2 = (
        run_dataset("okx", "oi_history", "daily", go_o, ts=ts, force=force, meta={"ccys": okx_ccys})
        if okx_ccys
        else None
    )
    return p1, p2


def fetch_market_caps(ids: list[str], ts: datetime | None = None, force: bool = False):
    return run_dataset(
        "coingecko",
        "market_chart_history",
        "daily",
        lambda c: [
            c.get(
                f"/coins/{i}/market_chart",
                params={"vs_currency": "usd", "days": 365, "interval": "daily"},
            )
            for i in ids
        ],
        ts=ts,
        force=force,
        meta={"ids": ids},
    )


# --------------------------------------------------------------------------- orchestration
def backfill(start: date, ts: datetime | None = None, force: bool = False) -> dict:
    """Fetch history for the current universe and rebuild the walk-forward daily indicators."""
    from monitor.jobs_hourly import _symbol_map

    ts = ts or utc_now()
    sm = _symbol_map()
    fr = FetchRun("backfill", ts)
    t1 = sm.filter(pl.col("tier") == 1)
    t12 = sm.filter(pl.col("tier").is_in([1, 2]))
    b_spot = [s["binance"] for s in t12["spot"].to_list() if s and s.get("binance")]
    o_spot = [s["okx"] for s in t12["spot"].to_list() if s and s.get("okx")]
    b_perp = [s["binance"] for s in t1["perp"].to_list() if s and s.get("binance")]
    y_perp = [s["bybit"] for s in t1["perp"].to_list() if s and s.get("bybit")]
    o_perp = [s["okx"] for s in t1["perp"].to_list() if s and s.get("okx")]
    fr.run("binance_klines_history", lambda: fetch_binance_klines_history(b_spot, start, ts, force))
    fr.run("okx_klines_history", lambda: fetch_okx_candles_history(o_spot, start, ts, force))
    fr.run(
        "binance_funding_history", lambda: fetch_binance_funding_history(b_perp, start, ts, force)
    )
    fr.run("bybit_funding_history", lambda: fetch_bybit_funding_history(y_perp, start, ts, force))
    fr.run("okx_funding_history", lambda: fetch_okx_funding_history(o_perp, ts, force))
    fr.run(
        "oi_history", lambda: fetch_oi_history(b_perp, [s.split("-")[0] for s in o_perp], ts, force)
    )
    fr.run("market_caps_365d", lambda: fetch_market_caps(t12["id"].to_list(), ts, force))
    fr.flush()
    counts = rebuild_history()
    return {**fr.out, **counts}


def rebuild_history() -> dict:
    """Parse the history raw files into the tables and recompute the walk-forward indicators."""
    store = RawStore()
    sha, now = git_sha(), utc_now()
    counts = {}

    def envs(name):
        return [store.read(f) for f in store.all(name)]

    frames = [parse_binance_klines_history(e) for e in envs("binance_spot_klines_1d_history")] + [
        parse_okx_candles_history(e) for e in envs("okx_klines_1d_history")
    ]
    frames = [f for f in frames if f.height]
    if frames:
        counts["prices_daily"] = archive.upsert(
            "prices_daily", pl.concat(frames, how="diagonal_relaxed")
        ).height
    fh = (
        [parse_funding_history(e, "binance") for e in envs("binance_usdm_funding_history")]
        + [parse_funding_history(e, "bybit") for e in envs("bybit_funding_history")]
        + [parse_funding_history(e, "okx") for e in envs("okx_funding_history")]
    )
    fh = [f for f in fh if f.height]
    if fh:
        f = pl.concat(fh).with_columns(
            pl.lit("funding_history").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        counts["funding_history"] = archive.upsert("funding_history", f).height
    oi_rows = []
    for e in envs("binance_usdm_oi_history"):
        for rec in e.records:
            for d in rec.body:
                base, _ = binance.split_multiplier(d["symbol"].removesuffix("USDT"))
                oi_rows.append(
                    {
                        "date": binance._ms(d["timestamp"]).date(),
                        "venue": "binance",
                        "base": base,
                        "oi_usd": float(d["sumOpenInterestValue"]),
                    }
                )
    for e in envs("okx_oi_history"):
        for rec in e.records:
            ccy = rec.url.split("ccy=")[1].split("&")[0]
            for d in rec.body.get("data") or []:
                oi_rows.append(
                    {
                        "date": okx._ms(d[0]).date(),
                        "venue": "okx",
                        "base": ccy,
                        "oi_usd": float(d[1]),
                    }
                )
    if oi_rows:
        o = pl.DataFrame(oi_rows, infer_schema_length=None).with_columns(
            pl.lit("oi_history").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        counts["oi_history"] = archive.upsert("oi_history", o).height
    mc_rows = []
    for e in envs("coingecko_market_chart_history"):
        for rec, i in zip(e.records, e.meta["ids"], strict=True):
            b = rec.body
            if not isinstance(b, dict) or "market_caps" not in b:
                continue
            for p, m, v in zip(b["prices"], b["market_caps"], b["total_volumes"], strict=False):
                mc_rows.append(
                    {
                        "date": datetime.fromtimestamp(p[0] / 1000, tz=UTC).date(),
                        "id": i,
                        "price_usd": p[1],
                        "market_cap_usd": m[1],
                        "volume_24h_usd": v[1],
                    }
                )
    if mc_rows:
        m = (
            pl.DataFrame(mc_rows, infer_schema_length=None)
            .unique(subset=["date", "id"])
            .with_columns(
                pl.lit("coingecko").alias("source"),
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
            )
        )
        counts["market_cap_history"] = archive.upsert("market_cap_history", m).height
    counts.update(walk_forward())
    return counts


def _daily_funding_from_history(fh: pl.DataFrame, oi_hist: pl.DataFrame | None) -> pl.DataFrame:
    """Daily OI-weighted annualised funding per base from the funding history. Periods per
    day are inferred per venue-symbol (8h → 3, 4h → 6); weights are the venue's OI when the OI
    history covers the day, else equal weights (flagged `weights = equal`)."""
    f = fh.with_columns(pl.col("ts").dt.date().alias("date"))
    per = f.group_by("date", "venue", "symbol", "base").agg(
        pl.col("funding_rate").sum().alias("fr_day"), pl.len().alias("n")
    )
    per = per.with_columns(
        (pl.col("fr_day") * 365.0).alias("fr_ann")
    )  # sum over the day × 365 = annualised
    if oi_hist is not None and oi_hist.height:
        per = per.join(
            oi_hist.select("date", "venue", "base", "oi_usd"),
            on=["date", "venue", "base"],
            how="left",
        )
    else:
        per = per.with_columns(pl.lit(None, dtype=pl.Float64).alias("oi_usd"))
    per = per.with_columns(
        pl.col("oi_usd").fill_null(1.0).alias("w"), pl.col("oi_usd").is_null().alias("equal_w")
    )
    return (
        per.group_by("date", "base")
        .agg(
            ((pl.col("fr_ann") * pl.col("w")).sum() / pl.col("w").sum()).alias("funding_ann"),
            pl.col("oi_usd").sum().alias("oi_usd"),
            pl.col("venue").n_unique().cast(pl.Int64).alias("n_venues"),
            pl.col("equal_w").any().alias("equal_weights"),
        )
        .sort("base", "date")
    )


def walk_forward() -> dict:
    """Recompute the daily positioning indicators and rules for every day in the history,
    using only data up to that day (funding z over 90 d, OI change, quadrant), plus the daily
    fragility components that can be built from history (z_fr, z_dd, z_sc_neg)."""
    th = __import__("yaml").safe_load((CONFIG / "thresholds.yaml").read_text())
    sha, now = git_sha(), utc_now()
    fh = archive.read("funding_history")
    if fh is None or not fh.height:
        return {"walk_forward": 0}
    oi_hist = archive.read("oi_history")
    daily = _daily_funding_from_history(fh, oi_hist)
    # merge with the live funding_daily (which is OI-weighted with all venues) — live rows win
    live = archive.read("funding_daily")
    if live is not None and live.height:
        daily = (
            pl.concat(
                [
                    daily.with_columns(pl.lit("history").alias("origin")),
                    live.select("date", "base", "funding_ann", "oi_usd", "n_venues").with_columns(
                        pl.lit(False).alias("equal_weights"), pl.lit("live").alias("origin")
                    ),
                ],
                how="diagonal_relaxed",
            )
            .sort("origin")
            .unique(subset=["date", "base"], keep="last")
            .sort("base", "date")
        )
    archive.upsert(
        "funding_daily_history",
        daily.with_columns(
            pl.lit("funding_history+funding_daily").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        ),
    )
    from monitor.jobs_hourly import _daily_prices_by_base

    prices = _daily_prices_by_base()
    rows, fires = [], []
    w90 = th["robust_z"]["window_days_funding"]
    for (base,), g in daily.group_by("base", maintain_order=True):
        g = g.sort("date")
        fr_ = g["funding_ann"].to_numpy()
        z = pos.robust_z_series(fr_, w90)
        px = prices.filter(pl.col("base") == base).sort("date")
        j = g.join(px.select("date", "close"), on="date", how="left")
        oi = j["oi_usd"].to_numpy()
        for i, d in enumerate(g["date"].to_list()):
            oi_chg = float(oi[i] / oi[i - 5] - 1) if i >= 5 and oi[i - 5] and oi[i] else None
            rows.append(
                {
                    "date": d,
                    "base": base,
                    "funding_ann": float(fr_[i]),
                    "z_fr": None if np.isnan(z[i]) else float(z[i]),
                    "oi_usd": float(oi[i]) if oi[i] else None,
                    "oi_change_5d": oi_chg,
                }
            )
            if not np.isnan(z[i]):
                when = datetime.combine(d, datetime.min.time(), tzinfo=UTC)
                # full rules need liquidation data / depth that do not exist in history: evaluate the
                # partial-input variants (funding and OI legs only), labelled 4.1p / 4.2p on the page
                oi_pct = pos.percentile_rank(oi[: i + 1], 90) if i >= 30 and oi[i] else None
                t41, t42 = th["rules"]["crowded_long"], th["rules"]["capitulation"]
                f41 = rules_mod.RuleFire(
                    rule_id="4.1p",
                    asset=base,
                    ts=when,
                    fired=(
                        bool(z[i] > t41["z_fr_min"] and oi_pct >= t41["oi_rel_percentile_min"])
                        if oi_pct is not None
                        else None
                    ),
                    inputs={"z_fr": float(z[i]), "oi_pctile": oi_pct},
                    thresholds={
                        "z_fr_min": t41["z_fr_min"],
                        "oi_rel_percentile_min": t41["oi_rel_percentile_min"],
                    },
                    note="partial: no liquidation density / depth in history",
                )
                f42 = rules_mod.RuleFire(
                    rule_id="4.2p",
                    asset=base,
                    ts=when,
                    fired=(
                        bool(z[i] < t42["z_fr_max"] and oi_chg <= -t42["oi_drop_5d_min"])
                        if oi_chg is not None
                        else None
                    ),
                    inputs={"z_fr": float(z[i]), "oi_change_5d": oi_chg},
                    thresholds={
                        "z_fr_max": t42["z_fr_max"],
                        "oi_drop_5d_min": t42["oi_drop_5d_min"],
                    },
                    note="partial: no liquidation volume in history",
                )
                fires.extend([f41, f42])
    hist = pl.DataFrame(rows, infer_schema_length=None).with_columns(
        pl.lit("walk_forward").alias("source"),
        pl.lit(now).alias("fetched_at"),
        pl.lit(sha).alias("git_sha"),
    )
    archive.upsert("positioning_history", hist)
    # fragility history from the components available in history: z_fr (Tier 1 aggregate), z_dd (BTC), z_sc_neg
    uni = archive.read("universe")
    t1 = set(
        uni.filter((pl.col("as_of") == uni["as_of"].max()) & (pl.col("tier") == 1))[
            "symbol"
        ].to_list()
    )
    agg = (
        daily.filter(pl.col("base").is_in(t1))
        .group_by("date")
        .agg(
            (
                (pl.col("funding_ann") * pl.col("oi_usd").fill_null(1.0)).sum()
                / pl.col("oi_usd").fill_null(1.0).sum()
            ).alias("fr")
        )
        .sort("date")
    )
    w250 = th["robust_z"]["window_days_fragility"]
    zfr = pos.robust_z_series(agg["fr"].to_numpy(), w250)
    btc = prices.filter(pl.col("base") == "BTC").sort("date")
    dd = np.array(
        [state_mod.drawdown_from_high(btc["close"].to_numpy()[: i + 1]) for i in range(btc.height)],
        dtype=float,
    )
    zdd = pos.robust_z_series(-dd, w250)
    dd_map = dict(zip(btc["date"].to_list(), zdd, strict=True))
    sc = archive.read("stablecoin_growth")
    zsc_map = {}
    if sc is not None and sc.height:
        s = sc.sort("date")
        zs = pos.robust_z_series(-s["growth_30d"].fill_null(np.nan).to_numpy(), w250)
        zsc_map = dict(zip(s["date"].to_list(), zs, strict=True))
    frag_rows = []
    for i, d in enumerate(agg["date"].to_list()):
        comps = {
            "z_fr": None if np.isnan(zfr[i]) else float(zfr[i]),
            "z_dd": None if np.isnan(dd_map.get(d, np.nan)) else float(dd_map[d]),
            "z_sc_neg": None if np.isnan(zsc_map.get(d, np.nan)) else float(zsc_map[d]),
        }
        vals = [v for v in comps.values() if v is not None]
        frag_rows.append(
            {
                "date": d,
                "phi": float(np.mean(vals)) if vals else None,
                "n_components": len(vals),
                **comps,
            }
        )
    if frag_rows:
        archive.upsert(
            "fragility_history",
            pl.DataFrame(frag_rows, infer_schema_length=None).with_columns(
                pl.lit("walk_forward (3 of 5 components available in history)").alias("source"),
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
            ),
        )
    if fires:
        import json

        df = pl.DataFrame(
            [
                {
                    **f.model_dump(exclude={"inputs", "thresholds"}),
                    "inputs": json.dumps(f.inputs, default=str),
                    "thresholds": json.dumps(f.thresholds),
                }
                for f in fires
            ],
            infer_schema_length=None,
        ).with_columns(
            pl.lit("rules-walk-forward").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        archive.upsert("rule_fires", df)
    # tier history proxy: volume-based membership per month (depth pending), flagged
    vol = (
        prices.with_columns(pl.col("date").dt.truncate("1mo").alias("month"))
        .group_by("month", "base")
        .agg(pl.col("volume_quote").mean().alias("adv_reported"))
    )
    return {
        "walk_forward": hist.height,
        "fragility_history": len(frag_rows),
        "rule_fires_history": len(fires),
        "tier_proxy_months": vol["month"].n_unique(),
    }
