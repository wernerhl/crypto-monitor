"""Resistance/support conditional-probability model (work order 8): the frozen event definition,
the three causal reference levels, the break/reject/chop labels, the two objects (base-rate
direction + magnitude), and the honesty rails — the base rate publishes on full history while the
crowding/Φ overlay stays preliminary and the Λ/D magnitude claim reports insufficient sample."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from monitor.compute import resistance as rz

ROOT = Path(__file__).resolve().parents[2]


def _synth(base: str, closes: list[float], highs=None, lows=None, vols=None, start=date(2020, 1, 1)):
    n = len(closes)
    highs = highs or [c * 1.01 for c in closes]
    lows = lows or [c * 0.99 for c in closes]
    vols = vols or [1_000_000.0] * n
    return pl.DataFrame({
        "base": [base] * n,
        "date": [start + timedelta(days=i) for i in range(n)],
        "venue": ["binance"] * n,
        "open": closes, "high": highs, "low": lows, "close": closes,
        "volume_quote": vols,
    })


def test_params_frozen_and_have_calibrated_on():
    c = yaml.safe_load((ROOT / "config" / "resistance_model.yaml").read_text())
    assert str(c["calibrated_on"]) == "2026-09-19"
    assert c["event"]["proximity_band"] == 0.02
    assert c["event"]["labels"]["break_margin_m"] == 0.005
    # all three reference-level definitions are specified (never one picked after results)
    assert set(c["reference_levels"].keys()) == {"hi", "swing", "vp"}
    assert set(c["event"]["horizons_days"]) == {5, 20}


def test_reference_levels_are_causal_no_lookahead():
    # a spike on the LAST day must not raise the 90-day high used at earlier days
    closes = [100.0] * 100 + [500.0]
    df = _synth("BTC", closes)
    o = rz.canonical_ohlc(df, ["BTC"], ["binance"])
    h = o["high"].to_numpy()
    r_hi = rz._rolling_max_shift(h, 90)
    # R at the last index excludes the last bar (causal), so it never sees the spike itself
    assert r_hi[-1] < 200, "level at t must not include the bar at t"


def test_break_and_reject_labels():
    m, hold, react = 0.005, 2, 5
    R = 100.0
    # break: pop above 100.5 and hold two closes above 99.5 (length > react+hold to resolve)
    c = np.array([100.0, 101.0, 101.2, 101.5, 101.6, 101.7, 101.8, 102.0, 102.1, 102.2])
    lab, resolved = rz._label_side(rz.SIDE_RESISTANCE, 0, R, c, c * 0.99, c * 1.01, 98.0, m, hold, react)
    assert resolved and lab == "break"
    # rejection: fall back below 99.5 and print a lower low than the pre-test swing (98)
    c2 = np.array([100.0, 99.0, 96.0, 95.0, 94.0, 93.0, 92.0, 91.0, 90.0, 89.0])
    lab2, _ = rz._label_side(rz.SIDE_RESISTANCE, 0, R, c2, c2 * 0.97, c2 * 1.01, 98.0, m, hold, react)
    assert lab2 == "reject"


def _real_events():
    prices = pl.read_parquet(ROOT / "data" / "processed" / "prices_daily.parquet")
    c = rz.cfg()
    ev = rz.build_events(prices, c)
    ev = rz.attach_state(
        ev,
        pl.read_parquet(ROOT / "data" / "processed" / "positioning_history.parquet"),
        pl.read_parquet(ROOT / "data" / "processed" / "fragility_series.parquet"),
        pl.read_parquet(ROOT / "data" / "processed" / "positioning.parquet"),
    )
    return ev, c


def test_events_carry_three_definitions_and_both_sides():
    ev, _ = _real_events()
    assert set(ev["r_def"].unique().to_list()) == {"hi", "swing", "vp"}
    assert set(ev["side"].unique().to_list()) == {"resistance", "support"}
    # every label is one of the three outcomes
    labs = set(ev.filter(pl.col("resolved_5"))["label_5"].unique().to_list())
    assert labs <= {"break", "reject", "chop"}
    # the close-outcome conditioner is present
    assert "close_cleared" in ev.columns


def test_base_rate_publishes_and_size_asymmetry():
    ev, c = _real_events()
    cell = rz.fit_direction(ev, "resistance", "hi", 5, rz.BASE_REGRESSORS, c)
    assert cell["published"] is True and cell["n_events"] >= c["samples"]["min_events_publish"]
    assert cell["converged"] is True
    assert cell["calibration"], "an out-of-sample calibration curve is reported"
    mag = rz.magnitude_summary(ev, "resistance", "hi", 5)["by_direction"]
    # the whole thesis: the losing-side (reject) move is larger in magnitude than the break move
    assert abs(mag["reject"]["mean"]) > abs(mag["break"]["mean"])
    assert mag["reject"]["mean"] < 0  # a resistance rejection is a drawdown


def test_overlay_preliminary_and_lambda_claim_insufficient():
    ev, c = _real_events()
    cells = rz.model_cells(ev, c)
    for cell in cells:
        assert cell["overlay"]["preliminary"] is True
        lam = cell["lambda_over_depth_magnitude"]
        # liquidation depth is collector-era only: the Λ/D magnitude claim cannot be published yet
        assert not lam.get("published"), "Λ/D magnitude must not publish on a single-digit sample"


def test_probability_is_conditional_on_the_close():
    ev, c = _real_events()
    as_of = ev["date"].max()
    act = rz.active_tests(ev, as_of, c)
    # close_cleared is a regressor, and 'now' vs 'if the close clears' give different probabilities
    assert "close_cleared" in rz.BASE_REGRESSORS
    if act.height:
        for r in act.to_dicts():
            for _H, p in (r["probs"] or {}).items():
                now, clr = p.get("now", {}), p.get("if_clear", {})
                if now and clr:
                    assert now != clr
                    return


def test_no_threshold_added_and_no_trigger():
    # the sub-module changes no threshold and adds no rule/trigger (a hard "Do not")
    th = yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())
    assert "resistance" not in th and "resistance_model" not in th
    src = (ROOT / "src" / "monitor" / "compute" / "resistance.py").read_text()
    assert "rule_fires" not in src and "alerts" not in src
