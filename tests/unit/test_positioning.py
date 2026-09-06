"""Positioning (notes Section 4): funding aggregation, robust z, quadrant, liquidation
structure, options metrics. Expected values worked by hand in the comments."""

from __future__ import annotations

import math
from datetime import UTC, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from monitor.compute import positioning as pos
from monitor.fetch.deribit import black76_delta


def test_aggregate_funding_oi_weighted_and_interval_annualised():
    ts = datetime(2026, 9, 6, 12, tzinfo=UTC)
    rows = [
        # binance: 0.01 % per 8h -> ann = 0.0001 × 3 × 365 = 0.1095; OI 300
        dict(
            ts=ts,
            base="X",
            venue="binance",
            funding_rate=0.0001,
            funding_interval_h=8.0,
            oi_usd=300.0,
        ),
        # bybit: 0.01 % per 4h -> ann = 0.0001 × 6 × 365 = 0.219; OI 100
        dict(
            ts=ts,
            base="X",
            venue="bybit",
            funding_rate=0.0001,
            funding_interval_h=4.0,
            oi_usd=100.0,
        ),
        # okx: no funding reported -> only counted in OI
        dict(
            ts=ts, base="X", venue="okx", funding_rate=None, funding_interval_h=None, oi_usd=600.0
        ),
    ]
    a = pos.aggregate_funding(pl.DataFrame(rows)).to_dicts()[0]
    # weighted: (0.1095×300 + 0.219×100)/400 = (32.85 + 21.9)/400 = 0.136875
    assert a["funding_ann"] == pytest.approx(0.136875)
    assert a["oi_usd"] == 1000.0 and a["n_venues"] == 3 and a["n_venues_funding"] == 2


def test_robust_z_hand_value():
    # history 1..41 (median 21, MAD 10 -> scaled 14.826); last value 51 -> z = 30/14.826
    x = np.array([*list(range(1, 42)), 51], dtype=float)
    z, n = pos.robust_z(x, window=90, min_n=30)
    assert n == 41 and z == pytest.approx(30 / 14.826)
    assert pos.robust_z(x[:20], 90)[0] is None  # too short
    assert pos.robust_z(np.array([5.0] * 40 + [6.0]), 90)[0] is None  # MAD = 0


def test_oi_relative_and_percentile_rank():
    assert pos.oi_relative(2e9, 1e11) == 0.02 and pos.oi_relative(None, 1e11) is None
    # last value 9 with history 0..9 -> nine of ten prior obs (0..8) are ≤ 9 -> rank 1.0; value 4.5 -> 5/10
    assert pos.percentile_rank(np.array([*list(range(10)), 9.0]), 90) == 1.0
    assert pos.percentile_rank(np.array([*list(range(10)), 4.5]), 90) == 0.5


def test_quadrant_and_slope():
    close = np.array([100, 101, 102, 103, 104, 105, 110.0])
    oi = np.array([10, 10, 10, 10, 10, 10, 12.0])
    q = pos.oi_price_quadrant(close, oi, d=1, slope_window=5)
    assert q["quadrant"] == "new longs" and q["dlogp"] == pytest.approx(math.log(110 / 105))
    q = pos.oi_price_quadrant(close, np.array([10, 10, 10, 10, 10, 10, 8.0]), d=1)
    assert q["quadrant"] == "short covering"
    # slope: Δlog OI = 2 Δlog P exactly -> b = 2
    lp = np.cumsum(np.random.default_rng(1).normal(0, 0.01, 40))
    close = 100 * np.exp(lp)
    oi = 10 * np.exp(2 * lp)
    assert pos.oi_price_quadrant(close, oi, d=5, slope_window=20)["slope_20d"] == pytest.approx(
        2.0, abs=1e-9
    )


def test_liquidation_prices_and_mass():
    # eq 4.3 with P0 = 100, ℓ = 10, m = 0.005: long liq 100(1 − 0.1 + 0.005) = 90.5; short 109.5
    assert pos.liquidation_prices(100.0, 10.0, 0.005) == (pytest.approx(90.5), pytest.approx(109.5))
    atoms = pos.liquidation_density(
        [(100.0, 1000.0)], {10.0: 0.5, 20.0: 0.5}, 0.005, long_share=0.6
    )
    # longs: 600 split 300 @ 90.5 (ℓ=10) and 300 @ 95.5 (ℓ=20); shorts: 400 -> 200 @ 109.5, 200 @ 104.5
    lam = pos.liquidation_mass(
        atoms, p_t=100.0, sigma_d=0.03, kappa=2.0, h_days=1.0
    )  # band ±6 % -> [94, 106]
    assert lam["lambda_minus"] == pytest.approx(
        300.0
    )  # only the ℓ=20 longs at 95.5 are inside [94, 100]
    assert lam["lambda_plus"] == pytest.approx(200.0)  # ℓ=20 shorts at 104.5 inside [100, 106]
    assert lam["asymmetry"] == pytest.approx(100.0)


def test_oi_entries_scaled_to_current_oi():
    d0 = datetime(2026, 9, 1, tzinfo=UTC).date()
    df = pl.DataFrame(
        {
            "date": [d0 + timedelta(days=i) for i in range(4)],
            "close": [100.0, 110.0, 105.0, 120.0],
            "oi_usd": [1000.0, 1300.0, 1200.0, 1500.0],
        }
    )
    e = pos.oi_entries_from_history(df)
    # increments: +300 @110, −100 (ignored), +300 @120; total 600 scaled to current 1500 -> 750 each
    assert e == [(110.0, pytest.approx(750.0)), (120.0, pytest.approx(750.0))]


def test_black76_delta_matches_deribit_greeks_fixture():
    # BTC-30OCT26-54000-C on 2026-09-06: mark_iv 55.07, F 80344.97, T ≈ 54 days, Deribit delta 0.97619
    t = (
        datetime(2026, 10, 30, 8, tzinfo=UTC) - datetime(2026, 9, 6, 6, 25, tzinfo=UTC)
    ).total_seconds() / (365 * 86400)
    d = black76_delta(80344.97, 54000.0, t, 0.5507, True)
    assert d == pytest.approx(0.97619, abs=0.01)
    assert black76_delta(100.0, 100.0, 1.0, 0.5, False) == pytest.approx(
        black76_delta(100.0, 100.0, 1.0, 0.5, True) - 1.0
    )


def test_realised_var_and_options_metrics():
    r = np.full(40, 0.02)  # 2 % daily moves -> RV² = 365/30 × 30 × 0.0004 = 0.146
    assert pos.realised_var_30(r) == pytest.approx(0.146)
    ts = datetime(2026, 9, 6, tzinfo=UTC)
    rows = []
    for days, iv in ((7, 40.0), (30, 50.0), (90, 60.0)):
        exp = ts + timedelta(days=days)
        t = days / 365
        for k in (80.0, 90.0, 100.0, 110.0, 120.0):
            for typ in ("call", "put"):
                delta = black76_delta(100.0, k, t, iv / 100, typ == "call")
                rows.append(
                    dict(
                        ts=ts,
                        currency="X",
                        instrument=f"X-{days}-{k}-{typ[0]}",
                        expiry=exp,
                        strike=k,
                        option_type=typ,
                        mark_iv=iv,
                        open_interest=1.0 + k / 100,
                        underlying_price=100.0,
                        mark_price=0.1,
                        delta_est=delta,
                        t_years=t,
                    )
                )
    chain = pl.DataFrame(rows)
    m = pos.options_metrics(chain, r)
    # flat smile per expiry -> ATM and 25Δ IVs equal the expiry IV; term points interpolate exactly
    assert (
        m["iv_1w"] == pytest.approx(0.40)
        and m["iv_1m"] == pytest.approx(0.50)
        and m["iv_3m"] == pytest.approx(0.60)
    )
    assert m["term_slope"] == pytest.approx(0.20)
    assert m["rr25_1m"] == pytest.approx(0.0, abs=1e-9)
    assert m["vrp"] == pytest.approx(0.25 - 0.146)
    assert m["n_expiries"] == 3 and m["largest_oi"][0]["max_oi_strike"] == 120.0
