"""Work order 10 — post-mortem of the 15–21 Sep BTC run: trend state as a first-class layer,
demand-side flows, reading reconciliation, two-sided convexity, analyst discipline, and the
freshness debt (positioning/OI history roll-forward with a fail-not-publish lag guard)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yaml

from monitor.compute import demand as dm
from monitor.compute import trend as tr
from monitor.compute.reading import reading_text, state_reading

ROOT = Path(__file__).resolve().parents[2]


# ---------------------------------------------------------------- §1 trend state
def test_trend_rule_frozen_and_states_valid():
    c = yaml.safe_load((ROOT / "config" / "trend_state.yaml").read_text())
    assert str(c["calibrated_on"]) == "2026-09-26"
    assert c["ma_windows"] == [20, 50, 200]
    prices = pl.read_parquet(ROOT / "data" / "processed" / "prices_daily.parquet")
    st = tr.states(prices, c)
    assert st and all(s["state"] in tr.STATES for s in st)


def test_continuation_base_rate_and_phi_interaction():
    c = tr.cfg()
    prices = pl.read_parquet(ROOT / "data" / "processed" / "prices_daily.parquet")
    br = tr.continuation_base_rate(prices, c)
    for s in tr.STATES:
        cell = br["by_state"][s]
        assert cell["n"] > 0 and 0.0 <= cell["p_up"] <= 1.0 and cell["se"] is not None
    pt = tr.phi_by_trend(prices, pl.read_parquet(ROOT / "data" / "processed" / "fragility_series.parquet"), c)
    # the interaction is reported (Φ conditioned on trend), descriptive
    assert "UPTREND" in pt["by_state"]


def test_trend_state_uptrend_downtrend_rule():
    c = tr.cfg()
    up = np.linspace(100, 300, 400)          # steady rise → uptrend
    s_up = tr._series(up, c)["state"][-1]
    dn = np.linspace(300, 100, 400)          # steady fall → downtrend
    s_dn = tr._series(dn, c)["state"][-1]
    assert s_up == "UPTREND" and s_dn == "DOWNTREND"


# ---------------------------------------------------------------- §2 demand flows
def test_demand_block_computes_premium_and_flow_and_marks_unavailable():
    prices = pl.read_parquet(ROOT / "data" / "processed" / "prices_daily.parquet")
    onchain = pl.read_parquet(ROOT / "data" / "processed" / "onchain.parquet")
    b = dm.block(prices, onchain)
    assert b["coinbase_premium"] and all("premium_z" in p for p in b["coinbase_premium"])
    assert b["exchange_net_flow"] and all("net_flow_7d_usd" in f for f in b["exchange_net_flow"])
    # ETF flows and stablecoin inflow are named but NOT fetched (no scraping)
    assert b["etf_flow"]["available"] is False
    assert b["stablecoin_inflow_sif"]["available"] is False


# ---------------------------------------------------------------- §3/§4/§5 reading
def _phi_reading(phi, trend_state="UPTREND", active=None, conflicts=None, demand=None):
    frag = {"phi": phi, "n_components": 5, "z_vrp_neg": 1.0, "z_dd": 0.5}
    return state_reading(
        date(2026, 9, 19), frag, [{"currency": "BTC", "vrp": -0.05}], [], {"oi_rel_pctile": 0.99, "oi_rel_pctile_n": 250},
        -0.03, -0.3, 0.01, [], [], False,
        trend_btc={"state": trend_state}, trend_continuation={"by_state": {"UPTREND": {"p_up": 0.52}}},
        active_tests=active or [], conflicts=conflicts, demand=demand,
    )


def test_phi_gt_one_carries_no_direction_disclaimer():
    txt = reading_text(_phi_reading(1.3))
    assert "carries no directional information" in txt
    assert "carries no directional information" not in reading_text(_phi_reading(0.4))


def test_two_sided_convexity_named_by_trend():
    up = reading_text(_phi_reading(0.5, "UPTREND"))
    dn = reading_text(_phi_reading(0.5, "DOWNTREND"))
    assert "cheap in both directions" in up
    assert "upside (call) convexity" in up and "downside (put) convexity" in dn


def test_leverage_reading_conditional_on_trend():
    txt = reading_text(_phi_reading(0.5, "UPTREND"))
    assert "UPTREND" in txt and "continuation base rate" in txt
    assert "close" in txt.lower()  # §5 price statements say daily close


def test_reading_conflict_logged_when_levered_vs_break():
    conflicts: list = []
    active = [{"base": "BTC", "side": "resistance", "r_def": "hi", "close_cleared": False,
               "cif_break": {"20": 0.67}, "cif_reject": {"20": 0.20}}]
    reading_text(_phi_reading(1.3, "UPTREND", active=active, conflicts=conflicts))
    assert conflicts and conflicts[0]["base"] == "BTC"  # levered Φ vs break-leaning model


def test_demand_appears_in_reading():
    demand = {"coinbase_premium": [{"base": "BTC", "premium_z": 1.5}],
              "exchange_net_flow": [{"base": "BTC", "net_flow_7d_usd": 4e9}]}
    txt = reading_text(_phi_reading(0.5, demand=demand))
    assert "Demand:" in txt and "outflow" in txt


def test_runbook_and_notes_have_discipline_rule():
    rb = (ROOT / "docs" / "runbook.md").read_text()
    assert "mechanism-only judgment; not supported by the system's tests" in rb
    assert (ROOT / "docs" / "notes" / "postmortem_2026-09-run.md").exists()


# ---------------------------------------------------------------- §6 freshness debt
def test_history_tables_are_owned_by_hourly_with_a_parent():
    fr = yaml.safe_load((ROOT / "config" / "freshness.yaml").read_text())["tables"]
    for t, parent in (("positioning_history", "positioning"), ("oi_history", "oi_daily")):
        assert fr[t]["job"] == "hourly" and fr[t]["parent"] == parent
        assert not fr[t].get("optional")  # a real lag must be fatal now
    jw = yaml.safe_load((ROOT / "config" / "job_writes.yaml").read_text())
    assert "positioning_history" in jw["hourly"]["tables"] and "oi_history" in jw["hourly"]["tables"]


def test_lag_guard_raises_when_history_lags_live(tmp_path, monkeypatch):
    from monitor import archive
    from monitor import jobs_hourly as hourly

    monkeypatch.setattr(archive, "PROCESSED", tmp_path)
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "arch")
    now = datetime(2026, 9, 26, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    # live OI is current, history is two days behind → the guard must refuse to publish
    archive.upsert("oi_daily", pl.DataFrame({"date": [date(2026, 9, 26)], "base": ["BTC"],
                                             "oi_usd": [1.0], "suspect": [False], **{k: [v] for k, v in prov.items()}}))
    archive.upsert("oi_history", pl.DataFrame({"date": [date(2026, 9, 24)], "venue": ["aggregate"],
                                              "base": ["BTC"], "oi_usd": [1.0], **{k: [v] for k, v in prov.items()}}))
    with pytest.raises(RuntimeError, match="lags"):
        hourly._assert_history_current()


def test_no_threshold_or_phi_change():
    th = yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())
    assert "trend" not in th and "trend_state" not in th
    # trend module reads no thresholds config and writes no rule/trigger table
    src = (ROOT / "src" / "monitor" / "compute" / "trend.py").read_text()
    assert "thresholds.yaml" not in src and "rule_fires" not in src
