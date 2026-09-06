"""Fragility index, net liquidity, vol-state model and the trigger rules."""

from __future__ import annotations

from datetime import UTC, datetime

import numpy as np
import pytest
import yaml

from monitor import rules as rules_mod
from monitor.compute import state as state_mod
from monitor.paths import CONFIG

TH = yaml.safe_load((CONFIG / "thresholds.yaml").read_text())["rules"]
TS = datetime(2026, 9, 6, tzinfo=UTC)


def test_net_liquidity_units():
    # WALCL 6,737,204 (millions) − TGA 967,935 (millions) − RRP 0.675 (billions) = 5768.594 bn
    assert state_mod.net_liquidity(6_737_204, 967_935, 0.675) == pytest.approx(5768.594)


def test_drawdown_from_high():
    assert state_mod.drawdown_from_high(np.array([100, 120, 90.0]), 90) == pytest.approx(-0.25)
    assert state_mod.drawdown_from_high(np.array([100, 120, 130.0]), 90) == 0.0


def test_fragility_index_mean_of_available_components():
    hist = np.array(list(range(1, 42)), dtype=float)
    series = {"z_fr": np.append(hist, 51.0), "z_oi": np.append(hist, 21.0)}  # z = 30/14.826 and 0
    z = state_mod.fragility_components(series, window=250, min_n=30)
    assert (
        z["z_fr"] == pytest.approx(30 / 14.826)
        and z["z_oi"] == pytest.approx(0.0)
        and z["z_dd"] is None
    )
    phi, n = state_mod.fragility_index(z)
    assert n == 2 and phi == pytest.approx(15 / 14.826)


def test_vol_state_model_on_synthetic_two_regime_series():
    rng = np.random.default_rng(0)
    n, s = 600, np.zeros(600, int)
    for i in range(1, n):
        s[i] = s[i - 1] if rng.random() < 0.95 else 1 - s[i - 1]
    x = np.zeros(n)
    for i in range(1, n):
        x[i] = [-1.0, 0.0][s[i]] * 0.5 + 0.5 * x[i - 1] + [0.2, 0.4][s[i]] * rng.standard_normal()
    r = state_mod.vol_state_model(x)
    assert r["status"] == "ok" and 0 <= r["p_high"] <= 1
    assert (
        r["expected_duration_high"] > 5
        and "ar.L1" in r["params"]
        and r["std_errors"]["ar.L1"] is not None
    )
    assert state_mod.vol_state_model(x[:100])["status"].startswith("insufficient")


def test_rules_fire_and_report_unavailable_inputs():
    f = rules_mod.crowded_long(
        "BTC", TS, z_fr=2.5, oi_rel_pctile=0.95, lambda_minus_2=2e8, depth_2pct=1e8, th=TH
    )
    assert f.fired and f.inputs["lambda_over_depth"] == 2.0 and f.thresholds["z_fr_min"] == 2.0
    assert rules_mod.crowded_long("BTC", TS, 2.5, 0.95, 5e7, 1e8, TH).fired is False  # ratio 0.5
    u = rules_mod.crowded_long("BTC", TS, None, 0.95, 5e7, 1e8, TH)
    assert u.fired is None and "z_fr" in u.note
    assert rules_mod.capitulation("ETH", TS, -2.5, -0.25, 0.97, TH).fired
    assert rules_mod.capitulation("ETH", TS, -2.5, -0.10, 0.97, TH).fired is False
    assert (
        rules_mod.vol_underpricing("BTC", TS, -0.01, 1.5, TH).fired
        and rules_mod.vol_underpricing("BTC", TS, 0.05, 1.5, TH).fired is False
    )
    assert (
        rules_mod.cliff("ARB", TS, 0.02, 0.5, TH, "2026-10-01").fired
        and rules_mod.cliff("ARB", TS, 0.005, 1.0, TH).fired is False
    )
    assert rules_mod.cliff("ARB", TS, None, None, TH).fired is None
    g = rules_mod.gate_rule(
        "X", TS, dtl_days=4.0, round_trip_cost=0.01, expected_net_return=0.1, th=TH
    )
    assert g.fired is True  # closed on DTL
    assert rules_mod.gate_rule("X", TS, 1.0, 0.03, 0.1, TH).fired is True  # 0.03 > 0.25 × 0.1
    assert rules_mod.gate_rule("X", TS, 1.0, 0.01, 0.1, TH).fired is False
