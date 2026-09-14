"""Freshness contract (work order 6, A2/A3).

`config/freshness.yaml` names every processed table with its cadence, budget, owning job and
parent (for derived tables). Ages are measured on the data's own time column — a date column
counts as the end of that UTC day — never on `fetched_at`, which is how five days of frozen
daily grids went unreported (the rows were re-upserted daily with a fresh fetch time and an
old date). Every job writes `status_<job>.json` from this contract, and `assert_job` fails a
job when a table it owns breaches its budget or a derived table lags its parent by more than
one period of its cadence. The fragility builder reads its gap reasons from `reason_for`."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import polars as pl
import yaml

from monitor import archive
from monitor.meta import git_sha, utc_now
from monitor.paths import CONFIG, SITE_DATA


class FreshnessError(RuntimeError):
    pass


def contract() -> dict:
    return yaml.safe_load((CONFIG / "freshness.yaml").read_text())


def _latest_dt(df: pl.DataFrame, col: str) -> datetime | None:
    if col not in df.columns or not df.height:
        return None
    v = df[col].drop_nulls()
    if not v.len():
        return None
    m = v.max()
    if isinstance(m, datetime):
        return m if m.tzinfo else m.replace(tzinfo=UTC)
    if isinstance(m, date):
        return datetime.combine(m, datetime.min.time(), tzinfo=UTC) + timedelta(days=1)
    return None


def _is_date_col(spec: dict) -> bool:
    return spec.get("time_col", "fetched_at") in ("date", "as_of", "week", "hour_date")


def table_rows(now: datetime | None = None) -> list[dict]:
    """One row per table in the contract: latest data time, age, budget, parent lag, status,
    reason. `status` ∈ {fresh, stale, lagging, unavailable}."""
    now = now or utc_now()
    c = contract()
    periods = c["period_hours"]
    latest: dict[str, datetime | None] = {}
    rows_n: dict[str, int] = {}
    for t, spec in c["tables"].items():
        df = archive.read(t)
        rows_n[t] = int(df.height) if df is not None else 0
        latest[t] = _latest_dt(df, spec.get("time_col", "fetched_at")) if df is not None else None
    out = []
    for t, spec in c["tables"].items():
        lt = latest.get(t)
        budget = float(spec["budget_hours"])
        row = {
            "table": t,
            "cadence": spec["cadence"],
            "job": spec["job"],
            "kind": spec["kind"],
            "parent": spec.get("parent"),
            "optional": bool(spec.get("optional", False)),
            "date_keyed": _is_date_col(spec),
            "rows": rows_n[t],
            "latest": lt.isoformat() if lt else None,
            "age_hours": None
            if lt is None
            else round(max((now - lt).total_seconds() / 3600, 0.0), 2),
            "budget_hours": budget,
            "parent_lag_hours": None,
            "status": "unavailable",
            "reason": "table not built yet" if not rows_n[t] else "",
        }
        if lt is not None:
            row["status"] = "fresh" if row["age_hours"] <= budget else "stale"
            if row["status"] == "stale":
                row["reason"] = (
                    f"last data {lt:%Y-%m-%d %H:%M} UTC, {row['age_hours']:.0f} h old against a {budget:.0f} h budget ({spec['cadence']})"
                )
            parent = spec.get("parent")
            if parent and latest.get(parent) is not None:
                # compare on the coarser of the two clocks: a date-keyed table counts as its
                # data day, so a daily parent never looks ahead of an hourly child by a day
                pd_, ct = latest[parent], lt
                if _is_date_col(c["tables"][parent]) or _is_date_col(spec):
                    # a date column was stored as the end of its day: undo that for the day math
                    pday = (
                        (pd_ - timedelta(days=1)).date()
                        if _is_date_col(c["tables"][parent])
                        else pd_.date()
                    )
                    cday = (ct - timedelta(days=1)).date() if _is_date_col(spec) else ct.date()
                    lag = (pday - cday).days * 24.0
                else:
                    lag = (pd_ - ct).total_seconds() / 3600
                row["parent_lag_hours"] = round(max(lag, 0.0), 2)
                if lag > periods.get(spec["cadence"], 24) and row["status"] == "fresh":
                    row["status"] = "lagging"
                    row["reason"] = (
                        f"lags its parent {parent} by {lag:.0f} h (more than one {spec['cadence']} period)"
                    )
        out.append(row)
    return out


def violations(job: str, now: datetime | None = None) -> list[str]:
    """Tables owned by `job` that are stale, lagging, or (non-optional) unavailable."""
    return [
        f"{r['table']}: {r['status']} — {r['reason']}"
        for r in table_rows(now)
        if r["job"] == job
        and r["status"] != "fresh"
        and not (r["status"] == "unavailable" and r["optional"])
    ]


def assert_job(job: str, now: datetime | None = None) -> None:
    """A3: fail the job with the table names when its own tables breach the contract."""
    v = violations(job, now)
    if v:
        raise FreshnessError(f"freshness contract violated by job {job}:\n  " + "\n  ".join(v))


def reason_for(table: str, now: datetime | None = None) -> str | None:
    """Gap reason for a component whose source table is not fresh, in the form the tiles
    show: 'source stale since 2026-09-07 (dvol_daily; parent dvol is current)'. None when fresh."""
    rows = {r["table"]: r for r in table_rows(now)}
    r = rows.get(table)
    if r is None:
        return f"{table} is not in the freshness contract"
    if r["status"] == "fresh":
        return None
    if r["status"] == "unavailable":
        return f"{table} has no rows"
    since = r["latest"][:10] if r["latest"] else "?"
    if r.get("date_keyed") and r["latest"]:  # the row stores the end of the data day; show the day
        since = (datetime.fromisoformat(r["latest"]) - timedelta(days=1)).date().isoformat()
    parent = r.get("parent")
    ptxt = ""
    if parent and parent in rows:
        ptxt = f"; parent {parent} is {'current' if rows[parent]['status'] == 'fresh' else rows[parent]['status']}"
    return f"source stale since {since} ({table}{ptxt})"


def write_status(job: str, out: Path = SITE_DATA, fetches: list[dict] | None = None) -> Path:
    """status_<job>.json: every table with age, budget and reason, plus this job's fetch records."""
    from monitor.meta import dump_json

    out.mkdir(parents=True, exist_ok=True)
    now = utc_now()
    if fetches is None:
        fs = archive.read_fetch_status()
        fetches = []
        if fs is not None and fs.height:
            fetches = (
                fs.sort("ts")
                .group_by("dataset")
                .agg(
                    pl.col("ts").last(),
                    pl.col("ok").last(),
                    pl.col("reason").last(),
                    pl.col("job").last(),
                )
                .sort("ok", "dataset")
                .to_dicts()
            )
    payload = {
        "generated_at": now.isoformat(),
        "git_sha": git_sha(),
        "job": job,
        "tables": table_rows(now),
        "fetches": fetches,
    }
    p = out / f"status_{job}.json"
    p.write_text(dump_json(payload))
    return p
