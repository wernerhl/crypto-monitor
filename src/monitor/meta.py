"""Provenance helpers: every computed value carries `source`, `fetched_at`, `git_sha`."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime

from monitor.paths import ROOT


def utc_now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def git_sha() -> str:
    """Current commit sha; `GIT_SHA` env wins (set by Actions), else `git rev-parse`."""
    env = os.environ.get("GIT_SHA")
    if env:
        return env
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return "unknown"
