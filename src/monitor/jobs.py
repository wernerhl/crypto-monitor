"""Job orchestration: what the Actions workflows and the Makefile call.

`fetch_daily` writes raw files (idempotent per day). `compute_daily` rebuilds the processed
tables from the raw files on disk — it never touches the network — so `make all` on a clean
clone reproduces every table from `data/raw/`.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import yaml

from monitor import archive
from monitor.compute import universe as uni_mod
from monitor.fetch import binance, bybit, coinbase, coingecko, coinpaprika, kraken, okx
from monitor.fetch.base import RawStore, SanityError
from monitor.fetch.resilient import FetchRun
from monitor.meta import dump_json, git_sha, utc_now
from monitor.paths import CONFIG, SITE_DATA

log = logging.getLogger("monitor.jobs")
COIN_META_PER_RUN = 30  # 30 × 12.5 s ≈ 6 min of the daily budget without a key
COIN_META_MAX_AGE_DAYS = 30


def _cfg(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def _candidates_from_markets(markets: pl.DataFrame, cfg: dict) -> pl.DataFrame:
    n = cfg["candidate_universe"]["top_n_by_market_cap"] + 60  # headroom before exclusions
    return markets.sort("rank", nulls_last=True).head(n)


# --------------------------------------------------------------------------- fetch
def fetch_daily(ts: datetime | None = None, force: bool = False) -> dict[str, str]:
    """Fetch every daily raw dataset for phase 2. Returns {dataset: path}."""
    ts = ts or utc_now()
    cfg = _cfg("universe.yaml")
    store = RawStore()
    out: dict[str, str] = {}

    # 1. aggregator markets (CoinGecko primary, CoinPaprika fallback)
    try:
        p = coingecko.fetch_markets(pages=cfg["candidate_universe"]["pages"], ts=ts, force=force)
        markets = coingecko.parse_markets(store.read(p))
        out["markets"] = str(p)
    except Exception as e:
        log.warning("coingecko markets failed (%s); falling back to coinpaprika", e)
        p = coinpaprika.fetch_markets(ts=ts, force=force)
        markets = coinpaprika.parse_markets(store.read(p))
        out["markets"] = str(p)
    cands = _candidates_from_markets(markets, cfg)

    # 1b. category membership lists (one call each) — exclusions for every candidate
    ex = cfg["exclusions"]
    out["category_members"] = str(
        coingecko.fetch_category_members(
            list(ex["stablecoin_category_ids"]) + list(ex["wrapped_or_lst_category_ids"]),
            ts=ts,
            force=force,
        )
    )

    # 2. coin metadata (categories) for candidates lacking fresh meta, capped per run
    meta = archive.read("coin_meta")
    fresh = set()
    if meta is not None and meta.height:
        cutoff = ts - timedelta(days=COIN_META_MAX_AGE_DAYS)
        fresh = set(meta.filter(pl.col("fetched_at") > cutoff)["id"].to_list())
    need = [i for i in cands["id"].to_list() if i not in fresh][:COIN_META_PER_RUN]
    if need:
        src = markets["source"][0]
        mod = coingecko if src == "coingecko" else coinpaprika
        out["coin_meta"] = str(mod.fetch_coin_meta(need, ts=ts, force=force))

    # 3. venue listings
    fr = FetchRun("daily", ts)
    fr.run("binance_listings", lambda: binance.fetch_listings(ts=ts, force=force))
    fr.run("bybit_listings", lambda: bybit.fetch_listings(ts=ts, force=force))
    fr.run("okx_listings", lambda: okx.fetch_listings(ts=ts, force=force))
    fr.run("coinbase_listings", lambda: coinbase.fetch_listings(ts=ts, force=force))
    fr.run("kraken_listings", lambda: kraken.fetch_listings(ts=ts, force=force))
    listings = _parse_listings(store, ts)
    symap = uni_mod.resolve_symbols(cands, listings, cfg.get("symbol_map") or {})

    # 4. perps (OI) — Binance needs per-symbol calls; Bybit/OKX one call each
    bsyms = [s["binance"] for s in symap["perp"].to_list() if s and s.get("binance")]
    fr.run("binance_perps", lambda: binance.fetch_perps(bsyms, ts=ts, force=force))
    fr.run("bybit_perps", lambda: bybit.fetch_perps(ts=ts, force=force))
    fr.run("okx_perps", lambda: okx.fetch_perps(ts=ts, force=force))

    # 5. daily spot candles for every candidate on every venue that lists it
    have_prices = archive.read("prices_daily")
    limit = 120 if have_prices is None or not have_prices.height else 10
    for venue, mod in (("binance", binance), ("bybit", bybit), ("okx", okx)):
        syms = [s[venue] for s in symap["spot"].to_list() if s and s.get(venue)]
        fr.run(
            f"{venue}_klines",
            lambda mod=mod, syms=syms: mod.fetch_klines_1d(syms, limit=limit, ts=ts, force=force),
        )
    since = (ts - timedelta(days=10)) if limit == 10 else None
    syms = [s["coinbase"] for s in symap["spot"].to_list() if s and s.get("coinbase")]
    fr.run(
        "coinbase_klines",
        lambda syms=syms: coinbase.fetch_klines_1d(syms, ts=ts, force=force, start=since),
    )
    syms = [s["kraken"] for s in symap["spot"].to_list() if s and s.get("kraken")]
    fr.run(
        "kraken_klines",
        lambda syms=syms: kraken.fetch_klines_1d(syms, ts=ts, force=force, since=since),
    )
    # 6. context datasets (stablecoins, macro, unlocks, fees, on-chain, governance)
    from monitor.jobs_daily_ctx import fetch_context

    out.update({f"ctx_{k}": v for k, v in fetch_context(ts=ts, force=force).items()})
    fr.flush()
    out.update(fr.out)
    return out


def _parse_listings(store: RawStore, ts: datetime) -> pl.DataFrame:
    sha = git_sha()
    parts = [
        binance.parse_listings(
            store.read(store.latest("binance_spot_listings", ts)),
            store.read(store.latest("binance_usdm_listings", ts)),
            sha,
        ),
        bybit.parse_listings(store.read(store.latest("bybit_listings", ts))),
        okx.parse_listings(store.read(store.latest("okx_listings", ts))),
        coinbase.parse_listings(store.read(store.latest("coinbase_listings", ts))),
        kraken.parse_listings(store.read(store.latest("kraken_listings", ts))),
    ]
    return pl.concat(parts, how="diagonal_relaxed")


# --------------------------------------------------------------------------- compute
def compute_daily(as_of: date | None = None, rebuild: bool = False) -> dict[str, int]:
    """Rebuild processed tables from raw files. With `rebuild`, every raw file ever fetched
    is replayed (used by `make all`); otherwise only the latest daily bucket is applied."""
    store = RawStore()
    counts: dict[str, int] = {}
    sha = git_sha()

    def envs(name: str):
        files = store.all(name) if rebuild else ([store.latest(name)] if store.latest(name) else [])
        return [store.read(f) for f in files if f]

    # markets
    frames = [coingecko.parse_markets(e) for e in envs("coingecko_markets")] + [
        coinpaprika.parse_markets(e) for e in envs("coinpaprika_markets")
    ]
    if frames:
        counts["markets"] = archive.upsert(
            "markets", pl.concat(frames, how="diagonal_relaxed")
        ).height
    # category members
    frames = [coingecko.parse_category_members(e) for e in envs("coingecko_category_members")]
    if frames:
        counts["category_members"] = archive.upsert(
            "category_members", pl.concat(frames, how="diagonal_relaxed")
        ).height
    # coin meta
    frames = [coingecko.parse_coin_meta(e) for e in envs("coingecko_coin_meta")] + [
        coinpaprika.parse_coin_meta(e) for e in envs("coinpaprika_coin_meta")
    ]
    frames = [f for f in frames if f.height]
    if frames:
        counts["coin_meta"] = archive.upsert(
            "coin_meta", pl.concat(frames, how="diagonal_relaxed")
        ).height
    # listings
    frames = []
    for e_spot, e_fut in zip(
        envs("binance_spot_listings"), envs("binance_usdm_listings"), strict=False
    ):
        frames.append(binance.parse_listings(e_spot, e_fut, sha))
    frames += [bybit.parse_listings(e) for e in envs("bybit_listings")]
    frames += [okx.parse_listings(e) for e in envs("okx_listings")]
    frames += [coinbase.parse_listings(e) for e in envs("coinbase_listings")]
    frames += [kraken.parse_listings(e) for e in envs("kraken_listings")]
    if frames:
        counts["venue_listings"] = archive.upsert(
            "venue_listings", pl.concat(frames, how="diagonal_relaxed")
        ).height
    # perps
    frames = (
        [binance.parse_perps(e) for e in envs("binance_usdm_perps")]
        + [bybit.parse_perps(e) for e in envs("bybit_perps")]
        + [okx.parse_perps(e) for e in envs("okx_perps")]
    )
    if frames:
        counts["perp_snapshot"] = archive.upsert(
            "perp_snapshot", pl.concat(frames, how="diagonal_relaxed")
        ).height
    # prices
    frames = []
    for name, mod in (
        ("binance_spot_klines_1d", binance),
        ("bybit_klines_1d", bybit),
        ("okx_klines_1d", okx),
        ("coinbase_klines_1d", coinbase),
        ("kraken_klines_1d", kraken),
    ):
        frames += [mod.parse_klines_1d(e) for e in envs(name)]
    frames = [f for f in frames if f.height]
    if frames:
        counts["prices_daily"] = archive.upsert(
            "prices_daily", pl.concat(frames, how="diagonal_relaxed")
        ).height

    # universe
    counts["universe"] = compute_universe(as_of=as_of).height
    from monitor.jobs_daily_ctx import compute_context

    counts.update(compute_context(as_of=as_of, rebuild=rebuild))
    for t in (
        "markets",
        "coin_meta",
        "venue_listings",
        "perp_snapshot",
        "prices_daily",
        "universe",
    ):
        archive.write_partitions(t)
    write_site_json()
    return counts


def compute_universe(as_of: date | None = None) -> pl.DataFrame:
    cfg = _cfg("universe.yaml")
    markets = archive.read("markets")
    if markets is None or not markets.height:
        raise SanityError("no markets table; run fetch first")
    as_of = as_of or markets["as_of"].max()
    day = markets.filter(pl.col("as_of") == as_of)
    src = "coingecko" if (day["source"] == "coingecko").any() else day["source"][0]
    day = day.filter(pl.col("source") == src)
    cands = _candidates_from_markets(day, cfg)
    meta = archive.read("coin_meta")
    members = archive.read("category_members")
    if members is not None and members.height:
        members = members.filter(pl.col("as_of") == members["as_of"].max())
    cands = uni_mod.classify_exclusions(cands, meta, cfg, members)
    allv = archive.read("venue_listings")
    if allv is None or not allv.height:
        raise SanityError("no venue_listings table; run fetch first")
    # each source's most recent listing snapshot (binance spot and usdm are separate sources
    # fetched at different timestamps; venues are fetched at different timestamps too)
    listings = allv.join(
        allv.group_by("venue", "source").agg(pl.col("ts").max()), on=["venue", "source", "ts"]
    )
    symap = uni_mod.resolve_symbols(cands, listings, cfg.get("symbol_map") or {})
    oi = uni_mod.oi_metrics(archive.read("perp_snapshot"), symap, as_of)
    adv = uni_mod.adv_metrics(archive.read("prices_daily"), symap, as_of)
    sectors = (cfg.get("sector_map") or {}).get("assignments") or {}
    # measured depth and wash-filtered ADV from the liquidity table (phase 3+); before the
    # first hourly run they are absent and the rules fall back to the flagged proxies
    depth_df = None
    liq = archive.read("liquidity")
    if liq is not None and liq.height:
        liq = liq.filter(pl.col("date") == liq["date"].max())
        depth_df = liq.select("id", "depth_2pct_usd").filter(pl.col("depth_2pct_usd").is_not_null())
        real = {
            r["id"]: r["adv_real_usd"]
            for r in liq.select("id", "adv_real_usd").to_dicts()
            if r["adv_real_usd"]
        }
        if real and adv.height:
            adv = (
                adv.with_columns(pl.col("id").replace_strict(real, default=None).alias("adv_real"))
                .with_columns(
                    pl.coalesce(pl.col("adv_real"), pl.col("adv_30d_usd")).alias("adv_30d_usd"),
                    pl.col("adv_real").is_not_null().alias("is_real"),
                )
                .drop("adv_real")
            )
    uni = uni_mod.tier(
        cands,
        symap,
        oi,
        adv,
        cfg,
        as_of,
        depth=depth_df,
        sector_map=sectors,
        source=src,
        fetched_at=utc_now(),
        git_sha=git_sha(),
    )
    archive.upsert("universe", uni)
    return uni


# freshness budget per table (hours); the status page greys anything older with the reason
MAX_AGE_H: dict[str, float] = {
    "markets": 30,
    "category_members": 30,
    "coin_meta": 24 * 45,
    "venue_listings": 30,
    "perp_snapshot": 30,
    "prices_daily": 54,
    "universe": 30,
}


def table_status(now: datetime | None = None) -> list[dict]:
    now = now or utc_now()
    rows = []
    for t, max_age in MAX_AGE_H.items():
        df = archive.read(t)
        if df is None or not df.height:
            rows.append(
                {
                    "table": t,
                    "rows": 0,
                    "latest": None,
                    "age_hours": None,
                    "max_age_hours": max_age,
                    "status": "unavailable",
                    "sources": "",
                    "reason": "table not built yet",
                }
            )
            continue
        tcol = archive.TIME_COL[t]
        latest = df[tcol].max()
        if isinstance(latest, date) and not isinstance(latest, datetime):
            # date-only stamp: the row's fetch time when the table has one, else end of that UTC day
            fetched = (
                df.filter(pl.col(tcol) == latest)["fetched_at"].max()
                if "fetched_at" in df.columns
                else None
            )
            latest_dt = (
                fetched
                if fetched is not None
                else datetime.combine(latest, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
            )
            if latest_dt.tzinfo is None:
                latest_dt = latest_dt.replace(tzinfo=UTC)
        else:
            latest_dt = latest if latest.tzinfo else latest.replace(tzinfo=UTC)
        age = max((now - latest_dt).total_seconds() / 3600, 0.0)  # A9: never negative
        assert age >= 0
        status = "fresh" if age <= max_age else "stale"
        rows.append(
            {
                "table": t,
                "rows": df.height,
                "latest": str(latest),
                "age_hours": round(age, 1),
                "max_age_hours": max_age,
                "status": status,
                "sources": ",".join(sorted(df["source"].unique().to_list())),
                "reason": None
                if status == "fresh"
                else f"latest row is {age:.0f} h old, budget {max_age} h",
            }
        )
    return rows


def write_site_json(out: Path = SITE_DATA) -> None:
    """JSON the page reads (phase 2: universe, status, build info)."""
    out.mkdir(parents=True, exist_ok=True)
    fs = archive.read("fetch_status")
    fetches = []
    if fs is not None and fs.height:
        latest = (
            fs.sort("ts")
            .group_by("dataset")
            .agg(
                pl.col("ts").last(),
                pl.col("ok").last(),
                pl.col("reason").last(),
                pl.col("job").last(),
            )
        )
        fetches = latest.sort("ok", "dataset").to_dicts()
    (out / "status.json").write_text(
        dump_json(
            {
                "generated_at": utc_now().isoformat(),
                "git_sha": git_sha(),
                "tables": table_status(),
                "fetches": fetches,
            },
            default=str,
        )
    )
    uni = archive.read("universe")
    if uni is None:
        return
    latest = uni.filter(pl.col("as_of") == uni["as_of"].max())
    payload = {
        "as_of": str(latest["as_of"][0]),
        "generated_at": utc_now().isoformat(),
        "git_sha": git_sha(),
        "rows": latest.select(
            "id",
            "symbol",
            "name",
            "tier",
            "excluded_reason",
            "sector",
            "market_cap_usd",
            "rank",
            "perp_venues",
            "perp_venue_list",
            "oi_median_usd",
            "oi_window_days",
            "depth_2pct_usd",
            "depth_status",
            "spot_venues",
            "adv_30d_usd",
            "adv_basis",
            "adv_window_days",
            "source",
        ).to_dicts(),
    }
    (out / "universe.json").write_text(dump_json(payload))
