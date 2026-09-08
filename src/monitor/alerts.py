"""Alerts (work order C2): one GitHub issue per active condition, closed automatically when the
condition clears, and an RSS feed at site/alerts.xml carrying the same items.

Conditions (each keyed so that the same condition maps to the same issue):
* `rule:<id>:<asset>` — a trigger rule is firing in the latest evaluation.
* `venue:<venue>` — a venue limit (or the low-score aggregate) is breached on the example
  book (from the published risk.json).
* `dataset:<name>` — a dataset failed in the last two consecutive runs of its job (one
  failed run is noise: geo-blocks and rate limits clear on the next attempt).

Issues are managed through the `gh` CLI (present on GitHub runners with GITHUB_TOKEN); when
it is missing, or no repository is configured, the sync is a dry run and only the table and
the feed are written. The state lives in the `alerts` table (key, opened_at, closed_at,
issue_number) so the feed shows when each condition opened and closed."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import shutil
import subprocess
from datetime import datetime
from email.utils import format_datetime
from pathlib import Path
from xml.sax.saxutils import escape

import polars as pl

from monitor import archive
from monitor.meta import git_sha, utc_now
from monitor.paths import SITE

log = logging.getLogger("monitor.alerts")
LABEL = "alert"
SITE_URL = "https://wernerhl.github.io/crypto-monitor/"
PANEL = {
    "rule": "#p-triggers",
    "cliffs": "#p-events",
    "venue": "#p-venue",
    "dataset": "status.html",
}


def current_conditions(site_data: Path = SITE / "data") -> list[dict]:
    """Active conditions from the archive and the published risk.json."""
    out: list[dict] = []
    from monitor.jobs_hourly import latest_calendar, latest_rule_fires

    cal = latest_calendar()
    cliffs = []
    if cal is not None and cal.height:
        for r in cal.filter(pl.col("fired")).to_dicts():
            try:
                cliffs.append((r["asset"], json.loads(r.get("inputs") or "{}")))
            except ValueError:
                cliffs.append((r["asset"], {}))
    last = latest_rule_fires()
    if last is not None and last.height:
        for r in last.filter(pl.col("fired")).sort("rule_id", "asset").to_dicts():
            try:
                inputs = json.loads(r.get("inputs") or "{}")
            except ValueError:
                inputs = {}
            out.append(
                {
                    "key": f"rule:{r['rule_id']}:{r['asset']}",
                    "kind": "rule",
                    "title": f"Rule {r['rule_id']} firing on {r['asset']}",
                    "body": f"Evaluated {r['ts']:%Y-%m-%d %H:%M} UTC. Inputs: {json.dumps(inputs, default=str)}. Thresholds: {r.get('thresholds')}. Pre-committed action per docs/indicators.md; nothing here is a forecast.",
                }
            )
    if cliffs:
        out.append(cliff_calendar(cliffs))
    rk = site_data / "risk.json"
    if rk.exists():
        try:
            v = json.loads(rk.read_text()).get("venue") or {}
        except (OSError, ValueError):
            v = {}
        for e in v.get("exposure", []):
            if e.get("breach"):
                out.append(
                    {
                        "key": f"venue:{e['venue']}",
                        "kind": "venue",
                        "title": f"Venue limit breached on the example book: {e['venue']}",
                        "body": f"Exposure {100 * e['share_nav']:.1f}% of NAV against a limit of {100 * e['limit_share_nav']:.1f}% (grade {e.get('grade', '?')}). The book is the illustrative one in config/book.yaml.",
                    }
                )
        low = v.get("low_score") or {}
        if low.get("breach"):
            out.append(
                {
                    "key": "venue:low-score-aggregate",
                    "kind": "venue",
                    "title": "Low-score venue aggregate above its limit (example book)",
                    "body": f"Low-score venues hold {100 * low['share_nav']:.1f}% of NAV against a limit of {100 * low['limit_share_nav']:.1f}%.",
                }
            )
    fs = archive.read_fetch_status()
    if fs is not None and fs.height:
        # last two runs per (job, dataset): both failed → condition
        # a geo-block (HTTP 451/403 from a GitHub runner) is a documented, expected outcome of
        # that runner, not an outage: it neither counts as a failure nor breaks a streak
        fs = fs.filter(~pl.col("reason").fill_null("").str.contains(r"HTTP (451|403)"))
        g = (
            fs.sort("ts")
            .group_by("job", "dataset")
            .agg(
                pl.col("ok").tail(2).alias("last2"),
                pl.col("reason").last().alias("reason"),
                pl.col("ts").last().alias("ts"),
            )
        )
        for r in g.sort("job", "dataset").to_dicts():
            if len(r["last2"]) >= 2 and not any(r["last2"]):
                out.append(
                    {
                        "key": f"dataset:{r['dataset']}",
                        "kind": "dataset",
                        "title": f"Dataset unavailable for more than one run: {r['dataset']} ({r['job']})",
                        "body": f"Last failure {r['ts']:%Y-%m-%d %H:%M} UTC: {r['reason']}. Tables that depend on it are stale (status page). Runbook: docs/runbook.md.",
                    }
                )
    return out


def _gh_available() -> bool:
    return (
        shutil.which("gh") is not None
        and bool(os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN"))
        and bool(_repo())
    )


def _repo() -> str | None:
    return os.environ.get("GITHUB_REPOSITORY") or os.environ.get("ALERTS_REPO")


def _gh(*args: str) -> str:
    return subprocess.run(
        ["gh", *args, "-R", _repo()], check=True, capture_output=True, text=True
    ).stdout


def cliff_calendar(cliffs: list[tuple[str, dict]]) -> dict:
    """One rolling condition for Rule 5.1 (work order 2, item 4): the qualifying cliffs of the
    next 30 days as a table. The key is constant, so the issue is reused and its body is
    refreshed when the list changes; the weekly review reads this list."""
    rows = sorted(cliffs, key=lambda c: (str(c[1].get("unlock_date") or ""), c[0]))
    lines = [
        "| token | cliff date | share of float | days of volume | USD |",
        "|---|---|---:|---:|---:|",
    ]
    for asset, i in rows:
        sh, dv, usd = (
            i.get("unlock_share_of_float"),
            i.get("unlock_days_of_volume"),
            i.get("unlock_usd"),
        )
        lines.append(
            f"| {asset} | {i.get('unlock_date', '?')} | {'' if sh is None else f'{100 * sh:.1f} %'} | {'' if dv is None else f'{dv:.1f}'} | {'' if usd is None else f'{usd / 1e6:,.1f} M'} |"
        )
    return {
        "key": "cliffs:calendar",
        "kind": "cliffs",
        "title": f"Cliff calendar, next 30 days ({len(rows)} qualifying)",
        "body": "Scheduled cliffs meeting either Rule 5.1 leg (share of float > 1 % or > 2 days of real volume). The 2021–2026 backfill puts the 2026 pre-cliff drift at the base rate (docs/notes/pre_unlock_drift.md), so this list is informational.\n\n"
        + "\n".join(lines),
    }


def _issue_body(c: dict) -> str:
    return f"{c['body']}\n\nPanel: {SITE_URL}{PANEL.get(c['kind'], '')}\n\nThis issue closes automatically when the condition clears.\n\n<!-- alert-key: {c['key']} -->"


def _open_issues() -> dict[str, int]:
    """key → issue number for open issues carrying the alert label."""
    try:
        raw = _gh(
            "issue",
            "list",
            "--label",
            LABEL,
            "--state",
            "open",
            "--limit",
            "200",
            "--json",
            "number,body",
        )
    except subprocess.CalledProcessError as e:
        log.warning("gh issue list failed: %s", e.stderr[:200])
        return {}
    out = {}
    for it in json.loads(raw or "[]"):
        body = it.get("body") or ""
        if "<!-- alert-key:" in body:
            key = body.split("<!-- alert-key:", 1)[1].split("-->", 1)[0].strip()
            out[key] = it["number"]
    return out


def sync(site_out: Path = SITE, dry_run: bool | None = None) -> dict:
    """Reconcile issues and the `alerts` table with the current conditions; write the feed."""
    now, sha = utc_now(), git_sha()
    conds = {c["key"]: c for c in current_conditions(site_out / "data")}
    live = dry_run is False or (dry_run is None and _gh_available())
    table = archive.read("alerts")
    state: dict[str, dict] = {}
    if table is not None and table.height:
        for r in table.to_dicts():
            state[r["key"]] = r
    issues = _open_issues() if live else {}
    opened = closed = 0
    for key, c in conds.items():
        s = state.get(key)
        if s and s.get("closed_at") is None and (s.get("issue_number") or not live):
            if s.get("body") != c["body"]:  # the rolling calendar changed: refresh in place
                s["body"], s["title"] = c["body"], c["title"]
                if live and s.get("issue_number"):
                    with contextlib.suppress(subprocess.CalledProcessError):
                        _gh(
                            "issue",
                            "edit",
                            str(s["issue_number"]),
                            "--title",
                            f"[alert] {c['title']}",
                            "--body",
                            _issue_body(c),
                        )
            continue  # already open, and it has its issue (or this is a dry run)
        num = issues.get(key) or (s.get("issue_number") if s else None)
        if live and num is None:
            body = _issue_body(c)
            try:
                _gh(
                    "label",
                    "create",
                    LABEL,
                    "--force",
                    "--color",
                    "B60205",
                    "--description",
                    "automatic monitor alert",
                )
                url = _gh(
                    "issue",
                    "create",
                    "--title",
                    f"[alert] {c['title']}",
                    "--body",
                    body,
                    "--label",
                    LABEL,
                ).strip()
                num = int(url.rstrip("/").rsplit("/", 1)[-1])
            except (subprocess.CalledProcessError, ValueError) as e:
                log.warning("issue create failed for %s: %s", key, e)
        if s and s.get("closed_at") is None:
            s["issue_number"] = num  # opened in a dry run; the issue exists now
            continue
        state[key] = {
            "key": key,
            "kind": c["kind"],
            "title": c["title"],
            "body": c["body"],
            "opened_at": now,
            "closed_at": None,
            "issue_number": num,
        }
        opened += 1
    if live:  # rows closed by an earlier (dry or failed) run whose issue is still open on GitHub
        for key, s in state.items():
            num = s.get("issue_number") or issues.get(key)
            if key not in conds and s.get("closed_at") is not None and num in issues.values():
                with contextlib.suppress(subprocess.CalledProcessError):
                    _gh(
                        "issue",
                        "close",
                        str(num),
                        "--comment",
                        "Condition no longer active (automatic, reconciled).",
                    )
    for key, s in state.items():
        if key in conds or s.get("closed_at") is not None:
            continue
        num = s.get("issue_number") or issues.get(key)
        if live and num:
            try:
                _gh(
                    "issue",
                    "close",
                    str(num),
                    "--comment",
                    (
                        "Superseded by the rolling cliff-calendar issue (one issue for Rule 5.1, not one per token)."
                        if key.startswith("rule:5.1:")
                        else f"Condition cleared at {now:%Y-%m-%d %H:%M} UTC (automatic)."
                    ),
                )
            except subprocess.CalledProcessError as e:
                log.warning("issue close failed for %s: %s", key, e.stderr[:200])
        s["closed_at"] = now
        closed += 1
    if state:
        df = pl.DataFrame(
            list(state.values()),
            schema={
                "key": pl.Utf8,
                "kind": pl.Utf8,
                "title": pl.Utf8,
                "body": pl.Utf8,
                "opened_at": pl.Datetime("us", "UTC"),
                "closed_at": pl.Datetime("us", "UTC"),
                "issue_number": pl.Int64,
            },
        ).with_columns(
            pl.lit("rule_fires+risk.json+fetch_status").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        )
        archive.upsert("alerts", df)
    write_rss(list(state.values()), site_out / "alerts.xml", now)
    return {"active": len(conds), "opened": opened, "closed": closed, "live": live}


def write_rss(items: list[dict], path: Path, now: datetime) -> Path:
    """RSS 2.0: active conditions first, then the last 50 closed ones, newest first."""
    items = sorted(
        items, key=lambda s: (s.get("closed_at") is not None, -(s["opened_at"].timestamp()))
    )
    active = [s for s in items if s.get("closed_at") is None]
    done = [s for s in items if s.get("closed_at") is not None][:50]
    xml = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0"><channel>',
        f"<title>crypto-monitor alerts</title><link>{SITE_URL}</link><description>Rule firings, venue-limit breaches and datasets unavailable for more than one run. Each item is a condition, opened when it appears and closed when it clears.</description>",
        f"<lastBuildDate>{format_datetime(now)}</lastBuildDate>",
    ]
    for s in active + done:
        status = (
            "ACTIVE"
            if s.get("closed_at") is None
            else f"cleared {s['closed_at']:%Y-%m-%d %H:%M} UTC"
        )
        link = SITE_URL + PANEL.get(s.get("kind", ""), "")
        xml.append(
            f'<item><title>{escape(s["title"])} [{status}]</title><link>{escape(link)}</link><guid isPermaLink="false">{escape(s["key"])}@{s["opened_at"]:%Y%m%d%H%M}</guid><pubDate>{format_datetime(s["opened_at"])}</pubDate><description>{escape(s.get("body") or "")}</description></item>'
        )
    xml.append("</channel></rss>")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(xml) + "\n")
    return path
