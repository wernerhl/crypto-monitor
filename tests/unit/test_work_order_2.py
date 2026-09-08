"""Work order 2 (2026-09-08): items 3 (clustered SE, placebo), 4 (alert grouping), 5 (coverage),
6 (synced-folder guard)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest

from monitor.compute import cliff_study as cs


def test_clustered_se_and_placebo():
    # 100 events in 10 weeks, hits perfectly correlated within a week → clustered SE > i.i.d. SE
    weeks = np.repeat(np.arange(10), 10)
    hits = np.repeat(np.array([1, 0, 1, 0, 1, 0, 1, 0, 1, 0], dtype=float), 10)
    iid = float(np.sqrt(hits.mean() * (1 - hits.mean()) / hits.size))
    cl = cs.clustered_se(hits, weeks)
    assert cl is not None and cl > 2 * iid
    # one member per cluster → the i.i.d. formula (population form)
    single = cs.clustered_se(hits, np.arange(100))
    assert abs(single - float(np.sqrt(((hits - hits.mean()) ** 2).sum()) / 100)) < 1e-12
    # placebo: dates are non-cliff days of the same token and year, same window definitions
    start = date(2025, 1, 1)
    n = 120
    rows = []
    p = 100.0
    for i in range(n):
        p *= 0.995
        rows.append(
            {
                "date": start + timedelta(days=i),
                "venue": "binance",
                "base": "AAA",
                "close": p,
                "volume_quote": 1e6,
                "volume_base": None,
            }
        )
    prices = pl.DataFrame(rows)
    events = pl.DataFrame({"id": ["aaa"], "base": ["AAA"], "date": [start + timedelta(days=60)]})
    plc = cs.placebo_table(events, prices, k=5)
    assert plc.height == 5 and (plc["base"] == "AAA").all()
    assert all(d != start + timedelta(days=60) and d.year == 2025 for d in plc["date"])
    assert plc["hit"].all()  # a monotone downtrend: every pseudo-cliff is a hit
    assert set(plc.columns) >= {"week", "beta_mkt", "ret_pre_14d_beta_adj"}


def test_beta_adjusted_return_uses_latest_weekly_beta():
    betas = pl.DataFrame(
        {"id": ["aaa", "aaa"], "week": [date(2025, 1, 6), date(2025, 2, 3)], "beta_MKT": [0.5, 2.0]}
    )
    lk = cs._BetaLookup(betas)
    assert lk.at("aaa", date(2025, 1, 20)) == 0.5 and lk.at("aaa", date(2025, 2, 10)) == 2.0
    assert lk.at("aaa", date(2024, 12, 1)) is None and lk.at("zzz", date(2025, 3, 1)) is None


def test_rule_51_firings_collapse_into_one_calendar_condition(tmp_path, monkeypatch):
    from monitor import alerts, archive

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    site = tmp_path / "site"
    (site / "data").mkdir(parents=True)
    now = datetime(2026, 9, 8, 7, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    rows = [
        {
            "ts": now,
            "rule_id": "5.1",
            "asset": a,
            "fired": True,
            "inputs": f'{{"unlock_date": "2026-09-2{i}", "unlock_share_of_float": 0.0{i + 1}, "unlock_days_of_volume": {i + 2}.5}}',
            "thresholds": "{}",
            "note": None,
            **prov,
        }
        for i, a in enumerate(["ARB", "APT", "ZRO"])
    ]
    archive.upsert("cliff_calendar", pl.DataFrame([{**r, "status": "calendar"} for r in rows]))
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
    conds = alerts.current_conditions(site / "data")
    keys = {c["key"] for c in conds}
    assert keys == {"cliffs:calendar", "rule:4.1:BTC"}
    cal = next(c for c in conds if c["key"] == "cliffs:calendar")
    assert "3 qualifying" in cal["title"] and "| ARB |" in cal["body"] and "| ZRO |" in cal["body"]
    r = alerts.sync(site_out=site, dry_run=True)
    assert r["active"] == 2
    # the list changes → the same condition is refreshed, not reopened
    archive.upsert(
        "cliff_calendar",
        pl.DataFrame(
            [
                {
                    **rows[2],
                    "status": "calendar",
                    "fired": False,
                    "ts": now + timedelta(hours=1),
                    "fetched_at": now + timedelta(hours=1),
                }
            ]
        ),
    )
    r2 = alerts.sync(site_out=site, dry_run=True)
    assert r2["opened"] == 0 and r2["closed"] == 0
    al = archive.read("alerts").filter(pl.col("key") == "cliffs:calendar")
    assert al.height == 1 and "2 qualifying" in al["title"][0]


def test_liq_coverage_parser_and_covered_hours(tmp_path, monkeypatch):
    from monitor import archive, jobs_hourly
    from monitor.fetch import bybit
    from monitor.fetch.base import Envelope, Record

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    hour = datetime(2026, 9, 8, 11, tzinfo=UTC)
    env = Envelope(
        "bybit",
        "liquidations_ws",
        "2026/09/08/1100",
        "2026-09-08T12:00:05+00:00",
        "abc",
        [Record("wss://x", 101, "2026-09-08T12:00:05+00:00", [{"n": 1}])],
        {
            "kind": "websocket",
            "hour": hour.isoformat(),
            "coverage": {"hour": hour.isoformat(), "connected_seconds": 1800.0, "n_messages": 1},
        },
    )
    cov = bybit.parse_liq_coverage(env)
    assert (
        cov.height == 1
        and abs(cov["connected_share"][0] - 0.5) < 1e-12
        and cov["n_messages"][0] == 1
    )
    archive.upsert("liq_coverage", cov)
    # rows: a bybit hour recorded at 50 % coverage (dropped), an unrecorded bybit hour (kept), okx (kept)
    liqs = pl.DataFrame(
        {
            "ts": [
                hour + timedelta(minutes=10),
                hour + timedelta(hours=1, minutes=10),
                hour + timedelta(minutes=5),
            ],
            "venue": ["bybit", "bybit", "okx"],
            "symbol": ["BTCUSDT", "BTCUSDT", "BTC-USDT-SWAP"],
            "base": ["BTC", "BTC", "BTC"],
            "side_closed": ["long", "long", "long"],
            "price": [1.0, 1.0, 1.0],
            "size_base": [1.0, 1.0, 1.0],
            "notional_usd": [1.0, 2.0, 4.0],
            "source": ["bybit_ws", "bybit_ws", "okx"],
        }
    )
    kept = jobs_hourly._covered_liquidations(liqs, hour + timedelta(hours=2))
    assert sorted(kept["notional_usd"].to_list()) == [2.0, 4.0]
    assert jobs_hourly._liq_source(liqs) == "bybit+okx (multi-venue)"
    share = jobs_hourly._liq_coverage_share(liqs, hour + timedelta(hours=2))
    assert share == 0.0  # the one recorded hour is under-covered


def test_synced_folder_guard():
    from monitor.paths import assert_not_synced

    with pytest.raises(SystemExit, match="cloud-synced"):
        assert_not_synced(Path("/Users/someone/Documents/project/crypto-monitor"))
    with pytest.raises(SystemExit):
        assert_not_synced(Path("/Users/someone/Library/CloudStorage/Dropbox/x"))
    assert_not_synced(Path("/Users/someone/crypto-monitor"))  # no raise
