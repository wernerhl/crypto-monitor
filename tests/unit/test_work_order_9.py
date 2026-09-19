"""Work order 9 — making the resistance estimates honest: competing-risks labelling (chop becomes
censoring), partial pooling, walk-forward recalibration, bootstrap bands, the consensus headline,
and the crowding overlay held preliminary. Everything walk-forward, causal, and clustered."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yaml

from monitor.compute import resistance as rz

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(scope="module")
def events():
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


@pytest.fixture(scope="module")
def model(events):
    ev, c = events
    return rz.run_model(ev, c, ev["date"].max(), date(2026, 9, 19)), c


def test_wo9_params_frozen():
    c = yaml.safe_load((ROOT / "config" / "resistance_model.yaml").read_text())
    assert c["competing_risks"]["max_window_days"] == 40
    assert c["competing_risks"]["cif_horizons"] == [5, 10, 20, 40]
    assert c["consensus"]["min_defs"] == 2
    assert c["recalibration"]["method"] == "isotonic"
    assert c["bootstrap"]["block"] == "week"


def test_chop_is_replaced_by_censoring(events):
    ev, c = events
    # the competing-risks outcome is only break / reject / censored — never a "chop" resolution
    assert set(ev["cause"].unique().to_list()) <= {"break", "reject", "censored"}
    assert {"cause", "ttr", "cens"} <= set(ev.columns)
    cif = rz.cif_empirical(ev.filter((pl.col("side") == "resistance") & (pl.col("r_def") == "hi")), c)
    assert set(cif["break"].keys()) == {5, 10, 20, 40}
    assert "chop" not in cif
    assert cif["median_ttr"]["break"] is not None


def test_time_to_event_is_causal_and_censors_end_of_data():
    # a test that never clears R*(1.005) nor falls below R*(0.995) is censored, not resolved
    c = np.array([100.0, 100.1, 100.2, 100.3, 100.4])
    cause, _ttr, cens = rz._time_to_events(rz.SIDE_RESISTANCE, 0, 100.0, c, c * 0.999, c * 1.001,
                                           99.8, 0.005, 2, 40)
    assert cens and cause == "censored"
    # and a clean break resolves with a finite time-to-event
    cb = np.array([100.0, 101.0, 101.2, 101.4, 101.6])
    cause2, ttr2, cens2 = rz._time_to_events(rz.SIDE_RESISTANCE, 0, 100.0, cb, cb * 0.99, cb * 1.01,
                                             98.0, 0.005, 2, 40)
    assert (not cens2) and cause2 == "break" and ttr2 >= 0


def test_consensus_needs_two_definitions(events):
    ev, c = events
    cons = rz.consensus_events(ev, c)
    assert cons.height and (cons["r_def"] == "consensus").all()
    # every consensus row corresponds to a (base, side, date) where >= 2 single defs fired
    single = ev.filter(pl.col("r_def").is_in(rz.R_DEFS))
    counts = single.group_by(["base", "side", "date"]).agg(pl.col("r_def").n_unique().alias("n"))
    ok = counts.filter(pl.col("n") >= 2).select("base", "side", "date")
    joined = cons.join(ok, on=["base", "side", "date"], how="inner")
    assert joined.height == cons.height


def test_pooled_hazard_single_model_and_cif_coherent(model):
    m, c = model
    assert m["pack"] is not None  # ONE partially-pooled model over all cells
    t = {"side": "resistance", "r_def": "hi", "dist": -0.01, "level_age_log": 3.0,
         "range_width": 0.3, "rv": 0.6, "close_cleared": 1, "btc_ret20": 0.05, "btc_dd90": -0.02}
    pr = rz.predict_cif_tests(m["pack"], [t], c)[0]
    hs = c["competing_risks"]["cif_horizons"]
    # cumulative incidence is non-decreasing in horizon and the two causes never exceed 1
    for i in range(1, len(hs)):
        assert pr["break"][hs[i]] >= pr["break"][hs[i - 1]] - 1e-9
    for h in hs:
        assert pr["break"][h] + pr["reject"][h] <= 1.0 + 1e-9


def test_recalibration_helps_and_raw_is_retained(model):
    m, _c = model
    prim = m["recal"]["primary_horizon"]
    b = m["recal"]["brier"][prim]
    # recalibration is no worse than raw, and the model beats the base rate at the primary horizon
    assert b["recal"] <= b["raw"] + 1e-9
    assert b["recal"] <= b["base_rate"] + 1e-9
    # the raw WO8 multinomial is kept for one release
    hi = next(x for x in m["cells"] if x["side"] == "resistance" and x["r_def"] == "hi")
    assert "raw_multinomial" in hi and hi["raw_multinomial"].get("base_rate")


def test_every_published_number_has_a_band(model):
    m, c = model
    for cell in m["cells"]:
        assert cell["band_break"] is not None
        for h in c["competing_risks"]["cif_horizons"]:
            lo, hi = cell["band_break"][h]
            assert lo <= hi  # a valid 16th-84th percentile band on every published number


def test_consensus_is_scored_against_definitions(model):
    m, _c = model
    bbd = m["recal"]["brier_by_def"]
    assert "consensus_days" in bbd and "non_consensus_days" in bbd
    assert m["headline_def"] in (*rz.R_DEFS, "consensus")


def test_tier3_overlay_stays_preliminary(model):
    m, c = model
    ov = rz.tier3_overlay(m["single"], c)
    assert ov["preliminary"] is True
    assert not ov["lambda_over_depth_magnitude"].get("published")  # n<=1 collector-era only
    assert "review" in ov["review_note"].lower()


def test_no_threshold_added_and_no_trigger():
    th = yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())
    assert "resistance" not in th and "resistance_model" not in th
    src = (ROOT / "src" / "monitor" / "compute" / "resistance.py").read_text()
    assert "rule_fires" not in src and "alerts" not in src
