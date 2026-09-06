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
