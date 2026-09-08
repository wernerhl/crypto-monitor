"""Hourly job (build prompt §6): funding, OI, order books, trades, liquidations, options for
Tier 1 (books and trades also for Tier 2) → positioning, liquidity, fragility, rules → JSON
for the site. Target runtime < 6 minutes; the job aborts with a scope message after 10."""

from __future__ import annotations

import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from monitor import archive
from monitor import rules as rules_mod
from monitor.compute import liquidity as liq
from monitor.compute import positioning as pos
from monitor.compute import state as state_mod
from monitor.fetch import binance, bybit, coinbase, deribit, kraken, okx
from monitor.fetch.base import RawStore
from monitor.fetch.resilient import FetchRun
from monitor.meta import dump_json, git_sha, utc_now
from monitor.paths import CONFIG, SITE_DATA

log = logging.getLogger("monitor.hourly")
MAJORS = ("BTC", "ETH")


def _cfg(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def _current_universe() -> pl.DataFrame:
    uni = archive.read("universe")
    if uni is None or not uni.height:
        raise RuntimeError("universe not computed; run the daily job first")
    return uni.filter(pl.col("as_of") == uni["as_of"].max())


def _symbol_map() -> pl.DataFrame:
    """Venue symbols for the current universe (same resolver as the daily job)."""
    from monitor.compute import universe as uni_mod

    uni = _current_universe()
    markets = archive.read("markets")
    day = markets.filter(pl.col("as_of") == markets["as_of"].max())
    allv = archive.read("venue_listings")
    listings = allv.join(
        allv.group_by("venue", "source").agg(pl.col("ts").max()), on=["venue", "source", "ts"]
    )
    cfg = _cfg("universe.yaml")
    sm = uni_mod.resolve_symbols(
        day.filter(pl.col("id").is_in(uni["id"])), listings, cfg.get("symbol_map") or {}
    )
    return sm.join(uni.select("id", "symbol", "tier"), on="id")


def _syms(sm: pl.DataFrame, tiers: tuple[int, ...], market: str, venue: str) -> list[str]:
    return [
        s[venue]
        for s, t in zip(sm[market].to_list(), sm["tier"].to_list(), strict=True)
        if t in tiers and s and s.get(venue)
    ]


# --------------------------------------------------------------------------- fetch
def fetch_hourly(
    ts: datetime | None = None, force: bool = False, budget_s: float = 600.0
) -> dict[str, str]:
    t0 = time.monotonic()
    ts = ts or utc_now()
    sm = _symbol_map()
    out: dict[str, str] = {}

    def check():
        if time.monotonic() - t0 > budget_s:
            raise RuntimeError(
                "hourly fetch exceeded 10 minutes: reduce scope (fewer Tier 2 books or trades) — see docs/runbook.md"
            )

    # derivatives, Tier 1
    fr = FetchRun("hourly", ts)
    b1 = _syms(sm, (1,), "perp", "binance")
    fr.run("binance_perps", lambda: binance.fetch_perps(b1, ts=ts, force=force, freq="hourly"))
    fr.run(
        "bybit_perps",
        lambda: bybit.fetch_perps(
            ts=ts, force=force, freq="hourly", symbols=_syms(sm, (1, 2), "perp", "bybit")
        ),
    )
    fr.run(
        "okx_perps",
        lambda: okx.fetch_perps(
            ts=ts, force=force, freq="hourly", symbols=_syms(sm, (1, 2), "perp", "okx")
        ),
    )
    fr.run(
        "okx_funding", lambda: okx.fetch_funding(_syms(sm, (1,), "perp", "okx"), ts=ts, force=force)
    )
    fr.run("binance_long_short", lambda: binance.fetch_long_short(b1, ts=ts, force=force))
    fr.run("binance_coinm", lambda: binance.fetch_coinm_marks(ts=ts, force=force))
    fr.run("okx_futures", lambda: okx.fetch_futures_marks(ts=ts, force=force))
    fr.run(
        "okx_oi_rubik_1h",
        lambda: okx.fetch_oi_rubik(
            [s.split("-")[0] for s in _syms(sm, (1, 2), "perp", "okx")],
            period="1H",
            ts=ts,
            force=force,
            freq="hourly",
        ),
    )
    check()
    # liquidations (OKX, mode a): page back to the previous hour's fetch
    prev = archive.read("liquidations")
    since = (
        int(prev["ts"].max().timestamp() * 1000)
        if prev is not None and prev.height
        else int((ts - timedelta(hours=6)).timestamp() * 1000)
    )
    ulys = [s.replace("-SWAP", "") for s in _syms(sm, (1,), "perp", "okx")]
    fr.run(
        "okx_liquidations", lambda: okx.fetch_liquidations(ulys, since_ms=since, ts=ts, force=force)
    )
    # options
    fr.run("deribit_options", lambda: deribit.fetch_options(ts=ts, force=force))
    check()
    # order books, Tier 1 + 2, five venues
    for venue, mod in (
        ("binance", binance),
        ("bybit", bybit),
        ("okx", okx),
        ("coinbase", coinbase),
        ("kraken", kraken),
    ):
        syms = _syms(sm, (1,), "spot", venue)  # Tier 2 books are taken once a day (daily job)
        if syms:
            fr.run(
                f"{venue}_books",
                lambda mod=mod, syms=syms: mod.fetch_books(syms, ts=ts, force=force),
            )
        check()
    # recent trades, Tier 1 only
    for venue, mod in (
        ("binance", binance),
        ("okx", okx),
        ("coinbase", coinbase),
        ("kraken", kraken),
    ):
        syms = _syms(sm, (1,), "spot", venue)
        if syms:
            fr.run(
                f"{venue}_trades",
                lambda mod=mod, syms=syms: mod.fetch_trades(syms, ts=ts, force=force),
            )
    bsy = _syms(sm, (1,), "perp", "bybit")
    if bsy:
        fr.run("bybit_trades", lambda: bybit.fetch_trades(bsy, ts=ts, force=force))
    check()
    fr.flush()
    out.update(fr.out)
    out["_elapsed_s"] = f"{time.monotonic() - t0:.1f}"
    return out


def fetch_hourly_klines(ts: datetime | None = None, force: bool = False) -> dict[str, str]:
    """Daily dataset: 7 days of hourly bars for Tier 1+2 on every venue (wash-filter input),
    plus one Tier 2 order-book snapshot per venue (the liquidity gate for Tier 2)."""
    ts = ts or utc_now()
    sm = _symbol_map()
    out = {}
    fr = FetchRun("daily-hourly-klines", ts)
    for venue, mod in (
        ("binance", binance),
        ("bybit", bybit),
        ("okx", okx),
        ("coinbase", coinbase),
        ("kraken", kraken),
    ):
        syms = _syms(sm, (2,), "spot", venue)
        if syms:
            fr.run(
                f"{venue}_books_t2",
                lambda mod=mod, syms=syms: mod.fetch_books(syms, ts=ts, force=force, freq="daily"),
            )
    for venue, mod in (("binance", binance), ("bybit", bybit), ("okx", okx)):
        syms = _syms(sm, (1, 2), "spot", venue)
        if syms:
            fr.run(
                f"{venue}_klines_1h",
                lambda mod=mod, syms=syms: mod.fetch_klines_1h(syms, limit=168, ts=ts, force=force),
            )
    for venue, mod in (("coinbase", coinbase), ("kraken", kraken)):
        syms = _syms(sm, (1, 2), "spot", venue)
        if syms:
            fr.run(
                f"{venue}_klines_1h",
                lambda mod=mod, syms=syms: mod.fetch_klines_1h(syms, ts=ts, force=force),
            )
    fr.flush()
    out.update(fr.out)
    return out


# --------------------------------------------------------------------------- compute
def _envs(store: RawStore, name: str, rebuild: bool):
    files = store.all(name) if rebuild else ([store.latest(name)] if store.latest(name) else [])
    return [store.read(f) for f in files if f]


def compute_hourly(rebuild: bool = False) -> dict[str, int]:
    """Parse the hourly raw files into tables, then recompute the derived tables."""
    store = RawStore()
    counts: dict[str, int] = {}
    E = lambda name: _envs(store, name, rebuild)  # noqa: E731

    def put(table: str, frames: list[pl.DataFrame]) -> None:
        frames = [f for f in frames if f is not None and f.height]
        if frames:
            counts[table] = archive.upsert(table, pl.concat(frames, how="diagonal_relaxed")).height

    # perps (hourly buckets) — OKX funding merged in
    okx_fund = [okx.parse_funding(e) for e in E("okx_funding")]
    fund = (
        pl.concat(okx_fund, how="diagonal_relaxed").unique(subset=["symbol"], keep="last")
        if okx_fund
        else None
    )
    okx_p = []
    for e in E("okx_perps"):
        df = okx.parse_perps(e)
        if fund is not None:
            df = df.drop("funding_rate", "funding_interval_h", "next_funding_time").join(
                fund, on="symbol", how="left"
            )
        okx_p.append(df)
    put(
        "perp_snapshot",
        [binance.parse_perps(e) for e in E("binance_usdm_perps")]
        + [bybit.parse_perps(e) for e in E("bybit_perps")]
        + okx_p,
    )
    put("long_short", [binance.parse_long_short(e) for e in E("binance_usdm_long_short")])
    put(
        "oi_rubik",
        [okx.parse_oi_rubik(e) for e in E("okx_oi_rubik_1H")]
        + [okx.parse_oi_rubik(e) for e in E("okx_oi_rubik_1D")],
    )
    # dated futures
    listings = archive.read("venue_listings")
    fut = (
        [binance.parse_futures_marks(e, listings) for e in E("binance_usdm_perps")]
        if listings is not None
        else []
    )
    fut += [binance.parse_coinm_marks(e) for e in E("binance_coinm_marks")]
    fut += [bybit.parse_futures_marks(e) for e in E("bybit_perps")]
    perps_now = archive.read("perp_snapshot")
    idx = {}
    if perps_now is not None and perps_now.height:
        last = (
            perps_now.filter(pl.col("venue") == "okx")
            .sort("ts")
            .group_by("base")
            .agg(pl.col("mark_price").last())
        )
        idx = dict(zip(last["base"], last["mark_price"], strict=True))
    fut += [okx.parse_futures_marks(e, idx) for e in E("okx_futures_marks")]
    # liquidations with contract values
    cv = {}
    if listings is not None:
        ok = listings.filter((pl.col("venue") == "okx") & (pl.col("market") == "perp"))
        cv = dict(zip(ok["symbol"], ok["multiplier"], strict=True))
    put(
        "liquidations",
        [okx.parse_liquidations(e, cv) for e in E("okx_liquidations")]
        + [binance.parse_liquidations_ws(e) for e in E("binance_liquidations_ws")]
        + [bybit.parse_liquidations_ws(e) for e in E("bybit_liquidations_ws")],
    )
    # options
    opt_frames, dv_frames = [], []
    for e in E("deribit_options"):
        o, f, d = deribit.parse_options(e)
        opt_frames.append(o)
        fut.append(f)
        dv_frames.append(d)
    put("options", opt_frames)
    put("dvol", dv_frames)
    put("futures_marks", fut)
    # books and trades
    put(
        "orderbook_depth",
        [binance.parse_books(e) for e in E("binance_spot_books")]
        + [bybit.parse_books(e) for e in E("bybit_books")]
        + [okx.parse_books(e) for e in E("okx_books")]
        + [coinbase.parse_books(e) for e in E("coinbase_books")]
        + [kraken.parse_books(e) for e in E("kraken_books")],
    )
    put(
        "trade_stats",
        [binance.parse_trades(e) for e in E("binance_spot_trades")]
        + [bybit.parse_trades(e) for e in E("bybit_trades")]
        + [okx.parse_trades(e) for e in E("okx_trades")]
        + [coinbase.parse_trades(e) for e in E("coinbase_trades")]
        + [kraken.parse_trades(e) for e in E("kraken_trades")],
    )
    put(
        "prices_hourly",
        [binance.parse_klines_1h(e) for e in E("binance_spot_klines_1h")]
        + [bybit.parse_klines_1h(e) for e in E("bybit_klines_1h")]
        + [okx.parse_klines_1h(e) for e in E("okx_klines_1h")]
        + [coinbase.parse_klines_1h(e) for e in E("coinbase_klines_1h")]
        + [kraken.parse_klines_1h(e) for e in E("kraken_klines_1h")],
    )
    counts.update(compute_derived())
    return counts


# --------------------------------------------------------------------------- derived tables
def _daily_prices_by_base() -> pl.DataFrame:
    """Daily close (Binance preferred, else the median across venues) and Σ quote volume per base."""
    p = archive.read("prices_daily")
    if p is None:
        return pl.DataFrame(
            schema={
                "date": pl.Date,
                "base": pl.Utf8,
                "close": pl.Float64,
                "volume_quote": pl.Float64,
            }
        )
    p = p.with_columns(
        pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias("vq")
    )
    close = p.group_by("date", "base").agg(
        pl.col("close").filter(pl.col("venue") == "binance").first().alias("c_b"),
        pl.col("close").median().alias("c_m"),
    )
    vol = p.group_by("date", "base").agg(pl.col("vq").sum().alias("volume_quote"))
    return (
        close.join(vol, on=["date", "base"])
        .with_columns(pl.coalesce(pl.col("c_b"), pl.col("c_m")).alias("close"))
        .drop("c_b", "c_m")
        .sort("base", "date")
    )


def oi_daily_grid() -> pl.DataFrame:
    """A4: one OKX-rubik OI value per (date, base) — the 1D series for closed days, today's last
    1H point for the current day. Suspect jumps (|Δlog| > 0.4) are kept but flagged."""
    r = archive.read("oi_rubik")
    schema = {
        "date": pl.Date,
        "base": pl.Utf8,
        "oi_usd": pl.Float64,
        "suspect": pl.Boolean,
        "source": pl.Utf8,
    }
    if r is None or not r.height:
        return pl.DataFrame(schema=schema)
    d1 = (
        r.filter(pl.col("period") == "1D")
        .with_columns(pl.col("ts").dt.date().alias("date"))
        .sort("ts")
        .unique(subset=["date", "base"], keep="last")
    )
    h1 = (
        r.filter(pl.col("period") == "1H")
        .with_columns(pl.col("ts").dt.date().alias("date"))
        .sort("ts")
        .unique(subset=["date", "base"], keep="last")
    )
    last_daily = d1.group_by("base").agg(pl.col("date").max().alias("dmax"))
    h1 = (
        h1.join(last_daily, on="base", how="left")
        .filter(pl.col("dmax").is_null() | (pl.col("date") > pl.col("dmax")))
        .drop("dmax")
    )
    out = pl.concat(
        [
            d1.select("date", "base", "oi_usd", "suspect").with_columns(
                pl.lit("okx_rubik_1D").alias("source")
            ),
            h1.select("date", "base", "oi_usd", "suspect").with_columns(
                pl.lit("okx_rubik_1H (today)").alias("source")
            ),
        ],
        how="vertical_relaxed",
    )
    now = utc_now()
    archive.upsert(
        "oi_daily",
        out.with_columns(pl.lit(now).alias("fetched_at"), pl.lit(git_sha()).alias("git_sha")),
    )
    return out.sort("base", "date")


def compute_derived(as_of: date | None = None) -> dict[str, int]:
    th = _cfg("thresholds.yaml")
    sha, now = git_sha(), utc_now()
    counts: dict[str, int] = {}
    uni = _current_universe()
    tier1 = uni.filter(pl.col("tier") == 1)
    mcap = dict(zip(uni["symbol"], uni["market_cap_usd"], strict=True))
    perps = archive.read("perp_snapshot")
    prices = _daily_prices_by_base()
    as_of = as_of or (perps["ts"].max().date() if perps is not None else now.date())

    # ---- funding / OI daily series per base
    agg = pos.aggregate_funding(perps) if perps is not None else None
    daily = pos.daily_from_snapshots(agg) if agg is not None else None
    if daily is not None and daily.height:
        archive.upsert(
            "funding_daily",
            daily.with_columns(
                pl.lit("perp_snapshot").alias("source"),
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
            ),
        )
    fd = archive.read("funding_daily")
    # the backfill merges venue funding history with the live table (history rows first,
    # live rows win on overlap); use it so z-scores and percentiles have their windows
    fdh = archive.read("funding_daily_history")
    if fdh is not None and fdh.height:
        cols = ["date", "base", "funding_ann", "oi_usd", "oi_usd_okx", "n_venues"]
        live = fd.select(cols) if fd is not None else None
        fd = fdh.select(cols)
        if live is not None:
            fd = (
                pl.concat([fd, live], how="diagonal_relaxed")
                .unique(subset=["date", "base"], keep="last")
                .sort("base", "date")
            )
        # A5: funding_daily holds the full daily-grid series (history + live), not just live days
        archive.upsert(
            "funding_daily",
            fd.with_columns(
                pl.lit("funding_history+perp_snapshot").alias("source"),
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
            ),
        )
    oi_grid = oi_daily_grid()
    if oi_grid.height:
        # the OI statistics use the rubik grid; funding weights keep their own OI
        fd = fd.drop("oi_usd_okx").join(
            oi_grid.select("date", "base", pl.col("oi_usd").alias("oi_usd_okx")),
            on=["date", "base"],
            how="left",
        )

    # ---- liquidity: depth aggregate, wash filters, real ADV
    depth = archive.read("orderbook_depth")
    dep_day = liq.aggregate_depth(depth) if depth is not None else None
    hourly = archive.read("prices_hourly")
    corr = liq.volume_absret_corr(hourly) if hourly is not None else None
    tstats = archive.read("trade_stats")
    wash = _wash_table(depth, tstats, corr, as_of)
    if wash is not None and wash.height:
        archive.upsert(
            "wash_filters",
            wash.with_columns(
                pl.lit("orderbook_depth+trade_stats+prices_hourly").alias("source"),
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
            ),
        )
        counts["wash_filters"] = wash.height
    venue_vol = _daily_venue_volume()
    adv = (
        liq.real_adv(
            venue_vol,
            wash
            if wash is not None
            else pl.DataFrame(
                schema={"date": pl.Date, "base": pl.Utf8, "venue": pl.Utf8, "pass": pl.Boolean}
            ),
            as_of,
        )
        if venue_vol.height
        else None
    )
    liq_rows = _liquidity_table(uni, dep_day, adv, prices, th, as_of, now, sha)
    if liq_rows.height:
        archive.upsert("liquidity", liq_rows)
        counts["liquidity"] = liq_rows.height

    # ---- positioning per Tier 1 base
    pos_rows, fires = [], []
    long_share = archive.read("long_short")
    liqs = archive.read("liquidations")
    lev = {
        float(k): float(v) for k, v in th["positioning"]["leverage_distribution"]["default"].items()
    }
    for base in tier1["symbol"].to_list():
        row = _positioning_row(
            base, fd, prices, mcap.get(base), depth, dep_day, long_share, liqs, lev, th, as_of, now
        )
        if row:
            pos_rows.append(row)
            f = rules_mod.crowded_long(
                base,
                now,
                row["z_fr"],
                row["oi_rel_pctile"],
                row["lambda_minus_2"],
                row["depth_2pct_usd"],
                th["rules"],
            )
            fires.append(f)
            fires.append(
                rules_mod.capitulation(
                    base,
                    now,
                    row["z_fr"],
                    row["oi_change_5d"],
                    row["long_liq_24h_pctile"],
                    th["rules"],
                )
            )
    # ---- options + VRP for the majors
    opt_rows = []
    opts = archive.read("options")
    for cur in MAJORS:
        om = _options_row(cur, opts, prices, now)
        if om:
            opt_rows.append(om)
    # ---- fragility
    frag = _fragility_row(fd, prices, opt_rows, mcap, tier1, th, as_of, now)
    phi = frag.get("phi") if frag else None
    for om in opt_rows:
        fires.append(
            rules_mod.vol_underpricing(om["currency"], now, om.get("vrp"), phi, th["rules"])
        )
    if pos_rows:
        df = pl.DataFrame(pos_rows).with_columns(pl.lit(sha).alias("git_sha"))
        archive.upsert("positioning", df)
        counts["positioning"] = df.height
    if opt_rows:
        df = pl.DataFrame(
            [
                {k: (json.dumps(v, default=str) if k == "largest_oi" else v) for k, v in r.items()}
                for r in opt_rows
            ]
        ).with_columns(pl.lit(sha).alias("git_sha"))
        archive.upsert("options_metrics", df)
        counts["options_metrics"] = df.height
    if frag:
        df = pl.DataFrame([frag]).with_columns(pl.lit(sha).alias("git_sha"))
        archive.upsert("fragility", df)
        counts["fragility"] = 1
    if fires:
        df = pl.DataFrame(
            [
                {
                    **f.model_dump(exclude={"inputs", "thresholds"}),
                    "inputs": json.dumps(f.inputs, default=str),
                    "thresholds": json.dumps(f.thresholds),
                }
                for f in fires
            ]
        ).with_columns(
            pl.lit("rules").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        archive.upsert("rule_fires", df)
        counts["rule_fires"] = df.height
    # ---- vol state (needs ≥ 250 daily returns of the index proxy = BTC)
    vs = _vol_state(prices, now, sha)
    if vs:
        counts["vol_state"] = 1
    write_hourly_json()
    return counts


def _daily_venue_volume() -> pl.DataFrame:
    p = archive.read("prices_daily")
    if p is None:
        return pl.DataFrame(
            schema={"date": pl.Date, "base": pl.Utf8, "venue": pl.Utf8, "volume_quote": pl.Float64}
        )
    return (
        p.with_columns(
            pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias(
                "volume_quote"
            )
        )
        .group_by("date", "base", "venue")
        .agg(pl.col("volume_quote").sum())
    )


def _wash_table(depth, tstats, corr, as_of: date) -> pl.DataFrame | None:
    if depth is None or not depth.height:
        return None
    d = (
        depth.with_columns(pl.col("ts").dt.date().alias("date"))
        .group_by("date", "base", "venue")
        .agg(
            pl.col("depth_usd").median().alias("depth_usd"),
            pl.col("truncated").any().alias("truncated"),
        )
    )
    vol = _daily_venue_volume()
    v = d.join(vol, on=["date", "base", "venue"], how="left")
    # a day without a closed daily candle yet (today) uses the last closed day's volume
    last_vol = (
        vol.sort("date")
        .group_by("base", "venue")
        .agg(pl.col("volume_quote").last().alias("vq_last"))
    )
    v = (
        v.join(last_vol, on=["base", "venue"], how="left")
        .with_columns(pl.coalesce(pl.col("volume_quote"), pl.col("vq_last")).alias("volume_quote"))
        .drop("vq_last")
    )
    if tstats is not None and tstats.height:
        t = (
            tstats.with_columns(
                pl.col("ts").dt.date().alias("date"),
                (pl.col("benford_chi2") / pl.col("n_trades")).alias("dev"),
            )
            .group_by("date", "base", "venue")
            .agg(pl.col("dev").median().alias("benford_dev"))
        )
        v = v.join(t, on=["date", "base", "venue"], how="left")
    else:
        v = v.with_columns(pl.lit(None, dtype=pl.Float64).alias("benford_dev"))
    if corr is not None and corr.height:
        v = v.join(
            corr.select("venue", "base", "vol_absret_corr"), on=["venue", "base"], how="left"
        )
    else:
        v = v.with_columns(pl.lit(None, dtype=pl.Float64).alias("vol_absret_corr"))
    return liq.wash_filters(v)


def _liquidity_table(uni, dep_day, adv, prices, th, as_of, now, sha) -> pl.DataFrame:
    rows = []
    lcfg = {**th["rules"]["gate"], **th["liquidity"]}
    book = _cfg("book.yaml")
    q_by_id = {p["asset"]: abs(p["weight"]) * book["nav_usd"] for p in book["positions"]}
    dd = (
        {r["base"]: r for r in dep_day.to_dicts()} if dep_day is not None and dep_day.height else {}
    )
    ad = {r["base"]: r for r in adv.to_dicts()} if adv is not None and adv.height else {}
    ami = {r["base"]: r for r in liq.amihud(prices).to_dicts()} if prices.height else {}
    for r in uni.filter(pl.col("tier").is_in([1, 2])).to_dicts():
        b = r["symbol"]
        latest_dd = max(
            (v for v in dd.values() if v["base"] == b), key=lambda x: x["date"], default=None
        )
        a = ad.get(b, {})
        sigma = _sigma_daily(prices, b)
        adv_real = a.get("adv_real_usd")
        q = q_by_id.get(
            r["id"], 0.01 * book["nav_usd"]
        )  # 1 % NAV reference size when not in the book
        g = liq.gate(q, adv_real or 0.0, sigma or 0.0, 10.0, None, lcfg) if adv_real else None
        rows.append(
            {
                "date": as_of,
                "id": r["id"],
                "base": b,
                "tier": r["tier"],
                "depth_2pct_usd": latest_dd["depth_2pct_usd"] if latest_dd else None,
                "depth_lower_bound": latest_dd["depth_lower_bound"] if latest_dd else None,
                "depth_date": latest_dd["date"] if latest_dd else None,
                "depth_venues": ",".join(latest_dd["venues"]) if latest_dd else None,
                "adv_reported_usd": a.get("adv_reported_usd"),
                "adv_real_usd": adv_real,
                "adv_days": a.get("n_days"),
                "venues_passing": ",".join(a.get("venues_passing") or []) if a else None,
                "amihud": ami.get(b, {}).get("amihud"),
                "sigma_daily": sigma,
                "reference_q_usd": q,
                "dtl_days": g["dtl_days"] if g else None,
                "impact_cost": g["impact_cost"] if g else None,
                "gate_pass": g["pass"] if g else None,
                "gate_note": None if g else "no real ADV yet",
                "source": "orderbook_depth+prices_daily+wash_filters",
                "fetched_at": now,
                "git_sha": sha,
            }
        )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def _sigma_daily(prices: pl.DataFrame, base: str, window: int = 30) -> float | None:
    c = prices.filter(pl.col("base") == base).sort("date")["close"].to_numpy()
    if c.size < window + 1:
        return None
    r = np.diff(np.log(c[-window - 1 :]))
    return float(np.std(r, ddof=1))


def _positioning_row(
    base, fd, prices, mcap_usd, depth, dep_day, long_share, liqs, lev, th, as_of, now
) -> dict | None:
    if fd is None or not fd.height:
        return None
    f = fd.filter(pl.col("base") == base).sort("date")
    if not f.height:
        return None
    z_fr, n_fr = pos.robust_z(f["funding_ann"].to_numpy(), th["robust_z"]["window_days_funding"])
    oi_total = f["oi_usd"].to_numpy()
    # OI statistics on the OKX series (continuous history, reachable from every runner)
    oi = f["oi_usd_okx"].to_numpy() if "oi_usd_okx" in f.columns else oi_total
    oi_rel = pos.oi_relative(float(oi_total[-1]) if oi_total[-1] else None, mcap_usd)
    # OI^rel history needs daily market caps; use the current cap for the trailing window (flagged)
    oi_ok = np.array([v if v else np.nan for v in oi], dtype=float)
    oi_rel_hist = oi_ok / mcap_usd if mcap_usd else np.array([])
    pct = pos.percentile_rank(oi_rel_hist, 90) if np.isfinite(oi_rel_hist).sum() >= 30 else None
    px = prices.filter(pl.col("base") == base).sort("date")
    joined = (
        px.join(f.select("date", pl.col("oi_usd_okx").alias("oi_usd")), on="date", how="inner")
        .drop_nulls("oi_usd")
        .sort("date")
    )
    quad = (
        pos.oi_price_quadrant(joined["close"].to_numpy(), joined["oi_usd"].to_numpy())
        if joined.height > 5
        else {"dlogp": None, "dlogoi": None, "quadrant": None, "slope_20d": None}
    )
    oi_change_5d = (
        float(oi_ok[-1] / oi_ok[-6] - 1)
        if oi_ok.size >= 6 and np.isfinite(oi_ok[-6]) and oi_ok[-6] > 0 and np.isfinite(oi_ok[-1])
        else None
    )
    sigma = _sigma_daily(prices, base)
    ls = 0.5
    if long_share is not None and long_share.height:
        ls_df = long_share.filter(pl.col("base") == base).sort("ts")
        if ls_df.height:
            ls = float(ls_df["long_share"][-1])
    p_t = float(px["close"][-1]) if px.height else None
    lam = {"lambda_minus": None, "lambda_plus": None, "asymmetry": None}
    if joined.height >= 2 and sigma and p_t:
        atoms = pos.liquidation_density(
            pos.oi_entries_from_history(joined), lev, th["positioning"]["maintenance_margin"], ls
        )
        lam = pos.liquidation_mass(
            atoms,
            p_t,
            sigma,
            th["rules"]["crowded_long"]["lambda_minus_kappa"],
            th["positioning"]["liquidation_horizon_h_days"],
        )
    d2 = None
    if dep_day is not None and dep_day.height:
        dd = dep_day.filter(pl.col("base") == base).sort("date")
        d2 = float(dd["depth_2pct_usd"][-1]) if dd.height else None
    long_liq_24h, liq_pct = None, None
    if liqs is not None and liqs.height:
        lq = liqs.filter((pl.col("base") == base) & (pl.col("side_closed") == "long")).with_columns(
            pl.col("ts").dt.date().alias("d")
        )
        if lq.height:
            daily_liq = lq.group_by("d").agg(pl.col("notional_usd").sum()).sort("d")
            long_liq_24h = float(
                lq.filter(pl.col("ts") > now - timedelta(hours=24))["notional_usd"].sum()
            )
            arr = np.append(daily_liq["notional_usd"].to_numpy(), long_liq_24h)
            liq_pct = pos.percentile_rank(arr, 90) if daily_liq.height >= 30 else None
    return {
        "ts": now,
        "base": base,
        "funding_ann": float(f["funding_ann"][-1]) if f["funding_ann"][-1] is not None else None,
        "z_fr": z_fr,
        "z_fr_n": n_fr,
        "oi_usd": float(oi_total[-1]) if oi_total[-1] else None,
        "oi_stats_venue": "okx",
        "oi_rel": oi_rel,
        "oi_rel_pctile": pct,
        "oi_rel_pctile_n": int(min(int(np.isfinite(oi_rel_hist).sum()), 90)),
        "oi_change_5d": oi_change_5d,
        "dlogp_5d": quad["dlogp"],
        "dlogoi_5d": quad["dlogoi"],
        "quadrant": quad["quadrant"],
        "oi_price_slope_20d": quad["slope_20d"],
        "long_share": ls,
        "lambda_minus_2": lam["lambda_minus"],
        "lambda_plus_2": lam["lambda_plus"],
        "lambda_asymmetry": lam["asymmetry"],
        "depth_2pct_usd": d2,
        "lambda_over_depth": (lam["lambda_minus"] / d2)
        if (lam["lambda_minus"] is not None and d2)
        else None,
        "long_liq_24h_usd": long_liq_24h,
        "long_liq_24h_pctile": liq_pct,
        "liq_source": _liq_source(liqs) if long_liq_24h is not None else None,
        "sigma_daily": sigma,
        "source": "perp_snapshot+prices_daily+orderbook_depth+liquidations",
        "fetched_at": now,
    }


def _liq_source(liqs: pl.DataFrame | None) -> str | None:
    """Label the liquidation sample by the venues actually present in the table (B1)."""
    if liqs is None or not liqs.height:
        return None
    venues = sorted(liqs["venue"].drop_nulls().unique().to_list())
    kind = "single-venue sample" if len(venues) == 1 else "multi-venue"
    return f"{'+'.join(venues)} ({kind})"


def _options_row(cur, opts, prices, now) -> dict | None:
    if opts is None or not opts.height:
        return None
    o = opts.filter(pl.col("currency") == cur)
    if not o.height:
        return None
    last_ts = o["ts"].max()
    chain = o.filter(pl.col("ts") == last_ts)
    px = prices.filter(pl.col("base") == cur).sort("date")["close"].to_numpy()
    lr = np.diff(np.log(px)) if px.size > 1 else np.array([])
    m = pos.options_metrics(chain, lr)
    dv = archive.read("dvol")
    dvol = None
    if dv is not None and dv.height:
        d = dv.filter(pl.col("currency") == cur).sort("ts")
        dvol = float(d["dvol"][-1]) if d.height else None
    return {
        "ts": now,
        "currency": cur,
        "chain_ts": last_ts,
        "n_instruments": chain.height,
        "dvol": dvol,
        "iv_1w": m["iv_1w"],
        "iv_1m": m["iv_1m"],
        "iv_3m": m["iv_3m"],
        "term_slope": m["term_slope"],
        "rr25_1m": m["rr25_1m"],
        "rv30_var": m["rv30_var"],
        "vrp": m["vrp"],
        "largest_oi": m["largest_oi"],
        "n_expiries": m["n_expiries"],
        "source": "deribit",
        "fetched_at": now,
    }


def _fragility_row(fd, prices, opt_rows, mcap, tier1, th, as_of, now) -> dict | None:
    """A3: the live value is the last row of the shared daily series (compute.fragility)."""
    from monitor.compute import fragility as frag_mod

    series = build_fragility_series(fd, prices, mcap, tier1, th, as_of)
    if series is None or not series.height:
        return None
    archive.upsert(
        "fragility_series",
        series.with_columns(
            pl.lit("compute.fragility.build_series").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(git_sha()).alias("git_sha"),
        ),
    )
    r = frag_mod.last_row(series, as_of)
    if r is None:
        return None
    return {
        "ts": now,
        "date": r["date"],
        "phi": r["phi"],
        "n_components": int(r["n_components"]),
        **{c: r.get(c) for c in frag_mod.COMPONENTS},
        **{f"{c}_n": None for c in frag_mod.COMPONENTS},
        "z_fr_age_days": r.get("fr_age_days"),
        "z_oi_age_days": r.get("oi_rel_age_days"),
        "z_vrp_neg_age_days": r.get("vrp_neg_age_days"),
        "z_dd_age_days": r.get("dd_age_days"),
        "z_sc_neg_age_days": r.get("sc_neg_age_days"),
        "funding_ann_t1": r.get("funding_ann_t1"),
        "oi_rel_t1": r.get("oi_rel_t1"),
        "dd_90": r.get("dd_90"),
        "source": "fragility_series (shared live/history builder)",
        "fetched_at": now,
    }


def build_fragility_series(fd, prices, mcap, tier1, th, as_of):
    """Inputs on the daily grid for the shared builder: Tier 1 OI-weighted funding, Σ OKX OI /
    Σ Tier 1 cap, DVOL-based VRP (plus today's chain value when the DVOL day is missing), BTC
    close, stablecoin 30-day growth."""
    from monitor.compute import fragility as frag_mod

    if fd is None or not fd.height:
        return None
    w = th["robust_z"]["window_days_fragility"]
    t1 = set(tier1["symbol"].to_list())
    f = fd.filter(pl.col("base").is_in(t1)).sort("date")
    fund = (
        f.group_by("date")
        .agg(
            (
                (pl.col("funding_ann") * pl.col("oi_usd").fill_null(1.0)).sum()
                / pl.col("oi_usd").fill_null(1.0).sum()
            ).alias("fr")
        )
        .sort("date")
    )
    cap_t1 = sum(v for k, v in mcap.items() if k in t1 and v)
    oi = (
        f.group_by("date")
        .agg(
            pl.col("oi_usd_okx").sum().alias("oi"),
            pl.col("oi_usd_okx").is_not_null().sum().alias("n"),
        )
        .filter(pl.col("n") > 0)
    )
    oi_rel = (
        oi.with_columns((pl.col("oi") / cap_t1).alias("oi_rel")).select("date", "oi_rel")
        if cap_t1
        else None
    )
    vh = archive.read("vrp_history")
    vrp = (
        vh.filter(pl.col("currency") == "BTC").select("date", "vrp")
        if vh is not None and vh.height
        else None
    )
    btc = prices.filter(pl.col("base") == "BTC").select("date", "close")
    sc = archive.read("stablecoin_growth")
    scg = sc.select("date", "growth_30d") if sc is not None and sc.height else None
    return frag_mod.build_series(fund, oi_rel, vrp, btc, scg, as_of, window=w)


def _vol_state(prices, now, sha) -> dict | None:
    btc = prices.filter(pl.col("base") == "BTC").sort("date")
    if btc.height < 60:
        return None
    lr = np.diff(np.log(btc["close"].to_numpy()))
    # non-overlapping weekly realised vol: overlapping 20-day windows gave an AR coefficient
    # of one and meaningless durations
    rv = state_mod.realised_vol_weekly(lr)
    res = state_mod.vol_state_model(np.log(rv[np.isfinite(rv) & (rv > 0)]), min_obs=100)
    row = {
        "date": btc["date"][-1],
        "status": res["status"] + " (weekly realised vol; durations in weeks)",
        "p_high": res.get("p_high"),
        "expected_duration_high": res.get("expected_duration_high"),
        "expected_duration_low": res.get("expected_duration_low"),
        "params": json.dumps(res.get("params")),
        "std_errors": json.dumps(res.get("std_errors")),
        "n_obs": res.get("n_obs"),
        "source": "prices_daily",
        "fetched_at": now,
        "git_sha": sha,
    }
    archive.upsert("vol_state", pl.DataFrame([row]))
    return row


# --------------------------------------------------------------------------- site JSON
def _basis_term() -> list[dict]:
    """Current dated-futures basis by expiry for BTC and ETH on every venue with marks: the
    term structure of basis shown next to Φ (A6). Spot is the venue's index price at the mark's
    timestamp (daily close only as a fallback); expiries under seven days are left out, as in
    the trade table, because annualising a few days of basis is noise."""
    from monitor.compute.trades import annualised_basis
    from monitor.jobs_risk import _daily_close_by_base, _index_spot, latest_marks

    last = latest_marks()
    if last is None or not last.height:
        return []
    now = utc_now()
    px = _daily_close_by_base()
    spot = {
        r["base"]: r["close"]
        for r in px.sort("date").group_by("base").agg(pl.col("close").last()).to_dicts()
    }
    spot.update(_index_spot(last))
    out = []
    for r in (
        last.filter(
            pl.col("base").is_in(["BTC", "ETH"]) & (pl.col("expiry") > now + timedelta(days=7))
        )
        .sort("expiry")
        .to_dicts()
    ):
        p = spot.get(r["base"])
        b = annualised_basis(r["mark_price"], p, r["expiry"], now) if p else None
        if b is None:
            continue
        out.append(
            {
                "base": r["base"],
                "venue": r["venue"],
                "symbol": r["symbol"],
                "expiry": str(r["expiry"].date()),
                "days": (r["expiry"] - now).total_seconds() / 86400,
                "basis_ann": b,
                "spot_basis": "index" if r["base"] in _index_spot(last) else "daily close",
            }
        )
    return out


def _reading(payload: dict, out: Path) -> list[dict]:
    """State-reading segments (C1) from the same rows the page shows."""
    import json

    from monitor.compute.reading import state_reading

    frag = payload["fragility"][0] if payload["fragility"] else None
    pos = next((r for r in payload["positioning"] if r.get("base") == "BTC"), None)
    dd90 = dd_cycle = None
    px = _daily_prices_by_base()
    btc = px.filter(pl.col("base") == "BTC").sort("date")
    if btc.height:
        c = btc["close"].to_numpy()
        dd90 = float(c[-1] / c[-90:].max() - 1)
        dd_cycle = float(c[-1] / c.max() - 1)
    sc = archive.read("stablecoin_growth")
    sc30 = None
    if sc is not None and sc.height:
        sc30 = sc.sort("date")["growth_30d"].drop_nulls().to_list()
        sc30 = sc30[-1] if sc30 else None
    breaches: list[str] = []
    low = False
    rk = out / "risk.json"
    if rk.exists():
        try:
            v = json.loads(rk.read_text()).get("venue") or {}
            breaches = [e["venue"] for e in v.get("exposure", []) if e.get("breach")]
            low = bool((v.get("low_score") or {}).get("breach"))
        except (OSError, ValueError):
            pass
    return state_reading(
        utc_now().date(),
        frag,
        payload["options"],
        payload["basis_term"],
        pos,
        dd90,
        dd_cycle,
        sc30,
        payload["rules"],
        breaches,
        low,
    )


def tiered_symbols() -> list[str]:
    """Symbols with a tier in the latest universe; excluded names (stablecoins, wrapped,
    tokenised assets) are dropped from every live listing even if older rows exist."""
    uni = archive.read("universe")
    if uni is None or not uni.height:
        return []
    u = uni.filter((pl.col("as_of") == uni["as_of"].max()) & pl.col("tier").is_not_null())
    return u["symbol"].to_list()


def latest_rule_fires(max_age_hours: int = 48) -> pl.DataFrame | None:
    """The latest evaluation of every (rule, asset) pair, whichever job produced it. Rules
    4.x are evaluated hourly, Rule 5.1 by the daily context job; filtering on one timestamp
    would drop whichever ran earlier."""
    rf = archive.read("rule_fires")
    if rf is None or not rf.height:
        return None
    cutoff = utc_now() - timedelta(hours=max_age_hours)
    tiered = tiered_symbols()
    if tiered:
        rf = rf.filter(pl.col("asset").is_in(tiered))
    return (
        rf.filter(pl.col("ts") >= cutoff)
        .sort("ts")
        .unique(subset=["rule_id", "asset"], keep="last")
        .sort("rule_id", "asset")
    )


def write_hourly_json(out: Path = SITE_DATA) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def latest(table: str, key: str) -> list[dict]:
        df = archive.read(table)
        if df is None or not df.height:
            return []
        return df.filter(pl.col(key) == df[key].max()).to_dicts()

    payload = {
        "generated_at": utc_now().isoformat(),
        "git_sha": git_sha(),
        "positioning": latest("positioning", "ts"),
        "options": latest("options_metrics", "ts"),
        "fragility": latest("fragility", "ts"),
        "rules": (lambda r: r.to_dicts() if r is not None else [])(latest_rule_fires()),
        "liquidity": [
            r for r in latest("liquidity", "date") if r.get("base") in set(tiered_symbols())
        ],
        "vol_state": latest("vol_state", "date"),
        "basis_term": _basis_term(),
    }
    payload["reading"] = _reading(payload, out)
    (out / "hourly.json").write_text(dump_json(payload))
