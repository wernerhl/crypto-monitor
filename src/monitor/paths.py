"""Repository paths, resolved relative to this file so the CLI works from any cwd."""

from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONFIG = ROOT / "config"
DATA = ROOT / "data"
RAW = DATA / "raw"
PROCESSED = DATA / "processed"
ARCHIVE = DATA / "archive"
SITE = ROOT / "site"
SITE_DATA = SITE / "data"
DOCS = ROOT / "docs"
TEMPLATES = Path(__file__).resolve().parent / "site" / "templates"


SYNCED_MARKERS = (
    "/Documents/",
    "/Desktop/",
    "/Downloads/",
    "/Library/CloudStorage/",
    "/Library/Mobile Documents/",
    "/Dropbox/",
    "/Google Drive/",
    "/OneDrive/",
)


def assert_not_synced(path: Path | None = None) -> None:
    """Refuse to write the archive from a clone inside a cloud-synced folder (work order 2,
    item 6): the sync agent throttles writes a hundredfold and tore parquet files twice on
    2026-09-08. Set MONITOR_ALLOW_SYNCED=1 to override knowingly."""
    import os

    p = str((path or ROOT).resolve()) + "/"
    if os.environ.get("MONITOR_ALLOW_SYNCED") == "1":
        return
    hit = next((m for m in SYNCED_MARKERS if m in p), None)
    if hit:
        raise SystemExit(
            f"refusing to run from {p.rstrip('/')}: it sits under a cloud-synced folder ({hit.strip('/')}). "
            "Use a clone outside synced folders (docs/runbook.md, 'Re-running by hand'); "
            "set MONITOR_ALLOW_SYNCED=1 to override."
        )
