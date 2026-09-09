"""Work order 4 (2026-09-08): descriptive state words (1), Rule 4.3 as a reading and the vol row
without a gate (2), corrected write sets (3), inert Coinglass extension (1)."""

from __future__ import annotations

from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl
import yaml

from monitor import rules as rules_mod
from monitor.compute import hitrates
from monitor.compute import trades as tr
from monitor.compute.reading import reading_text, state_reading

ROOT = Path(__file__).resolve().parents[2]
TH = {"vol_underpricing": {"vrp_max": 0.0, "phi_min": 1.0}}


def test_state_words_are_descriptive_and_43_is_not_a_firing_rule():
    frag = {
        "phi": 1.3,
        "n_components": 5,
        "z_fr": 0.1,
        "z_oi": 0.2,
        "z_vrp_neg": 2.0,
        "z_dd": 1.0,
        "z_sc_neg": 0.0,
    }
    rules = [
        {
            "rule_id": "4.3",
            "asset": "BTC",
            "fired": True,
            "status": "reading",
            "inputs": '{"driver_text": "implied vol at the 30th percentile"}',
        },
        {"rule_id": "4.1", "asset": "BTC", "fired": False, "status": "trigger"},
    ]
    seg = state_reading(
        date(2026, 9, 9),
        frag,
        [{"currency": "BTC", "vrp": -0.05}],
        [],
        None,
        None,
        None,
        None,
        rules,
        [],
        False,
        None,
        None,
    )
    text = reading_text(seg)
    assert "levered" in text and "fragile" not in text and "Rules firing" not in text
    assert "VRP negative (-0.050: implied vol at the 30th percentile)" in text
    seg2 = state_reading(
        date(2026, 9, 9),
        {**frag, "phi": -0.4},
        [],
        [],
        None,
        None,
        None,
        None,
        [],
        [],
        False,
        None,
        None,
    )
    assert "deleveraged" in reading_text(seg2)


def test_rule_43_status_reading_and_no_hit_rate_row():
    f = rules_mod.vol_underpricing("BTC", datetime(2026, 9, 9, tzinfo=UTC), -0.05, 1.2, TH)
    assert f.status == "reading" and f.fired is True
    assert "4.3" not in hitrates.HORIZONS and "5.1" not in hitrates.HORIZONS


def test_vol_premium_row_present_whatever_the_vrp_sign_with_driver_text():
    om = [
        {"currency": "BTC", "vrp": -0.08, "iv_1m": 0.40, "rv30_var": 0.25},
        {"currency": "ETH", "vrp": 0.03, "iv_1m": 0.60, "rv30_var": 0.30},
    ]
    drivers = {"BTC": {"driver": "post-shock", "text": "post-shock, RV at the 92nd percentile"}}
    df = tr.vol_premium(om, 1.4, drivers, {"deribit": "B"})
    assert df.height == 2
    btc = df.filter(pl.col("asset") == "BTC").to_dicts()[0]
    eth = df.filter(pl.col("asset") == "ETH").to_dicts()[0]
    assert "FORBIDDEN" not in btc["dominant_risk"] and "driver: post-shock" in btc["dominant_risk"]
    assert tr.POST_SHOCK_NOTE in btc["dominant_risk"]
    assert "driver" not in eth["dominant_risk"]


def test_alerts_ignore_readings(tmp_path, monkeypatch):
    from monitor import alerts, archive

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    (tmp_path / "site" / "data").mkdir(parents=True)
    now = datetime(2026, 9, 9, 1, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    archive.upsert(
        "rule_fires",
        pl.DataFrame(
            [
                {
                    "ts": now,
                    "rule_id": "4.3",
                    "asset": "BTC",
                    "fired": True,
                    "status": "reading",
                    "inputs": "{}",
                    "thresholds": "{}",
                    "note": None,
                    **prov,
                },
                {
                    "ts": now,
                    "rule_id": "4.2",
                    "asset": "BTC",
                    "fired": True,
                    "status": "trigger",
                    "inputs": "{}",
                    "thresholds": "{}",
                    "note": None,
                    **prov,
                },
            ]
        ),
    )
    keys = {c["key"] for c in alerts.current_conditions(tmp_path / "site" / "data")}
    assert keys == {"rule:4.2:BTC"}


def test_write_sets_match_the_work_order_4_assignment():
    cfg = yaml.safe_load((ROOT / "config" / "job_writes.yaml").read_text())
    assert (
        "trades_carry" in cfg["hourly"]["tables"]
        and "site/data/hourly.json" in cfg["hourly"]["site"]
    )
    for t in ("book_risk", "venue_scores", "trades_vol", "hit_rates", "cliff_calendar"):
        assert t in cfg["daily"]["tables"], t
    assert (
        "site/data/risk.json" in cfg["daily"]["site"]
        and "site/data/daily.json" in cfg["daily"]["site"]
    )
    for t in ("universe", "factor_betas", "screens", "cliff_study", "phi_shock_events"):
        assert t in cfg["weekly"]["tables"], t
    assert cfg["weekly"]["site"] == ["site/data/screens.json"]
    seen = {}
    for job, spec in cfg.items():
        for t in spec["tables"]:
            assert t not in seen or "backfill" in (job, seen[t]), f"{t} in {seen.get(t)} and {job}"
            seen.setdefault(t, job)


def test_coinglass_extension_is_inert_without_tables(tmp_path, monkeypatch):
    from monitor import archive
    from monitor.jobs_hourly import _extend_with_coinglass

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    fund = pl.DataFrame({"date": [date(2026, 1, 1)], "fr": [0.1]})
    oi = pl.DataFrame({"date": [date(2026, 1, 1)], "oi_rel": [0.02]})
    f2, o2 = _extend_with_coinglass(fund, oi, 1e12)
    assert f2.equals(fund) and o2.equals(oi)


def test_fragility_as_of_never_carries_a_daily_source_two_days():
    from monitor.jobs_hourly import _fragility_as_of

    px = pl.DataFrame({"date": [date(2026, 9, 7)], "base": ["BTC"], "close": [1.0]})
    assert _fragility_as_of(px, date(2026, 9, 9)) == date(2026, 9, 8)  # before the daily job
    px2 = pl.DataFrame({"date": [date(2026, 9, 8)], "base": ["BTC"], "close": [1.0]})
    assert _fragility_as_of(px2, date(2026, 9, 9)) == date(2026, 9, 9)  # after it
    assert _fragility_as_of(
        pl.DataFrame({"date": [], "base": [], "close": []}), date(2026, 9, 9)
    ) == date(2026, 9, 9)


def test_reading_gaps_are_separated_from_the_component_count():
    frag = {
        "phi": 0.1,
        "n_components": 3,
        "z_fr": 0.7,
        "z_oi": 0.2,
        "z_vrp_neg": None,
        "z_dd": None,
        "z_sc_neg": -0.8,
    }
    seg = state_reading(
        date(2026, 9, 9),
        frag,
        [],
        [],
        None,
        None,
        None,
        None,
        [],
        [],
        False,
        [{"component": "z_dd", "reason": "prices_daily stale: last 2026-09-07"}],
        None,
    )
    text = reading_text(seg)
    assert "components); component the 90-day range position unavailable" in text


def test_live_vrp_carries_the_realised_leg_one_day_for_todays_dvol(tmp_path, monkeypatch):
    from datetime import timedelta

    import numpy as np

    from monitor import archive
    from monitor.jobs_hourly import live_vrp_history

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    start = date(2026, 1, 1)
    n = 120
    dates = [start + timedelta(days=i) for i in range(n)]
    prices = pl.DataFrame(
        {
            "date": dates,
            "base": ["BTC"] * n,
            "close": list(100 + np.cumsum(np.random.default_rng(0).normal(0, 1, n))),
            "volume_quote": [1.0] * n,
        }
    )
    prov = {"source": "t", "fetched_at": datetime(2026, 5, 1, tzinfo=UTC), "git_sha": "x"}
    archive.upsert(
        "dvol_daily",
        pl.DataFrame(
            {
                "date": dates[:-1],
                "currency": ["BTC"] * (n - 1),
                "dvol": [50.0] * (n - 1),
                **{k: [v] * (n - 1) for k, v in prov.items()},
            }
        ),
    )
    today = dates[-1] + timedelta(days=1)
    archive.upsert(
        "dvol",
        pl.DataFrame(
            {
                "ts": [datetime.combine(today, datetime.min.time(), tzinfo=UTC)],
                "currency": ["BTC"],
                "dvol": [55.0],
                **{k: [v] for k, v in prov.items()},
            }
        ),
    )
    out = live_vrp_history(prices)
    b = out.filter(pl.col("currency") == "BTC").sort("date")
    assert b["date"][-1] == today and abs(b["iv30"][-1] - 0.55) < 1e-12
    lr = np.diff(np.log(prices["close"].to_numpy()[-31:]))
    assert (
        abs(b["rv30_var"][-1] - 365.0 / 30.0 * float(np.sum(lr**2))) < 1e-12
    )  # RV at the last close, carried one day
