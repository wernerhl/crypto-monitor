"""VRP history from DVOL, weekly realised vol, depth-aware tiering."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from monitor.backfill import vrp_history
from monitor.compute import state as state_mod
from monitor.compute import universe as univ


def test_vrp_history_matches_hand_value():
    d0 = date(2026, 1, 1)
    n = 45
    # constant 2 % daily moves -> RV30² = (365/30) × 30 × 0.0004 = 0.146; DVOL 50 -> IV² = 0.25 -> VRP 0.104
    close = 100 * np.exp(np.cumsum(np.full(n, 0.02)))
    prices = pl.DataFrame(
        {
            "date": [d0 + timedelta(days=i) for i in range(n)],
            "base": ["BTC"] * n,
            "close": close.tolist(),
            "volume_quote": [1.0] * n,
        }
    )
    dvol = pl.DataFrame(
        {
            "date": [d0 + timedelta(days=i) for i in range(n)],
            "currency": ["BTC"] * n,
            "dvol": [50.0] * n,
        }
    )
    v = vrp_history(dvol, prices, "BTC")
    assert v.height == n - 30 and v["vrp"][-1] == pytest.approx(0.25 - 0.146)


def test_realised_vol_weekly_blocks():
    r = np.full(21, 0.01)  # three exact weeks of 1 % moves -> vol = sqrt(1e-4 × 365)
    rv = state_mod.realised_vol_weekly(r)
    assert rv.shape == (3,) and rv[0] == pytest.approx(np.sqrt(1e-4 * 365))
    assert state_mod.realised_vol_weekly(np.full(5, 0.01)).size == 0


def test_tier_uses_measured_depth_and_marks_adv_basis():
    as_of = date(2026, 9, 6)
    from datetime import UTC, datetime

    ts = datetime(2026, 9, 6, tzinfo=UTC)
    import yaml

    from monitor.paths import CONFIG

    cfg = yaml.safe_load((CONFIG / "universe.yaml").read_text())
    cfg["candidate_universe"]["top_n_by_market_cap"] = 5
    markets = pl.DataFrame(
        [
            dict(
                as_of=as_of,
                id="a",
                symbol="A",
                name="A",
                rank=1,
                market_cap_usd=1e10,
                source="cg",
                fetched_at=ts,
                git_sha="t",
                excluded_reason=None,
                meta_status="categories",
            ),
            dict(
                as_of=as_of,
                id="b",
                symbol="B",
                name="B",
                rank=2,
                market_cap_usd=1e9,
                source="cg",
                fetched_at=ts,
                git_sha="t",
                excluded_reason=None,
                meta_status="categories",
            ),
        ]
    )
    sm = pl.DataFrame(
        {
            "id": ["a", "b"],
            "spot": [
                {
                    "binance": "AUSDT",
                    "bybit": "AUSDT",
                    "okx": None,
                    "coinbase": None,
                    "kraken": None,
                },
                {
                    "binance": "BUSDT",
                    "bybit": "BUSDT",
                    "okx": None,
                    "coinbase": None,
                    "kraken": None,
                },
            ],
            "perp": [
                {"binance": "AUSDT", "bybit": "AUSDT", "okx": "A-USDT-SWAP"},
                {"binance": "BUSDT", "bybit": "BUSDT", "okx": None},
            ],
            "note": [None, None],
        }
    )
    oi = pl.DataFrame({"id": ["a", "b"], "oi_median_usd": [5e8, 5e8], "oi_window_days": [30, 30]})
    adv = pl.DataFrame(
        {
            "id": ["a", "b"],
            "adv_30d_usd": [5e8, 5e8],
            "adv_window_days": [30, 30],
            "is_real": [True, False],
        }
    )
    # a: measured depth 1M < 3M threshold -> NOT tier 1 (falls to tier 2); b: no depth row -> pending -> tier 1
    depth = pl.DataFrame({"id": ["a"], "depth_2pct_usd": [1e6]})
    uni = univ.tier(
        markets, sm, oi, adv, cfg, as_of, depth=depth, source="cg", fetched_at=ts, git_sha="t"
    )
    r = {x["id"]: x for x in uni.to_dicts()}
    assert (
        r["a"]["tier"] == 2
        and r["a"]["depth_status"] == "measured"
        and r["a"]["adv_basis"] == "wash_filtered"
    )
    assert (
        r["b"]["tier"] == 1
        and r["b"]["depth_status"] == "pending_phase3"
        and r["b"]["adv_basis"] == "reported"
    )
