"""Phase 4 daily datasets: stablecoins, macro (FRED), unlocks, fees/TVL, on-chain (BTC/ETH),
governance, event strip. Called from `jobs.fetch_daily` / `jobs.compute_daily`."""

from __future__ import annotations

import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import polars as pl
import yaml

from monitor import archive
from monitor import rules as rules_mod
from monitor.compute import context as ctx
from monitor.compute import supply as sup
from monitor.fetch import fred, llama, okx, onchain, snapshot
from monitor.fetch.base import RawStore
from monitor.meta import dump_json, git_sha, utc_now
from monitor.paths import CONFIG, SITE_DATA

log = logging.getLogger("monitor.daily_ctx")


def _cfg(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def fetch_context(ts: datetime | None = None, force: bool = False) -> dict[str, str]:
    ts = ts or utc_now()
    out: dict[str, str] = {}
    store = RawStore()

    def weekly(name: str, fn):
        """Large, slow-moving datasets (3 MB and 1.4 MB gzipped): refresh when the latest raw
        file is older than 6 days, else reuse it."""
        latest = store.latest(name)
        if latest is not None and not force:
            day = datetime.strptime(str(latest.parent.relative_to(store.root)), "%Y/%m/%d").date()
            if (ts.date() - day).days < 6:
                return f"reused {latest} (weekly cadence)"
        return fn()

    oc = archive.read("onchain")
    cm_start = "2010-01-01"
    if oc is not None and oc.height:
        cm_start = str(oc["date"].max() - timedelta(days=10))
    steps = [
        ("stablecoins", lambda: llama.fetch_stablecoins(ts=ts, force=force)),
        (
            "unlocks",
            lambda: weekly(
                "llama_datasets_unlocks", lambda: llama.fetch_unlocks(ts=ts, force=force)
            ),
        ),
        (
            "fees_tvl",
            lambda: weekly("llama_api_fees_tvl", lambda: llama.fetch_fees_tvl(ts=ts, force=force)),
        ),
        (
            "unlock_detail",
            lambda: weekly(
                "llama_datasets_unlock_detail",
                lambda: llama.fetch_unlock_detail(_tier12_slugs(), ts=ts, force=force),
            ),
        ),
        ("fred", lambda: fred.fetch_series(ts=ts, force=force)),
        ("fred_meta", lambda: fred.fetch_meta(ts=ts, force=force)),
        ("coinmetrics", lambda: onchain.fetch_coinmetrics(start=cm_start, ts=ts, force=force)),
        ("btc_chain", lambda: onchain.fetch_btc_chain(ts=ts, force=force)),
        ("mempool", lambda: onchain.fetch_mempool(ts=ts, force=force)),
        ("eth_staking", lambda: onchain.fetch_eth_staking(ts=ts, force=force)),
        (
            "okx_oi_rubik_1d",
            lambda: okx.fetch_oi_rubik(_tier12_okx_ccys(), period="1D", ts=ts, force=force),
        ),
        (
            "snapshot",
            lambda: snapshot.fetch_proposals(
                _cfg("events.yaml")["snapshot_spaces"], ts=ts, force=force
            ),
        ),
    ]
    for name, fn in steps:
        try:
            p = fn()
            out[name] = str(p) if p else "skipped (no key)"
        except Exception as e:
            log.warning("%s failed: %s", name, e)
            out[name] = f"FAILED: {e}"
    return out


def _tier12_okx_ccys() -> list[str]:
    uni = archive.read("universe")
    if uni is None:
        return []
    u = uni.filter((pl.col("as_of") == uni["as_of"].max()) & pl.col("tier").is_in([1, 2]))
    return sorted(u["symbol"].to_list())


def _tier12_slugs() -> list[str]:
    """DefiLlama protocol slugs for Tier 1/2 assets that have an unlock schedule."""
    uni = archive.read("universe")
    sup = archive.read("unlock_supply")
    if uni is None or sup is None or not sup.height:
        return []
    ids = set(
        uni.filter((pl.col("as_of") == uni["as_of"].max()) & pl.col("tier").is_in([1, 2]))[
            "id"
        ].to_list()
    )
    latest = sup.filter(pl.col("as_of") == sup["as_of"].max())
    return sorted(
        {
            r["protocol"]
            for r in latest.filter(pl.col("id").is_in(ids)).select("protocol").to_dicts()
            if r["protocol"]
        }
    )


def _envs(store: RawStore, name: str, rebuild: bool):
    files = store.all(name) if rebuild else ([store.latest(name)] if store.latest(name) else [])
    return [store.read(f) for f in files if f]


def compute_context(as_of: date | None = None, rebuild: bool = False) -> dict[str, int]:
    store = RawStore()
    E = lambda name: _envs(store, name, rebuild)  # noqa: E731
    counts: dict[str, int] = {}

    def put(table: str, frames: list[pl.DataFrame]) -> None:
        frames = [f for f in frames if f is not None and f.height]
        if frames:
            counts[table] = archive.upsert(table, pl.concat(frames, how="diagonal_relaxed")).height

    snaps, hists = [], []
    for e in E("llama_stables_stablecoins"):
        s, h = llama.parse_stablecoins(e)
        snaps.append(s)
        hists.append(h)
    put("stablecoins", snaps)
    put("stablecoin_total", hists)
    evs, sups = [], []
    for e in E("llama_datasets_unlocks"):
        ev, s = llama.parse_unlocks(e)
        evs.append(ev)
        sups.append(s)
    put("unlock_events", evs)
    put("unlock_supply", sups)
    put("unlock_detail", [llama.parse_unlock_detail(e) for e in E("llama_datasets_unlock_detail")])
    put("fees_tvl", [llama.parse_fees_tvl(e) for e in E("llama_api_fees_tvl")])
    put("oi_rubik", [okx.parse_oi_rubik(e) for e in E("okx_oi_rubik_1D")])
    put("macro", [fred.parse_series(e) for e in E("fred_csv_series")])
    put("onchain", [onchain.parse_coinmetrics(e) for e in E("coinmetrics_asset_metrics")])
    put("btc_chain", [onchain.parse_btc_chain(e) for e in E("blockchain_info_charts")])
    oc = archive.read("onchain")
    eth_supply = None
    if oc is not None and oc.height:
        e = oc.filter((pl.col("asset") == "ETH") & pl.col("SplyCur").is_not_null()).sort("date")
        eth_supply = float(e["SplyCur"][-1]) if e.height else None
    put(
        "eth_staking",
        [onchain.parse_eth_staking(e, eth_supply) for e in E("ultrasound_supply_parts")],
    )
    put("proposals", [snapshot.parse_proposals(e) for e in E("snapshot_proposals")])
    counts.update(compute_supply_and_events(as_of))
    return counts


def compute_supply_and_events(as_of: date | None = None) -> dict[str, int]:
    th = _cfg("thresholds.yaml")
    sha, now = git_sha(), utc_now()
    counts: dict[str, int] = {}
    uni = archive.read("universe")
    if uni is None:
        return counts
    uni = uni.filter(pl.col("as_of") == uni["as_of"].max())
    as_of = as_of or uni["as_of"][0]
    markets = archive.read("markets")
    mk = markets.filter(pl.col("as_of") == markets["as_of"].max()).unique(
        subset=["id"], keep="last"
    )
    float_supply = {
        r["id"]: r["circulating_supply"]
        for r in mk.select("id", "circulating_supply").to_dicts()
        if r["circulating_supply"]
    }
    price = {
        r["id"]: r["price_usd"] for r in mk.select("id", "price_usd").to_dicts() if r["price_usd"]
    }
    symbols = dict(zip(uni["id"], uni["symbol"], strict=True))
    liq = archive.read("liquidity")
    adv_real: dict[str, float] = {}
    if liq is not None and liq.height:
        liq = liq.filter(pl.col("date") == liq["date"].max())
        adv_real = {
            r["id"]: r["adv_real_usd"]
            for r in liq.select("id", "adv_real_usd").to_dicts()
            if r["adv_real_usd"]
        }
    # fall back to reported ADV from the universe table for names without a liquidity row (flagged)
    adv_reported = {
        r["id"]: r["adv_30d_usd"]
        for r in uni.select("id", "adv_30d_usd").to_dicts()
        if r["adv_30d_usd"]
    }
    adv = {**adv_reported, **adv_real}
    ev = archive.read("unlock_events")
    if ev is not None and ev.height:
        ev = ev.filter(pl.col("fetched_at") == ev["fetched_at"].max())
        sched = sup.expand_linear(ev)
        # exact daily amounts from the per-protocol detail replace the even-spread linear
        # approximation for the protocols we have it for
        det = archive.read("unlock_detail")
        sup_tab = archive.read("unlock_supply")
        if det is not None and det.height and sup_tab is not None:
            det = det.filter(pl.col("fetched_at") == det["fetched_at"].max())
            latest_sup = sup_tab.filter(pl.col("as_of") == sup_tab["as_of"].max())
            slug_to_id = {
                r["protocol"]: r["id"]
                for r in latest_sup.select("protocol", "id").to_dicts()
                if r["protocol"]
            }
            det = det.with_columns(
                pl.col("protocol").replace_strict(slug_to_id, default=None).alias("id")
            ).drop_nulls("id")
            covered = set(det["id"].to_list())
            exact = det.filter(pl.col("date") > as_of).select(
                "id",
                "date",
                pl.lit("linear_exact").alias("kind"),
                pl.col("label").alias("recipient"),
                pl.col("label").alias("category"),
                "recipient_class",
                "amount",
                pl.lit("llama_datasets detail").alias("source"),
                "fetched_at",
                "git_sha",
            )
            sched = pl.concat(
                [
                    sched.filter(~(pl.col("id").is_in(covered) & (pl.col("kind") == "linear"))),
                    exact.select(sched.columns),
                ],
                how="vertical_relaxed",
            )
        in_uni = sched.filter(pl.col("id").is_in(uni.filter(pl.col("tier").is_not_null())["id"]))
        frames = []
        for h in (13, 30, 90):
            e = sup.esp(in_uni, as_of, h, float_supply, price, adv, th["supply"]["pi_c"])
            frames.append(e)
        esp_df = pl.concat(frames, how="diagonal_relaxed").with_columns(
            pl.lit(as_of).alias("as_of"),
            pl.lit("llama_datasets+markets+liquidity").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        esp_df = esp_df.with_columns(
            pl.col("id").is_in(list(adv_real)).not_().alias("adv_is_reported")
        )
        archive.upsert("esp", esp_df)
        counts["esp"] = esp_df.height
        dil = sup.dilution(
            in_uni, as_of, {k: v for k, v in float_supply.items() if k in set(uni["id"])}
        ).with_columns(
            pl.lit(as_of).alias("as_of"),
            pl.lit("llama_datasets+markets").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        archive.upsert("dilution", dil)
        counts["dilution"] = dil.height
        cl = sup.cliffs(in_uni, as_of, 28, float_supply, price, adv)
        fires = []
        for r in cl.to_dicts():
            f = rules_mod.cliff(
                symbols.get(r["id"], r["id"]),
                now,
                r["share_of_float"],
                r["days_of_volume"],
                th["rules"],
                str(r["date"]),
            )
            fires.append(
                {
                    **f.model_dump(exclude={"inputs", "thresholds"}),
                    "inputs": json.dumps(f.inputs, default=str),
                    "thresholds": json.dumps(f.thresholds),
                    "source": "rules",
                    "fetched_at": now,
                    "git_sha": sha,
                }
            )
        if cl.height:
            archive.upsert(
                "cliffs",
                cl.with_columns(
                    pl.lit(as_of).alias("as_of"),
                    pl.lit("llama_datasets+markets+liquidity").alias("source"),
                    pl.lit(now).alias("fetched_at"),
                    pl.lit(sha).alias("git_sha"),
                ),
            )
            counts["cliffs"] = cl.height
        if fires:
            archive.upsert("rule_fires", pl.DataFrame(fires))
    else:
        cl = None
    # stablecoin growth (fragility input)
    tot = archive.read("stablecoin_total")
    if tot is not None and tot.height:
        g = ctx.stablecoin_growth(tot, as_of).with_columns(
            pl.lit("llama_stables").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        archive.upsert("stablecoin_growth", g)
        counts["stablecoin_growth"] = g.height
    # macro
    fr = archive.read("macro")
    if fr is not None and fr.height:
        m, _ = ctx.macro_table(fr, as_of)
        m = m.with_columns(
            pl.lit(as_of).alias("as_of"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        archive.upsert("macro_latest", m)
        counts["macro_latest"] = m.height
    # event strip
    props = archive.read("proposals")
    exp = None
    om = archive.read("options")
    if om is not None and om.height:
        last = om.filter(pl.col("ts") == om["ts"].max())
        exp = last.group_by("currency", "expiry").agg(
            pl.col("open_interest").sum().alias("total_oi"),
            pl.col("strike")
            .filter(pl.col("open_interest") == pl.col("open_interest").max())
            .first()
            .alias("max_oi_strike"),
        )
    strip = ctx.event_strip(
        as_of,
        28,
        cl,
        props,
        _cfg("events.yaml").get("events") or [],
        exp,
        symbols,
        cliff_th=th["rules"]["cliff"],
        expiry_oi_share_min=th.get("events", {}).get("expiry_oi_share_min", 0.10),
    )
    strip = strip.with_columns(
        pl.lit(as_of).alias("as_of"), pl.lit(now).alias("fetched_at"), pl.lit(sha).alias("git_sha")
    )
    if strip.height:
        archive.upsert("event_strip", strip)
    counts["event_strip"] = strip.height
    write_daily_json()
    return counts


def write_daily_json(out: Path = SITE_DATA) -> None:
    out.mkdir(parents=True, exist_ok=True)

    def latest(table: str, key: str) -> list[dict]:
        df = archive.read(table)
        if df is None or not df.height:
            return []
        return df.filter(pl.col(key) == df[key].max()).to_dicts()

    sc = latest("stablecoins", "as_of")
    growth = latest("stablecoin_growth", "date")
    payload = {
        "generated_at": utc_now().isoformat(),
        "git_sha": git_sha(),
        "macro": [
            {
                k: r[k]
                for k in ("series_id", "series", "unit", "date", "value", "change_4w", "source")
            }
            for r in latest("macro_latest", "as_of")
        ],
        "stablecoins": {
            "date": str(growth[0]["date"]),
            "total_usd": growth[0]["total_usd"],
            "growth_30d": growth[0]["growth_30d"],
            "coins": sorted(sc, key=lambda r: -(r["supply_usd"] or 0))[:12],
        }
        if growth
        else None,
        "events": {
            "rows": latest("event_strip", "as_of"),
            "as_of": str(latest("event_strip", "as_of")[0]["as_of"])
            if latest("event_strip", "as_of")
            else None,
        },
        "esp": latest("esp", "as_of"),
        "dilution": latest("dilution", "as_of"),
        "cliffs": latest("cliffs", "as_of"),
        "onchain": _onchain_summary(),
        "eth_staking": latest("eth_staking", "date")[:1],
        "hit_rates": _hit_rates(),
        "cliff_study": _cliff_study(),
    }
    (out / "daily.json").write_text(dump_json(payload))
    write_history_json(out)


def _onchain_summary() -> list[dict]:
    oc = archive.read("onchain")
    if oc is None or not oc.height:
        return []
    out = []
    for asset in ("BTC", "ETH"):
        a = oc.filter(pl.col("asset") == asset).sort("date")
        if not a.height:
            continue
        last = a.tail(1).to_dicts()[0]
        prev = a.filter(pl.col("date") <= last["date"] - timedelta(days=30)).tail(1).to_dicts()
        out.append(
            {
                "asset": asset,
                "date": str(last["date"]),
                "mvrv": last["CapMVRVCur"],
                "exchange_supply_ntv": last["SplyExNtv"],
                "exchange_supply_30d_change": (last["SplyExNtv"] - prev[0]["SplyExNtv"])
                if prev and prev[0]["SplyExNtv"] and last["SplyExNtv"]
                else None,
                "flow_in_usd": last["FlowInExUSD"],
                "flow_out_usd": last["FlowOutExUSD"],
                "active_addresses": last["AdrActCnt"],
                "hash_rate": last["HashRate"],
                "status": last.get("SplyExNtv_status"),
                "source": "coinmetrics community",
                "lth_sopr": None,
                "lth_sopr_note": "unavailable: not in the CoinMetrics community tier (Glassnode optional)",
                "realised_cap": None,
                "realised_cap_note": "unavailable free; MVRV is provided directly",
            }
        )
    return out


def _cliff_study() -> dict:
    cs = archive.read("cliff_study")
    if cs is None or not cs.height:
        return {}
    latest = cs.filter(pl.col("as_of") == cs["as_of"].max())
    ev = archive.read("cliff_study_events")
    n_ev = (
        int(ev.filter(pl.col("as_of") == ev["as_of"].max()).height)
        if ev is not None and ev.height
        else 0
    )
    return {
        "as_of": str(latest["as_of"][0]),
        "n_events": n_ev,
        "rows": latest.drop("source", "fetched_at", "git_sha", "as_of").to_dicts(),
    }


def _hit_rates() -> dict:
    hr = archive.read("hit_rates")
    if hr is None or not hr.height:
        return {}
    latest = hr.filter(pl.col("as_of") == hr["as_of"].max())
    return {
        r["rule_id"]: {"hit_rate": r["hit_rate"], "n": r["n"], "horizon_days": r["horizon_days"]}
        for r in latest.to_dicts()
    }


def write_history_json(out: Path = SITE_DATA, days: int = 730) -> None:
    """Time series the charts read (data/history.json): fragility history and components,
    OI-weighted funding for the majors, stablecoin growth, macro context, BTC close, DVOL and
    the current IV term structure. Two years of daily points keeps the file around 200 kB."""
    out.mkdir(parents=True, exist_ok=True)
    cutoff = utc_now().date() - timedelta(days=days)

    def series(table: str, cols: list[str], key: str = "date", where=None) -> list[dict]:
        df = archive.read(table)
        if df is None or not df.height:
            return []
        if where is not None:
            df = df.filter(where)
        df = df.filter(pl.col(key) >= cutoff).sort(key)
        return df.select([key, *cols]).to_dicts()

    payload = {
        "generated_at": utc_now().isoformat(),
        "git_sha": git_sha(),
        "days": days,
        "fragility": series(
            "fragility_history", ["phi", "n_components", "z_fr", "z_dd", "z_sc_neg"]
        ),
        "funding": {
            b: series("funding_daily_history", ["funding_ann", "oi_usd"], where=pl.col("base") == b)
            for b in ("BTC", "ETH", "SOL")
        },
        "positioning_history": {
            b: series("positioning_history", ["z_fr", "oi_change_5d"], where=pl.col("base") == b)
            for b in ("BTC", "ETH")
        },
        "stablecoins": series("stablecoin_growth", ["total_usd", "growth_30d"]),
        "macro": {
            sid: series("macro", ["value"], where=pl.col("series_id") == sid)
            for sid in ("WALCL", "WTREGEN", "RRPONTSYD", "DFII10", "DTWEXBGS", "VIXCLS")
        },
        "btc_close": _btc_close(cutoff),
        "dvol": _dvol_series(),
        "term_structure": _term_structure(),
        "basis": _basis_history(cutoff),
        "hit_rates": _hit_rates(),
    }
    (out / "history.json").write_text(dump_json(_round(payload)))


def _round(o, sig: int = 5):
    """Round floats to `sig` significant digits (chart resolution) to keep the file small."""
    if isinstance(o, float):
        return float(f"{o:.{sig}g}") if o == o else None
    if isinstance(o, dict):
        return {k: _round(v, sig) for k, v in o.items()}
    if isinstance(o, list):
        return [_round(v, sig) for v in o]
    return o


def _btc_close(cutoff: date) -> list[dict]:
    from monitor.jobs_hourly import _daily_prices_by_base

    p = _daily_prices_by_base()
    if not p.height:
        return []
    return (
        p.filter((pl.col("base") == "BTC") & (pl.col("date") >= cutoff))
        .sort("date")
        .select("date", "close", "volume_quote")
        .to_dicts()
    )


def _basis_history(cutoff: date) -> dict:
    """Daily annualised basis of the Binance COIN-M current and next quarterly for BTC and ETH
    (from `basis_history`, built from continuous klines against the index; A6)."""
    b = archive.read("basis_history")
    if b is None or not b.height:
        return {}
    b = b.filter(pl.col("date") >= cutoff).sort("date")
    out = {}
    for base in ("BTC", "ETH"):
        for ct in ("CURRENT_QUARTER", "NEXT_QUARTER"):
            sub = b.filter((pl.col("base") == base) & (pl.col("contract") == ct))
            out[f"{base}_{ct}"] = [
                {"date": r["date"], "basis_ann": r["basis_ann"], "days": r["days_to_expiry"]}
                for r in sub.to_dicts()
            ]
    return out


def _term_structure() -> dict:
    """Per-expiry ATM IV and RR25 from the latest options snapshot (for the term-structure chart)."""
    from monitor.compute.positioning import expiry_metrics

    opts = archive.read("options")
    if opts is None or not opts.height:
        return {}
    out = {}
    for cur in ("BTC", "ETH"):
        o = opts.filter(pl.col("currency") == cur)
        if not o.height:
            continue
        chain = o.filter(pl.col("ts") == o["ts"].max()).with_columns(pl.col("mark_iv") / 100.0)
        em = expiry_metrics(chain)
        out[cur] = {
            "ts": str(o["ts"].max()),
            "rows": em.sort("t_years")
            .select("expiry", "t_years", "atm_iv", "rr25", "total_oi", "max_oi_strike")
            .to_dicts(),
        }
    return out


def _dvol_series() -> dict:
    dv = archive.read("dvol")
    if dv is None or not dv.height:
        return {}
    return {
        c: [
            {"ts": str(r["ts"]), "dvol": r["dvol"]}
            for r in dv.filter(pl.col("currency") == c).sort("ts").to_dicts()
        ]
        for c in ("BTC", "ETH")
    }
