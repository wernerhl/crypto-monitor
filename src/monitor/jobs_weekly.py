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
    from monitor.jobs_daily_ctx import write_daily_json
    from monitor.jobs_risk import compute_all_risk

    compute_all_risk()  # refresh w'B and screen ICs on the page
    write_daily_json()
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
