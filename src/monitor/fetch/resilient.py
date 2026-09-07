"""Per-dataset failure isolation for the jobs (build prompt 0.8: honesty about gaps).

A source that is down, rate-limited or geo-blocked (Binance returns HTTP 451 from US
addresses, which is where GitHub-hosted runners live) must not sink the whole run: the
dataset is recorded as unavailable with the reason in the `fetch_status` table, the status
page shows it, and every table that depends on it goes stale instead of silently filling."""

from __future__ import annotations

import logging
from collections.abc import Callable
from datetime import datetime

import httpx
import polars as pl

from monitor import archive
from monitor.meta import git_sha, utc_now

log = logging.getLogger("monitor.fetch")
# only the aggregator markets dataset aborts a run; everything else (geo-blocked venues
# included) degrades to "unavailable this run" and the last good snapshot is used
CRITICAL = {"markets"}


class FetchRun:
    def __init__(self, job: str, ts: datetime | None = None) -> None:
        self.job = job
        self.ts = ts or utc_now()
        self.rows: list[dict] = []
        self.out: dict[str, str] = {}

    def run(self, name: str, fn: Callable[[], object], critical: bool = False) -> object | None:
        try:
            res = fn()
            self.out[name] = str(res) if res is not None else "skipped"
            self.rows.append(
                {"ts": self.ts, "job": self.job, "dataset": name, "ok": True, "reason": None}
            )
            return res
        except Exception as e:
            reason = _reason(e)
            log.warning("%s failed: %s", name, reason)
            self.out[name] = f"FAILED: {reason}"
            self.rows.append(
                {"ts": self.ts, "job": self.job, "dataset": name, "ok": False, "reason": reason}
            )
            if critical or name in CRITICAL:
                self.flush()
                raise
            return None

    def flush(self) -> None:
        if not self.rows:
            return
        df = pl.DataFrame(self.rows).with_columns(
            pl.lit("fetch").alias("source"),
            pl.lit(utc_now()).alias("fetched_at"),
            pl.lit(git_sha()).alias("git_sha"),
        )
        archive.upsert("fetch_status", df)
        self.rows = []


def _reason(e: Exception) -> str:
    if isinstance(e, httpx.HTTPStatusError):
        code = e.response.status_code
        host = e.request.url.host
        if code == 451:
            return f"HTTP 451 from {host}: geo-blocked for this runner's address (Binance blocks US IPs); dataset unavailable this run"
        return f"HTTP {code} from {host}"
    if isinstance(e, httpx.TransportError):
        return f"network error: {type(e).__name__}"
    return f"{type(e).__name__}: {str(e)[:200]}"
