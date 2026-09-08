"""Work order 2026-09-08: reverse carry (A6), cliff study (A8), state reading (C1), alerts (C2),
liquidation source label (B1)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import numpy as np
import polars as pl

from monitor.compute import cliff_study as cs
from monitor.compute import trades as tr
from monitor.compute.reading import reading_text, state_reading


def test_basis_table_shows_reverse_carry_when_the_basis_is_negative():
    now = datetime(2026, 9, 8, 12, tzinfo=UTC)
    exp = now + timedelta(days=30)
    marks = pl.DataFrame(
        {
            "base": ["BTC", "ETH"],
            "symbol": ["BTC-Q", "ETH-Q"],
            "venue": ["okx", "okx"],
            "expiry": [exp, exp],
            "mark_price": [101_000.0, 2_950.0],
            "source": ["okx", "okx"],
        }
    ).with_columns(pl.col("expiry").cast(pl.Datetime("us", "UTC")))
    out = tr.basis_table(
        marks,
        {"BTC": 100_000.0, "ETH": 3_000.0},
        now,
        {"okx": {"taker": 0.0005}},
        {"okx": "B"},
        0.05,
        {"ETH": 0.10, "BTC": 0.10},
    )
    d = {r["asset"]: r for r in out.to_dicts()}
    assert d["BTC"]["structure"] == "cash-and-carry basis" and d["BTC"]["gross_ann"] > 0
    assert d["ETH"]["structure"] == "reverse carry (backwardation)"
    assert d["ETH"]["gross_ann"] > 0 and abs(d["ETH"]["gross_ann"] - (0.05 / 3.0 * 365 / 30)) < 1e-9
    # a short perp pays positive funding: it is a cost of the reverse carry, not of the carry
    assert d["ETH"]["cost_ann"] > d["BTC"]["cost_ann"] - 0.05
    assert "squeeze" in d["ETH"]["dominant_risk"] and "short perp" in d["ETH"]["instrument"]


def _prices(bases, start, n, drift):
    rows = []
    for b, dr in zip(bases, drift, strict=True):
        p = 100.0
        for i in range(n):
            p *= 1 + dr
            rows.append(
                {
                    "date": start + timedelta(days=i),
                    "venue": "binance",
                    "base": b,
                    "close": p,
                    "volume_quote": 1_000_000.0,
                    "volume_base": None,
                }
            )
    return pl.DataFrame(rows)


def test_cliff_study_counts_hits_and_flags_bases():
    start = date(2025, 1, 1)
    prices = _prices(["AAA", "BBB", "BTC"], start, 120, [-0.01, 0.01, 0.0])  # AAA falls, BBB rises
    ev = pl.DataFrame(
        {
            "id": ["aaa", "aaa", "bbb", "bbb"],
            "date": [
                start + timedelta(days=60),
                start + timedelta(days=60),
                start + timedelta(days=70),
                start + timedelta(days=200),
            ],
            "kind": ["cliff", "cliff", "cliff", "cliff"],
            "recipient_class": ["investors", "team", "ecosystem", "team"],
            "amount": [3_000.0, 1_000.0, 500.0, 500.0],
        }
    )
    as_of = start + timedelta(days=119)
    out = cs.event_table(
        ev,
        prices,
        {"aaa": "AAA", "bbb": "BBB"},
        {"aaa": 100_000.0, "bbb": 100_000.0},
        {"aaa": 0.0, "bbb": 0.0},
        as_of,
        start=date(2021, 1, 1),
        wash_pass_venues={"AAA": ["binance"]},
    )
    assert out.height == 2  # the day-200 cliff is after as_of
    a = out.filter(pl.col("id") == "aaa").to_dicts()[0]
    b = out.filter(pl.col("id") == "bbb").to_dicts()[0]
    assert a["hit"] is True and b["hit"] is False
    assert a["dominant_class"] == "investors" and a["classes"] == "investors,team"
    assert abs(a["share_of_float"] - 0.04) < 1e-9 and a["float_basis"]
    assert a["adv_basis"].startswith("wash-filtered") and b["adv_basis"].startswith(
        "exchange klines"
    )
    assert a["ret_pre_14d"] < 0 < b["ret_pre_14d"] and a["ret_post_14d"] < 0
    assert a["share_bucket"] == "2–5 %"
    summ = cs.summarise(
        out, {"single_unlock_float_share_min": 0.01, "single_unlock_days_of_volume_min": 2.0}
    )
    allrow = summ.filter(pl.col("group") == "all cliffs with price coverage").to_dicts()[0]
    assert allrow["n"] == 2 and allrow["hits"] == 1 and abs(allrow["hit_rate"] - 0.5) < 1e-12
    assert set(summ["group_kind"]) >= {
        "all",
        "rule",
        "recipient class",
        "share of float",
        "days of volume",
        "year",
    }
    br, n = cs.base_rate(prices, ["AAA", "BBB"], start, as_of)
    assert n > 0 and abs(br - 0.5) < 1e-9


def test_state_reading_is_deterministic_and_links_numbers():
    frag = {
        "phi": 1.23,
        "n_components": 5,
        "z_fr": 2.1,
        "z_oi": 0.4,
        "z_vrp_neg": -1.7,
        "z_dd": 0.2,
        "z_sc_neg": 0.1,
    }
    args = (
        date(2026, 9, 8),
        frag,
        [{"currency": "BTC", "vrp": -0.05}, {"currency": "ETH", "vrp": 0.02}],
        [
            {"base": "BTC", "venue": "okx", "days": 20.0, "basis_ann": -0.03},
            {"base": "BTC", "venue": "okx", "days": 80.0, "basis_ann": 0.04},
        ],
        {"oi_rel_pctile": 0.92, "oi_rel_pctile_n": 90},
        -0.02,
        -0.30,
        0.015,
        [
            {"rule_id": "4.1", "asset": "BTC", "fired": True},
            {"rule_id": "4.2", "asset": "BTC", "fired": None},
        ],
        ["binance"],
        False,
    )
    a, b = state_reading(*args), state_reading(*args)
    assert a == b
    text = reading_text(a)
    assert (
        "+1.23" in text
        and "fragile" in text
        and "funding at z = +2.10" in text
        and "variance risk premium at z = -1.70" in text
    )
    assert "BTC VRP negative" in text and "ETH VRP positive" in text
    assert (
        "backwardation" in text
        and "92nd percentile" in text
        and "-2.0% from its 90-day high" in text
        and "-30.0% from its cycle high" in text
    )
    assert "Rules firing: 4.1 on BTC" in text and "binance" in text
    hrefs = {s["href"] for s in a if "href" in s}
    assert {"#p-state", "#p-triggers", "#p-venue"} <= hrefs
    # no adjectives beyond the threshold words; no forecasts
    assert not any(w in text.lower() for w in ("buy", "sell", "bullish", "bearish", "expect"))


def test_alerts_open_and_close_conditions_dry_run(tmp_path, monkeypatch):
    from monitor import alerts, archive

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    now = datetime(2026, 9, 8, 7, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    archive.upsert(
        "rule_fires",
        pl.DataFrame(
            [
                {
                    "ts": now,
                    "rule_id": "4.1",
                    "asset": "BTC",
                    "fired": True,
                    "inputs": "{}",
                    "thresholds": "{}",
                    "note": None,
                    **prov,
                }
            ]
        ),
    )
    archive.upsert(
        "fetch_status",
        pl.DataFrame(
            [
                {
                    "ts": now - timedelta(hours=1),
                    "job": "hourly",
                    "dataset": "bybit_perps",
                    "ok": False,
                    "reason": "HTTP 403",
                    **prov,
                },
                {
                    "ts": now,
                    "job": "hourly",
                    "dataset": "bybit_perps",
                    "ok": False,
                    "reason": "HTTP 403",
                    **prov,
                },
                {
                    "ts": now - timedelta(hours=1),
                    "job": "hourly",
                    "dataset": "okx_perps",
                    "ok": True,
                    "reason": None,
                    **prov,
                },
                {
                    "ts": now,
                    "job": "hourly",
                    "dataset": "okx_perps",
                    "ok": False,
                    "reason": "timeout",
                    **prov,
                },
            ]
        ),
    )
    (site / "data" / "risk.json").write_text(
        '{"venue": {"exposure": [{"venue": "binance", "share_nav": 0.4, "limit_share_nav": 0.25, "breach": true}], "low_score": {"breach": false}}}'
    )
    r = alerts.sync(site_out=site, dry_run=True)
    assert r["live"] is False and r["active"] == 3 and r["opened"] == 3
    keys = set(archive.read("alerts")["key"])
    assert keys == {"rule:4.1:BTC", "venue:binance", "dataset:bybit_perps"}  # okx failed once only
    feed = (site / "alerts.xml").read_text()
    assert "<rss" in feed and "[ACTIVE]" in feed and "Rule 4.1 firing on BTC" in feed
    # the rule clears → the alert closes, the feed shows it cleared
    archive.upsert(
        "rule_fires",
        pl.DataFrame(
            [
                {
                    "ts": now + timedelta(hours=1),
                    "rule_id": "4.1",
                    "asset": "BTC",
                    "fired": False,
                    "inputs": "{}",
                    "thresholds": "{}",
                    "note": None,
                    **{**prov, "fetched_at": now + timedelta(hours=1)},
                }
            ]
        ),
    )
    r2 = alerts.sync(site_out=site, dry_run=True)
    assert r2["closed"] == 1 and r2["active"] == 2
    al = archive.read("alerts")
    assert al.filter(pl.col("key") == "rule:4.1:BTC")["closed_at"][0] is not None
    assert "cleared" in (site / "alerts.xml").read_text()


def test_liq_source_label_names_the_venues_present():
    from monitor.jobs_hourly import _liq_source

    assert _liq_source(None) is None
    one = pl.DataFrame({"venue": ["okx", "okx"]})
    assert _liq_source(one) == "okx (single-venue sample)"
    two = pl.DataFrame({"venue": ["okx", "binance", "bybit"]})
    assert _liq_source(two) == "binance+bybit+okx (multi-venue)"


def test_drawdown_helper_in_fragility_builder_is_non_positive():
    from monitor.compute.fragility import _drawdown

    d0 = date(2026, 1, 1)
    close = pl.DataFrame(
        {
            "date": [d0 + timedelta(days=i) for i in range(5)],
            "close": [100.0, 110.0, 120.0, 115.0, 100.0],
        }
    )
    dd = _drawdown(close, window=90)["dd"].to_numpy()
    assert np.all(dd <= 1e-12) and abs(dd[-1] - (100 / 120 - 1)) < 1e-12 and dd[2] == 0.0
