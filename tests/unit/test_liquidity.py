"""Liquidity gate (notes Section 7) on hand-built fixtures; expected values worked by hand."""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl
import pytest

from monitor.compute import liquidity as liq


def test_depth_from_levels_two_percent_band_and_coverage():
    # mid = (100 + 102)/2 = 101; band [98.98, 103.02]
    bids = [(100.0, 1.0), (99.0, 2.0), (98.0, 10.0)]  # 98 is outside the band
    asks = [(102.0, 1.0), (103.0, 3.0), (104.0, 10.0)]  # 104 outside
    d = liq.depth_from_levels(bids, asks, 0.02)
    assert d["mid"] == 101.0
    assert d["bid_depth_usd"] == pytest.approx(100 * 1 + 99 * 2)  # 298
    assert d["ask_depth_usd"] == pytest.approx(102 * 1 + 103 * 3)  # 411
    assert d["depth_usd"] == pytest.approx(709)
    assert d["spread_bps"] == pytest.approx(2 / 101 * 1e4)
    # book reaches 98 (2.97 % below mid) and 104 (2.97 % above) -> not truncated
    assert d["coverage_bid_pct"] == pytest.approx((1 - 98 / 101) * 100) and not d["truncated"]
    # a book ending inside the band is truncated
    assert liq.depth_from_levels(bids[:2], asks, 0.02)["truncated"]


def test_benford_stats_uniform_first_digits_fail_and_benford_like_pass():
    # 900 trades with first digits uniform 1..9 -> far from Benford -> tiny p
    sizes = [float(f"{d}{i:03d}") for d in range(1, 10) for i in range(100)]
    r = liq.benford_stats(sizes)
    assert r["n"] == 900 and r["p"] < 1e-6 and abs(r["shares"][0] - 1 / 9) < 1e-9
    # exact Benford shares -> chi2 ≈ 0 -> p ≈ 1
    n = 10000
    sizes = []
    for d in range(1, 10):
        sizes += [float(d)] * round(n * math.log10(1 + 1 / d))
    r = liq.benford_stats(sizes)
    assert r["chi2"] < 1.0 and r["p"] > 0.99
    assert liq.benford_stats([1.0] * 10)["p"] is None  # too few trades


def test_impact_cost_and_dtl_formulas():
    # eq 7.2: s/2 + Y σ sqrt(Q/ADV): spread 10 bps -> 0.0005; Y=0.75, σ=0.04, Q/ADV=0.04 -> 0.75*0.04*0.2 = 0.006
    assert liq.impact_cost(4e5, 1e7, 0.04, 10.0, 0.75) == pytest.approx(0.0005 + 0.006)
    # eq 7.3: Q/(ρ ADV) = 4e5 / (0.1 × 1e7) = 0.4 days
    assert liq.days_to_liquidate(4e5, 1e7, 0.1) == pytest.approx(0.4)
    cfg = {
        "participation_rate": 0.1,
        "dtl_max_days": 3.0,
        "impact_cost_share_of_net_return_max": 0.25,
        "impact_Y": 0.75,
    }
    g = liq.gate(4e5, 1e7, 0.04, 10.0, expected_net_return=0.10, cfg=cfg)
    # round trip 2 × 0.0065 = 0.013 ≤ 0.25 × 0.10 -> passes
    assert g["pass"] and g["round_trip_cost"] == pytest.approx(0.013)
    # 4 days to liquidate -> fails on DTL
    assert not liq.gate(4e6, 1e7, 0.04, 10.0, None, cfg)["pass"]


def test_amihud_hand_value():
    d0 = date(2026, 9, 1)
    rows = [
        dict(base="X", date=d0 + timedelta(days=i), close=c, volume_quote=v)
        for i, (c, v) in enumerate([(100, 1e6), (110, 2e6), (99, 1e6)])
    ]
    r = liq.amihud(pl.DataFrame(rows))
    # |log(110/100)|/2e6 + |log(99/110)|/1e6, over the two days with a return
    exp = (abs(math.log(1.1)) / 2e6 + abs(math.log(0.9)) / 1e6) / 2
    assert r["amihud"][0] == pytest.approx(exp) and r["n"][0] == 2


def test_wash_filters_flags():
    d = date(2026, 9, 6)
    rows = [
        dict(
            date=d,
            base="X",
            venue="coinbase",
            volume_quote=1e6,
            depth_usd=1e5,
            benford_dev=0.10,
            vol_absret_corr=0.3,
        ),  # ratio 10 (reference)
        dict(
            date=d,
            base="X",
            venue="kraken",
            volume_quote=2e6,
            depth_usd=1e5,
            benford_dev=0.20,
            vol_absret_corr=0.2,
        ),  # ratio 20 (reference) -> ref median 15, dev median 0.15
        dict(
            date=d,
            base="X",
            venue="shady",
            volume_quote=1e8,
            depth_usd=1e5,
            benford_dev=0.90,
            vol_absret_corr=-0.1,
        ),  # ratio 1000 > 5×15; dev 0.9 > 3×0.15
        dict(
            date=d,
            base="X",
            venue="binance",
            volume_quote=5e6,
            depth_usd=1e5,
            benford_dev=None,
            vol_absret_corr=None,
        ),  # ratio 50 ≤ 75, others unknown
    ]
    w = liq.wash_filters(pl.DataFrame(rows))
    r = {x["venue"]: x for x in w.to_dicts()}
    assert (
        r["shady"]["vol_depth_fail"]
        and r["shady"]["benford_fail"]
        and r["shady"]["corr_fail"]
        and not r["shady"]["pass"]
    )
    assert r["binance"]["pass"] and r["binance"]["benford_fail"] is None
    assert r["coinbase"]["pass"] and r["kraken"]["pass"]


def test_aggregate_depth_intraday_median_and_lower_bound():
    from datetime import UTC, datetime

    t0 = datetime(2026, 9, 6, 1, tzinfo=UTC)
    rows = []
    for h, (a, b, trunc) in enumerate(
        [(100.0, 50.0, False), (120.0, 60.0, True), (80.0, 40.0, False)]
    ):
        rows.append(
            dict(
                ts=t0 + timedelta(hours=h), venue="binance", base="X", depth_usd=a, truncated=trunc
            )
        )
        rows.append(
            dict(ts=t0 + timedelta(hours=h), venue="okx", base="X", depth_usd=b, truncated=False)
        )
    agg = liq.aggregate_depth(
        pl.DataFrame(rows).with_columns(pl.col("ts").cast(pl.Datetime("us", "UTC")))
    )
    r = agg.to_dicts()[0]
    # snapshot sums 150, 180, 120 -> median 150; one truncated snapshot -> lower bound
    assert r["depth_2pct_usd"] == 150.0 and r["depth_lower_bound"] and r["n_snapshots"] == 3
