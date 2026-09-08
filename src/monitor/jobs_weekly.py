"""Weekly job (build prompt §6): tier freeze, factor model, screen IC, hit rates, raw-file
rotation and the repo-size check. The review issue is opened by the workflow."""

from __future__ import annotations

import logging
from datetime import date, timedelta

import polars as pl

from monitor import archive
from monitor.compute import hitrates as hr
from monitor.jobs_hourly import _daily_prices_by_base
from monitor.jobs_risk import compute_factor_model
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
    events = cs.event_table(ev, vp, symbol_of, float_now, upd, as_of, start, wash_pass)
    summary = cs.summarise(events, th)
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
                        }
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


def compute_weekly() -> dict:
    out = {"tier_history": freeze_tier_membership()}
    out.update(compute_factor_model(utc_now(), git_sha()))
    h = compute_hit_rates()
    out["hit_rates"] = h.height
    out.update(compute_cliff_study())
    from monitor.jobs_daily_ctx import write_daily_json
    from monitor.jobs_risk import compute_all_risk

    compute_all_risk()  # refresh w'B and screen ICs on the page
    write_daily_json()  # also refreshes history.json
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
