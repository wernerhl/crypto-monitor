"""Weekly job (build prompt §6): tier freeze, factor model, screen IC, hit rates, raw-file
rotation and the repo-size check. The review issue is opened by the workflow."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import polars as pl

from monitor import archive
from monitor.compute import hitrates as hr
from monitor.jobs_hourly import _daily_prices_by_base
from monitor.meta import git_sha, utc_now

log = logging.getLogger("monitor.weekly")


def compute_hit_rates(as_of: date | None = None) -> pl.DataFrame:
    now, sha = utc_now(), git_sha()
    as_of = as_of or now.date()
    fires = archive.read("rule_fires")
    prices = _daily_prices_by_base()
    if fires is None or not fires.height or not prices.height:
        return pl.DataFrame()
    om = archive.read("options_metrics")
    iv_at = {}
    # historical flags: IV30 from the DVOL-based VRP history; live flags: the chain-based IV
    vh = archive.read("vrp_history")
    if vh is not None and vh.height:
        for r in vh.select("date", "currency", "iv30").to_dicts():
            iv_at[(r["currency"], r["date"])] = r["iv30"]
    if om is not None and om.height:
        for r in (
            om.with_columns(pl.col("ts").dt.date().alias("d"))
            .sort("ts")
            .unique(subset=["currency", "d"], keep="last")
            .to_dicts()
        ):
            if r.get("iv_1m") is not None:
                iv_at[(r["currency"], r["d"])] = r["iv_1m"]
    df = hr.hit_rates(fires, prices, iv_at, as_of).with_columns(
        pl.lit(as_of).alias("as_of"),
        pl.lit("rule_fires+prices_daily+options_metrics").alias("source"),
        pl.lit(now).alias("fetched_at"),
        pl.lit(sha).alias("git_sha"),
    )
    archive.upsert("hit_rates", df)
    return df


def compute_cliff_study(
    as_of: date | None = None, start: date = date(2021, 1, 1)
) -> dict[str, int]:
    """Historical unlock-cliff study (A8): per-event table and grouped hit rates on the
    methods page. Thresholds are read from config and not changed."""
    import yaml

    from monitor.compute import cliff_study as cs
    from monitor.paths import CONFIG

    now, sha = utc_now(), git_sha()
    as_of = as_of or now.date()
    th = yaml.safe_load((CONFIG / "thresholds.yaml").read_text())["rules"]["cliff"]
    ev = archive.read("unlock_events")
    vp = archive.read("prices_daily")
    if ev is None or not ev.height or vp is None or not vp.height:
        return {"cliff_study_events": 0, "cliff_study": 0}
    ev = ev.filter(pl.col("fetched_at") == ev["fetched_at"].max())
    mk = archive.read("markets")
    mk = mk.filter(pl.col("as_of") == mk["as_of"].max()).unique(subset=["id"], keep="last")
    symbol_of = {
        r["id"]: r["symbol"].upper() for r in mk.select("id", "symbol").to_dicts() if r["symbol"]
    }
    float_now = {
        r["id"]: r["circulating_supply"]
        for r in mk.select("id", "circulating_supply").to_dicts()
        if r["circulating_supply"]
    }
    us = archive.read("unlock_supply")
    upd: dict[str, float] = {}
    if us is not None and us.height:
        us = us.filter(pl.col("as_of") == us["as_of"].max())
        upd = {
            r["id"]: r["unlocks_per_day"]
            for r in us.select("id", "unlocks_per_day").to_dicts()
            if r["unlocks_per_day"]
        }
        for r in us.select("id", "circ_supply").to_dicts():
            float_now.setdefault(r["id"], r["circ_supply"])
    wash = archive.read("wash_filters")
    wash_pass: dict[str, list[str]] = {}
    if wash is not None and wash.height:
        w = wash.filter(pl.col("date") == wash["date"].max()).filter(pl.col("pass"))
        for b, g in w.group_by("base"):
            wash_pass[b[0] if isinstance(b, tuple) else b] = g["venue"].unique().to_list()
    betas = archive.read("factor_betas")
    events = cs.event_table(ev, vp, symbol_of, float_now, upd, as_of, start, wash_pass, betas)
    placebo = cs.placebo_table(events, vp, betas=betas)
    summary = cs.summarise(events, th, placebo)
    rule_bases = (
        events.filter(
            (pl.col("share_of_float") > th["single_unlock_float_share_min"])
            | (pl.col("days_of_volume") > th["single_unlock_days_of_volume_min"])
        )["base"]
        .unique()
        .to_list()
        if events.height
        else []
    )
    br_sub, n_sub = cs.base_rate(vp, rule_bases, start, as_of) if rule_bases else (None, 0)
    br, n_br = (
        cs.base_rate(vp, events["base"].unique().to_list(), start, as_of)
        if events.height
        else (None, 0)
    )
    summary = (
        pl.concat(
            [
                summary,
                pl.DataFrame(
                    [
                        {
                            **{c: None for c in summary.columns},
                            "group_kind": "base rate",
                            "group": "all days, same assets and period (share of negative 14-day returns)",
                            "n": n_br,
                            "hit_rate": br,
                        },
                        {
                            **{c: None for c in summary.columns},
                            "group_kind": "base rate",
                            "group": "all days, Rule 5.1 tokens only",
                            "n": n_sub,
                            "hit_rate": br_sub,
                        },
                    ]
                )
                .select(summary.columns)
                .cast(summary.schema),
            ],
            how="vertical",
        )
        if summary.height
        else summary
    )
    prov = {
        "as_of": pl.lit(as_of),
        "source": pl.lit("unlock_events+prices_daily+markets+unlock_supply+wash_filters"),
        "fetched_at": pl.lit(now),
        "git_sha": pl.lit(sha),
    }
    if events.height:
        archive.upsert("cliff_study_events", events.with_columns(**prov))
    if summary.height:
        archive.upsert("cliff_study", summary.with_columns(**prov))
    return {"cliff_study_events": events.height, "cliff_study": summary.height}


def compute_rule43_drivers(as_of: date | None = None) -> dict[str, int]:
    """Historical 4.3 flags by driver (work order 3, item 2): per-flag table and the summary
    for the methods page. Analysis only; the rule and its thresholds are unchanged."""
    from monitor.compute import rule43

    now, sha = utc_now(), git_sha()
    as_of = as_of or now.date()
    fires = archive.read("rule_fires")
    vh = archive.read("vrp_history")
    prices = _daily_prices_by_base()
    if fires is None or vh is None or not fires.height or not vh.height:
        return {"rule43_flags": 0, "rule43_drivers": 0}
    frames, summ = [], []
    for ccy in ("BTC", "ETH"):
        fl = rule43.classify_flags(fires, vh, prices, as_of, ccy)
        if fl.height:
            frames.append(fl)
            summ.append(rule43.driver_table(fl).with_columns(pl.lit(ccy).alias("currency")))
    prov = {
        "as_of": pl.lit(as_of),
        "source": pl.lit("rule_fires+vrp_history+prices_daily"),
        "fetched_at": pl.lit(now),
        "git_sha": pl.lit(sha),
    }
    n_f = n_s = 0
    if frames:
        f = pl.concat(frames, how="vertical").with_columns(**prov)
        archive.replace_slice("rule43_flags", "as_of", as_of, f)
        n_f = f.height
    if summ:
        t = pl.concat(summ, how="vertical").with_columns(**prov)
        archive.replace_slice("rule43_drivers", "as_of", as_of, t)
        n_s = t.height
    return {"rule43_flags": n_f, "rule43_drivers": n_s}


def freeze_tier_membership() -> int:
    """Copy the latest universe rows into `tier_history` with the freeze date (the official
    weekly membership used by backtests: membership as of date)."""
    uni = archive.read("universe")
    if uni is None or not uni.height:
        return 0
    latest = uni.filter(pl.col("as_of") == uni["as_of"].max()).select(
        "as_of",
        "id",
        "symbol",
        "tier",
        "excluded_reason",
        "oi_median_usd",
        "adv_30d_usd",
        "depth_2pct_usd",
        "depth_status",
        "adv_basis",
        "source",
        "fetched_at",
        "git_sha",
    )
    latest = latest.with_columns(pl.lit(utc_now().date()).alias("frozen_on"))
    archive.upsert("tier_history", latest)
    return latest.height


def compute_phi_validation(as_of: date | None = None) -> dict[str, int]:
    """Does Φ predict the damage after a shock? (work order 3, item 5). Tables for the note
    and the methods paragraph; no change to Φ or any threshold."""
    from monitor.compute import fragility_validation as fv

    now, sha = utc_now(), git_sha()
    as_of = as_of or now.date()
    frag = archive.read("fragility_series")
    px = _daily_prices_by_base().filter(pl.col("base") == "BTC").select("date", "close")
    if frag is None or not frag.height or not px.height:
        return {"phi_shock_events": 0}
    ev = fv.event_table(px, frag, as_of)
    plc = fv.placebo_table(px, frag, ev, as_of)
    reg = pl.concat(
        [
            fv.regressions(ev, "shocks"),
            fv.regressions(ev.filter(pl.col("n_components") >= 3), "shocks, ≥3 components"),
            fv.regressions(plc, "placebo"),
        ],
        how="vertical",
    )
    t_ev, edges = fv.terciles(ev, "shocks")
    t_sub, _ = fv.terciles(ev.filter(pl.col("n_components") >= 3), "shocks, ≥3 components", edges)
    t_pl, _ = fv.terciles(plc, "placebo", edges)
    terc = pl.concat([t_ev, t_sub, t_pl], how="vertical")
    prov = {
        "as_of": pl.lit(as_of),
        "source": pl.lit("fragility_series+prices_daily"),
        "fetched_at": pl.lit(now),
        "git_sha": pl.lit(sha),
    }
    out = {}
    for name, df in (
        ("phi_shock_events", ev),
        ("phi_shock_placebo", plc),
        ("phi_shock_regressions", reg),
        ("phi_shock_terciles", terc),
    ):
        if df.height:
            archive.replace_slice(name, "as_of", as_of, df.with_columns(**prov))
        out[name] = df.height
    return out


def compute_weekly() -> dict:
    """Weekly job (write set in config/job_writes.yaml): universe and tier freeze, factor
    model, risk panels (risk.json), the cliff study, the 4.3 driver table, the Φ validation."""
    from monitor.jobs import compute_universe
    from monitor.jobs_risk import compute_screens_weekly

    out = {"universe": compute_universe().height, "tier_history": freeze_tier_membership()}
    sc = compute_screens_weekly()  # factor model, screens, book factor exposure → screens.json
    out.update(sc.get("factor_model", {}))
    out["screens"] = len(sc["screens"])
    out.update(compute_cliff_study())
    out.update(compute_rule43_drivers())
    out.update(compute_phi_validation())
    return out


def rotation_plan(hourly_keep_days: int = 10, daily_keep_days: int = 90) -> dict[str, list]:
    """Raw files older than the retention windows, grouped by month, for the rotation step."""
    from monitor.fetch.base import RawStore

    store = RawStore()
    today = utc_now().date()
    plan: dict[str, list] = {}
    for p in store.root.glob("*/*/*/*.json.gz"):
        y, m, d = p.relative_to(store.root).parts[:3]
        day = date(int(y), int(m), int(d))
        hourly = not p.name.endswith("_0000.json.gz")
        keep = hourly_keep_days if hourly else daily_keep_days
        if today - day > timedelta(days=keep):
            plan.setdefault(f"{y}-{m}", []).append(str(p))
    return plan
