"""Work order 2 (2026-09-08): five fresh sources give five components with sample sizes; a
null component with a fresh source is a build failure; the range-position component is
bounded and reads ~0 for a price just under the top of a narrow range."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl
import pytest

from monitor.compute import fragility as fr


def _series(start: date, n: int, fn) -> list[tuple[date, float]]:
    return [(start + timedelta(days=i), float(fn(i))) for i in range(n)]


def _df(rows, col):
    return pl.DataFrame({"date": [d for d, _ in rows], col: [v for _, v in rows]})


def test_all_five_fresh_sources_yield_five_components_with_sizes():
    start = date(2025, 1, 1)
    n = 400
    rng = np.random.default_rng(0)
    end = start + timedelta(days=n - 1)
    fund = _df(_series(start, n, lambda i: 0.1 + 0.05 * np.sin(i / 20)), "fr")
    oi = _df(_series(start, n, lambda i: 0.02 + 0.002 * np.cos(i / 15)), "oi_rel")
    vrp = _df(_series(start, n, lambda i: 0.05 * np.sin(i / 30)), "vrp")
    px = _df(
        _series(start, n, lambda i: 100 * np.exp(np.cumsum(rng.normal(0, 0.02, n))[i])), "close"
    )
    sc = _df(_series(start, n, lambda i: 0.01 + 0.005 * np.sin(i / 40)), "growth_30d")
    out = fr.build_series(fund, oi, vrp, px, sc, end, window=250, min_n=30)
    last = fr.last_row(out, end)
    assert last["n_components"] == 5
    for c in fr.COMPONENTS:
        assert last[c] is not None and last[f"{c}_n"] == 250
    assert -2.0 <= last["z_dd"] <= 2.0
    assert fr.check_components(last, {c: end for c in fr.COMPONENTS}, end) == []


def test_null_component_with_fresh_source_is_a_build_failure():
    end = date(2026, 9, 8)
    row = {"z_fr": 0.1, "z_oi": 0.2, "z_vrp_neg": None, "z_dd": 0.0, "z_sc_neg": -0.1}
    with pytest.raises(RuntimeError, match="z_vrp_neg"):
        fr.check_components(row, {"z_vrp_neg": end - timedelta(days=1)}, end)
    # a stale source is a documented gap, not a failure
    gaps = fr.check_components(row, {"z_vrp_neg": end - timedelta(days=3)}, end)
    assert gaps == [{"component": "z_vrp_neg", "reason": "dvol stale: last 2026-09-05"}]
    assert fr.check_components(row, {}, end)[0]["reason"] == "dvol has no rows"


def test_range_position_component_is_bounded_and_near_zero_in_a_narrow_range():
    start = date(2026, 1, 1)
    # 90 days: range 95..100, today's close 2.7 % below the high, near the middle of the range
    closes = [95.0 + 5.0 * (i % 2) for i in range(89)] + [100.0 * (1 - 0.027)]
    px = pl.DataFrame({"date": [start + timedelta(days=i) for i in range(90)], "close": closes})
    dd = fr._drawdown(px, window=90)
    pos = dd["pos"][-1]
    assert abs(pos - (97.3 - 95.0) / 5.0) < 1e-9
    z = fr.range_position_score(np.array([0.0, 0.5, 1.0, pos]))
    assert z[0] == -2.0 and z[1] == 0.0 and z[2] == 2.0 and -0.5 < z[3] < 1.0
    assert dd["dd"][-1] == pytest.approx(-0.027)
