"""Trade structures, venue scoring, stress covariance / ES / scenarios, cross-section screens."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl
import pytest

from monitor.compute import crosssection as xs
from monitor.compute import trades as tr
from monitor.compute import venue as ven
from monitor.stress import covariance as cov
from monitor.stress import es as es_mod
from monitor.stress import scenarios as sc

NOW = datetime(2026, 9, 6, tzinfo=UTC)


def test_annualised_basis_and_table():
    # F = 82 000, P = 80 000, 73 days -> (0.025) × 365/73 = 0.125
    exp = NOW + timedelta(days=73)
    assert tr.annualised_basis(82_000, 80_000, exp, NOW) == pytest.approx(0.125)
    marks = pl.DataFrame(
        [
            dict(
                venue="deribit",
                symbol="BTC-18NOV26",
                base="BTC",
                expiry=exp,
                mark_price=82_000.0,
                index_price=80_000.0,
                settle_ccy="BTC",
                source="deribit",
            )
        ]
    )
    t = tr.basis_table(
        marks,
        {"BTC": 80_000.0},
        NOW,
        {"deribit": {"taker": 0.0003}},
        {"deribit": "C"},
        stable_borrow=0.06,
    )
    r = t.to_dicts()[0]
    # cost = 0.06 + 4 × 0.0003 × 365/73 = 0.06 + 0.006 = 0.066 -> net 0.059
    assert (
        r["gross_ann"] == pytest.approx(0.125)
        and r["cost_ann"] == pytest.approx(0.066)
        and r["net_ann"] == pytest.approx(0.059)
    )
    assert r["venue_score"] == "C" and "counterparty" in r["dominant_risk"]


def test_funding_carry_shrinks_trailing_mean():
    d0 = date(2026, 8, 1)
    fd = pl.DataFrame(
        {
            "date": [d0 + timedelta(days=k) for k in range(30)],
            "base": ["BTC"] * 30,
            "funding_ann": [0.10] * 30,
        }
    )
    t = tr.funding_carry(
        fd,
        date(2026, 9, 6),
        30,
        0.5,
        {"binance": {"taker": 0.0005}},
        {"binance": "C"},
        {"BTC": "binance"},
        0.06,
        {"BTC": 0.4},
    )
    r = t.to_dicts()[0]
    # gross = 0.5 × 0.10 = 0.05; cost = 0.06 + 4 × 0.0005 × 365/30 = 0.06 + 0.02433 = 0.08433
    assert r["gross_ann"] == pytest.approx(0.05) and r["cost_ann"] == pytest.approx(
        0.06 + 4 * 0.0005 * 365 / 30
    )


def test_vol_premium_has_no_gate_and_states_the_driver():
    """Review decision 2 (2026-09-08): the vol-selling row is present whatever the VRP sign;
    a negative premium is described by its driver instead of forbidden."""
    om = [{"currency": "BTC", "vrp": -0.05, "iv_1m": 0.5, "rv30_var": 0.3}]
    df = tr.vol_premium(
        om,
        1.5,
        {"BTC": {"driver": "complacency", "text": "implied vol at the 20th percentile"}},
        {"deribit": "B"},
    )
    assert df.height == 1
    risk = df["dominant_risk"][0]
    assert "FORBIDDEN" not in risk and "driver: implied vol at the 20th percentile" in risk
    df2 = tr.vol_premium(om, 0.2, {}, {"deribit": "B"})
    assert df2.height == 1 and "driver: unclassified" in df2["dominant_risk"][0]


def test_venue_score_and_limits():
    cfg = {
        "reviewed_on": date(2026, 9, 6),
        "score_weights": {
            "licensing": 2,
            "segregation": 2,
            "proof_of_reserves": 2,
            "withdrawal_history": 2,
            "wash_share": 1,
            "own_token_concentration": 1,
        },
        "exposure_limit_by_score": {"A": 0.4, "B": 0.25, "C": 0.1, "D": 0.0},
        "low_score_aggregate_limit": 0.15,
        "low_score_threshold": "C",
        "venues": {
            "good": {
                "licensing": "full",
                "segregation": "regulated",
                "proof_of_reserves": "audited_financials",
                "withdrawal_halts_last_3y": 0,
                "own_token_concentration": "none",
            },
            "bad": {
                "licensing": "none",
                "segregation": "none",
                "proof_of_reserves": "none",
                "withdrawal_halts_last_3y": 1,
                "own_token_concentration": "high",
            },
        },
    }
    v = ven.venue_table(cfg, {"bad": 0.5})
    r = {x["venue"]: x for x in v.to_dicts()}
    # good: 4+4+4+4+1+1 = 18 of 18 -> A; bad: 0+0+0+0+0+0 = 0 -> D
    assert (
        r["good"]["score_points"] == 18 and r["good"]["grade"] == "A" and r["bad"]["grade"] == "D"
    )
    book = {
        "nav_usd": 1_000_000,
        "positions": [
            {"asset": "x", "weight": 0.3, "venue": "good"},
            {"asset": "y", "weight": -0.05, "venue": "bad"},
        ],
        "collateral": [{"stablecoin": "USDT", "venue": "bad", "usd": 100_000}],
    }
    e = ven.exposure_table(book, v)
    ex = {x["venue"]: x for x in e.to_dicts()}
    assert (
        ex["bad"]["exposure_usd"] == 150_000
        and ex["bad"]["breach"] is True
        and ex["good"]["breach"] is False
    )
    low = ven.low_score_aggregate(e, cfg, book["nav_usd"])
    assert low["share_nav"] == pytest.approx(0.15) and low["breach"] is False


def test_effective_bets_matches_proposition_2_1():
    # equal weights, equal variance, common correlation 0.5, N = 4 -> 1/(0.5 + 0.5/4) = 1.6
    n, rho = 4, 0.5
    sigma = np.full((n, n), rho) + np.eye(n) * (1 - rho)
    w = np.full(n, 0.25)
    assert cov.effective_bets(w, sigma) == pytest.approx(1.6)
    assert cov.effective_bets(np.array([1.0, 0, 0, 0]), sigma) == pytest.approx(1.0)


def test_stress_covariance_tilt_and_fallbacks():
    hi, lo = np.array([[4.0]]), np.array([[1.0]])
    s, note = cov.stress_covariance(hi, lo, xi_high=0.5, phi=1.0, eta=0.5)
    # 0.5×4 + 0.5×1 + 0.5×1×(4−1) = 2.5 + 1.5 = 4.0
    assert s[0, 0] == pytest.approx(4.0) and note == "ok"
    s, note = cov.stress_covariance(None, lo, 0.5, 2.0, 0.5)
    assert s[0, 0] == 1.0 and "low-state" in note
    r = np.random.default_rng(0).normal(0, 0.02, (300, 3))
    lw = cov.weighted_ledoit_wolf(r, np.ones(300))
    assert lw.shape == (3, 3) and abs(lw[0, 0] - 0.0004) < 0.0002
    assert (
        cov.weighted_ledoit_wolf(r, np.r_[np.ones(10), np.zeros(290)]) is None
    )  # too few effective obs


def test_es_simulation_scales_with_vol():
    sigma = np.diag([0.02**2])
    a = es_mod.simulate_es(np.array([1.0]), sigma, df=4, n=50_000)
    b = es_mod.simulate_es(np.array([1.0]), 4 * sigma, df=4, n=50_000)
    assert a["es"] > a["var"] > 0 and b["es"] == pytest.approx(2 * a["es"], rel=0.1)


def test_cascade_and_halt_scenarios():
    positions = [
        {"asset_symbol": "BTC", "weight": 0.3, "venue": "v"},
        {"asset_symbol": "ETH", "weight": 0.1, "venue": "v"},
    ]
    c = sc.cascade_pnl(positions, {"BTC": 4e8}, {"BTC": 1e8}, {"BTC": 0.03}, nav=1e6, kappa=2.0)
    # move = −min(0.06, 0.02 × sqrt(4)) = −0.04 -> BTC pnl −0.012; ETH unavailable
    assert (
        c["rows"][0]["move"] == pytest.approx(-0.04)
        and c["total_share_nav"] == pytest.approx(-0.012)
        and c["rows"][1]["move"] is None
    )
    h = sc.venue_halt_pnl({"v": 400_000.0}, recovery=0.4, nav=1e6)
    assert h[0]["pnl_share_nav"] == pytest.approx(-0.24)
    s = sc.systemic_pnl(
        positions,
        [{"name": "USDT", "kind": "stablecoin"}],
        [{"stablecoin": "USDT", "venue": "v", "usd": 200_000}],
        nav=1e6,
    )
    assert s[0]["pnl_share_nav"] == pytest.approx(-0.1)


def test_sector_standardise_and_ic():
    df = pl.DataFrame(
        {
            "id": list("abcdefgh"),
            "sector": ["L1"] * 4 + ["DEX"] * 3 + ["tiny"],
            "x": [1.0, 2.0, 3.0, 40.0, 10.0, 20.0, 30.0, 5.0],
        }
    )
    z = xs.sector_standardise(df, "x")
    r = dict(zip(z["id"], z["x_z"], strict=True))
    # L1: median 2.5, MAD = median(|1−2.5|,|2−2.5|,|3−2.5|,|40−2.5|) = median(1.5,0.5,0.5,37.5) = 1.0 -> z(b) = −0.5/1.4826
    assert r["b"] == pytest.approx(-0.5 / 1.4826) and r["d"] == 3.0  # winsorised
    assert z.filter(pl.col("id") == "h")["x_z_note"][0] == "sector too small: cross-section z"
    weeks = [date(2026, 1, 5) + timedelta(weeks=k) for k in range(10)]
    rows, nxt = [], []
    for w in weeks:
        for i, v in enumerate([1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0]):
            rows.append({"id": str(i), "week": w, "s": v})
            nxt.append(
                {"id": str(i), "week": w, "ret_next": v * 0.01}
            )  # perfectly monotone -> IC 1
    ic = xs.spearman_ic(pl.DataFrame(rows), pl.DataFrame(nxt), "s")
    assert (
        ic["ic"] == pytest.approx(1.0) and ic["n_weeks"] == 10 and ic["ic_se"] == pytest.approx(0.0)
    )


def test_ew_weights_half_life():
    w = xs.ew_weights(17, half_life=8.0)
    assert w[-1] / w[-9] == pytest.approx(2.0) and w.sum() == pytest.approx(1.0)
