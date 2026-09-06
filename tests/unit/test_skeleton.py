"""Phase-1 smoke tests: configs parse, thresholds are the only source of rule parameters,
the site renders with the build sha embedded, and the CLI answers."""

from __future__ import annotations

import datetime as dt

import yaml
from typer.testing import CliRunner

from monitor.cli import app
from monitor.paths import CONFIG, ROOT
from monitor.site.render import render


def _load(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def test_configs_parse():
    for name in (
        "universe.yaml",
        "sources.yaml",
        "thresholds.yaml",
        "venues.yaml",
        "book.yaml",
        "events.yaml",
        "protocol_map.yaml",
    ):
        assert isinstance(_load(name), dict), name


def test_thresholds_carry_calibration_metadata():
    t = _load("thresholds.yaml")
    assert isinstance(t["calibrated_on"], dt.date)
    assert t["recalibration_note"].strip()
    assert set(t["rules"]) == {"crowded_long", "capitulation", "vol_underpricing", "cliff", "gate"}


def test_sources_have_verification_dates():
    s = _load("sources.yaml")
    for name, entry in s["sources"].items():
        assert isinstance(entry.get("verified_on"), dt.date), name


def test_example_book_weights_are_shares_of_nav():
    b = _load("book.yaml")
    gross = sum(abs(p["weight"]) for p in b["positions"])
    assert 0 < gross <= 2.0


def test_site_renders_with_build_sha(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_SHA", "abc123")
    out = render(tmp_path)
    html = out.read_text()
    assert 'data-build-sha="abc123"' in html
    assert "not investment advice" in html
    assert "googletagmanager" not in html and "analytics" not in html.lower()


def test_cli_version():
    r = CliRunner().invoke(app, ["version"])
    assert r.exit_code == 0 and r.output.strip()


def test_no_env_file_committed():
    assert not (ROOT / ".env").exists() or ".env" in (ROOT / ".gitignore").read_text()
