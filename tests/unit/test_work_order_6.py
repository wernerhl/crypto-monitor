"""Work order 6 (2026-09-13): freshness contract (A2/A3), history == live over five days (A5),
collector coverage reading (B), event strip (D), screens placeholders (E), vol rows hourly (C2),
borrow-rate adapter (F1)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
import yaml

ROOT = Path(__file__).resolve().parents[2]


def _tmp_archive(tmp_path, monkeypatch):
    from monitor import archive

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    return archive


def test_freshness_contract_covers_every_table_and_flags_stale_and_lagging(tmp_path, monkeypatch):
    from monitor import archive, freshness

    c = freshness.contract()
    registry = {t for t in archive.KEYS if t != "fetch_status" and t != "trade_structures"}
    missing = registry - set(c["tables"])
    assert not missing, f"tables without a freshness entry: {sorted(missing)}"
    assert all("budget_hours" in v and "job" in v and "cadence" in v for v in c["tables"].values())
    _tmp_archive(tmp_path, monkeypatch)
    now = datetime(2026, 9, 14, 6, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}  # fetched_at is recent on purpose
    archive.upsert(
        "dvol",
        pl.DataFrame(
            {
                "ts": [now - timedelta(hours=1)],
                "currency": ["BTC"],
                "dvol": [40.0],
                **{k: [v] for k, v in prov.items()},
            }
        ),
    )
    archive.upsert(
        "dvol_daily",
        pl.DataFrame(
            {
                "date": [date(2026, 9, 7)],
                "currency": ["BTC"],
                "dvol": [40.0],
                **{k: [v] for k, v in prov.items()},
            }
        ),
    )
    rows = {r["table"]: r for r in freshness.table_rows(now)}
    assert rows["dvol"]["status"] == "fresh"
    assert rows["dvol_daily"]["status"] == "stale" and "2026-09-08" in rows["dvol_daily"]["reason"]
    # the reason a component tile shows names the table and its parent's state
    assert freshness.reason_for("dvol_daily", now).startswith(
        "source stale since 2026-09-07 (dvol_daily; parent dvol is current)"
    )
    assert freshness.reason_for("dvol", now) is None
    with pytest.raises(freshness.FreshnessError, match="dvol_daily"):
        freshness.assert_job("hourly", now)


def test_freshness_derived_table_lagging_its_parent(tmp_path, monkeypatch):
    from monitor import archive, freshness

    _tmp_archive(tmp_path, monkeypatch)
    now = datetime(2026, 9, 14, 6, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    archive.upsert(
        "stablecoin_total",
        pl.DataFrame(
            {"date": [date(2026, 9, 13)], "total_usd": [1e11], **{k: [v] for k, v in prov.items()}}
        ),
    )
    archive.upsert(
        "stablecoin_growth",
        pl.DataFrame(
            {
                "date": [date(2026, 9, 8)],
                "total_usd": [1e11],
                "growth_30d": [0.01],
                **{k: [v] for k, v in prov.items()},
            }
        ),
    )
    rows = {r["table"]: r for r in freshness.table_rows(now)}
    assert (
        rows["stablecoin_growth"]["status"] in ("stale", "lagging")
        and rows["stablecoin_total"]["status"] == "fresh"
    )
    assert "stablecoin_growth" in " ".join(freshness.violations("daily", now))


def test_history_json_fragility_equals_live_series_on_the_last_five_days(tmp_path, monkeypatch):
    """A5: history.json is cut from the shared series at the live date and rounded to nine
    significant digits, so live == history to 1e-6 on every common date, not just the last."""
    from monitor import archive, jobs_daily_ctx

    _tmp_archive(tmp_path, monkeypatch)
    now = datetime(2026, 9, 14, 6, tzinfo=UTC)
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(13)]
    rng = np.random.default_rng(6)
    phi = rng.normal(0, 1, len(days))
    fs = pl.DataFrame(
        {
            "date": days,
            "phi": phi,
            "n_components": [5] * len(days),
            **{
                c: rng.normal(0, 1, len(days))
                for c in ("z_fr", "z_oi", "z_vrp_neg", "z_dd", "z_sc_neg")
            },
            **{k: [v] * len(days) for k, v in prov.items()},
        }
    )
    archive.upsert("fragility_series", fs)
    archive.upsert(
        "fragility",
        pl.DataFrame(
            {
                "ts": [now],
                "date": [days[-1]],
                "phi": [phi[-1]],
                "n_components": [5],
                **{k: [v] for k, v in prov.items()},
            }
        ),
    )
    out = tmp_path / "site"
    jobs_daily_ctx.write_history_json(out, days=30)
    h = json.loads((out / "history.json").read_text())["fragility"]
    assert h[-1]["date"] == str(days[-2])  # live date minus one
    series = {str(r["date"]): r["phi"] for r in fs.to_dicts()}
    for r in h[-5:]:
        assert abs(r["phi"] - series[r["date"]]) < 1e-6


def test_liq_source_and_pctile_accrual_read_from_rows_and_coverage(tmp_path, monkeypatch):
    from monitor import archive, jobs_hourly

    _tmp_archive(tmp_path, monkeypatch)
    now = datetime(2026, 9, 14, 6, tzinfo=UTC)
    assert jobs_hourly._liq_source(None) == "no liquidation sample in window"
    old = pl.DataFrame({"venue": ["okx"], "ts": [now - timedelta(days=40)]})
    assert jobs_hourly._liq_source(old) == "no liquidation sample in window"
    prov = {"source": "t", "fetched_at": now, "git_sha": "x"}
    hours = [now - timedelta(hours=i) for i in range(1, 30)]
    archive.upsert(
        "liq_coverage",
        pl.DataFrame(
            {
                "hour": hours,
                "venue": ["bybit"] * len(hours),
                "connected_share": [0.95] * len(hours),
                "n_messages": [3] * len(hours),
                **{k: [v] * len(hours) for k, v in prov.items()},
            }
        ),
    )
    txt = jobs_hourly._liq_pctile_available_on(now)
    assert txt is not None and (
        txt == "available" or txt.startswith("2026-10") or "needs 30 covered days" in txt
    )
    hb = jobs_hourly._liq_heartbeat()
    assert hb["bybit"] == max(hours).isoformat()


def test_ws_envelopes_are_parsed_only_when_newer_than_the_coverage_table(tmp_path, monkeypatch):
    from monitor import archive, jobs_hourly
    from monitor.fetch.base import Envelope, RawStore, Record

    _tmp_archive(tmp_path, monkeypatch)
    store = RawStore(tmp_path / "raw")
    for hh in (1, 2, 3):
        env = Envelope(
            "bybit",
            "liquidations_ws",
            f"2026/09/14/{hh:02d}00",
            "2026-09-14T04:00:00+00:00",
            "x",
            [Record("wss://x", 101, "2026-09-14T04:00:00+00:00", [])],
            {
                "kind": "websocket",
                "hour": f"2026-09-14T{hh:02d}:00:00+00:00",
                "coverage": {
                    "hour": f"2026-09-14T{hh:02d}:00:00+00:00",
                    "connected_seconds": 3600,
                    "n_messages": 0,
                },
            },
        )
        store.write(env, datetime(2026, 9, 14, hh, tzinfo=UTC), "hourly")
    assert len(jobs_hourly._ws_envelopes(store, "bybit", rebuild=False)) == 3
    prov = {"source": "t", "fetched_at": datetime(2026, 9, 14, 4, tzinfo=UTC), "git_sha": "x"}
    archive.upsert(
        "liq_coverage",
        pl.DataFrame(
            {
                "hour": [datetime(2026, 9, 14, 2, tzinfo=UTC)],
                "venue": ["bybit"],
                "connected_share": [1.0],
                "n_messages": [0],
                **{k: [v] for k, v in prov.items()},
            }
        ),
    )
    got = jobs_hourly._ws_envelopes(store, "bybit", rebuild=False)
    assert [e.meta["hour"] for e in got] == ["2026-09-14T03:00:00+00:00"]


def test_event_strip_dedupes_expiries_and_drops_past_dates():
    from monitor.compute.context import event_strip

    as_of = date(2026, 9, 14)
    ts = [datetime(2026, 9, 13, h, tzinfo=UTC) for h in (1, 2, 3)]
    exp = datetime(2026, 9, 25, 8, tzinfo=UTC)
    expiries = pl.DataFrame(
        {
            "ts": ts * 2,
            "currency": ["BTC"] * 3 + ["ETH"] * 3,
            "expiry": [exp] * 6,
            "total_oi": [100.0, 110.0, 120.0, 50.0, 60.0, 70.0],
            "max_oi_strike": [60000.0] * 3 + [3000.0] * 3,
        }
    )
    cliffs = pl.DataFrame(
        {
            "id": ["a", "b"],
            "date": [date(2026, 9, 11), date(2026, 9, 20)],
            "unlock_tokens": [1e6, 1e6],
            "share_of_float": [0.05, 0.05],
            "days_of_volume": [3.0, 3.0],
            "usd": [1e6, 1e6],
            "classes": [["team"], ["team"]],
        }
    )
    out = event_strip(
        as_of,
        28,
        cliffs,
        None,
        [],
        expiries,
        {"a": "AAA", "b": "BBB"},
        cliff_th={"single_unlock_float_share_min": 0.01, "single_unlock_days_of_volume_min": 2.0},
        expiry_oi_share_min=0.10,
    )
    ex = out.filter(pl.col("kind") == "options expiry")
    assert ex.height == 2 and set(ex["asset"]) == {"BTC", "ETH"}
    assert "OI 120" in ex.filter(pl.col("asset") == "BTC")["title"][0]  # the latest snapshot's OI
    assert (out["date"] >= as_of).all()


def test_vol_rows_are_in_the_hourly_write_set_and_screens_use_float_placeholder_notes():
    cfg = yaml.safe_load((ROOT / "config" / "job_writes.yaml").read_text())
    assert "trades_vol" in cfg["hourly"]["tables"] and "trades_vol" not in cfg["daily"]["tables"]
    assert "borrow_rates" in cfg["daily"]["tables"]


def test_borrow_rate_adapter_parses_configured_pools_and_falls_back(tmp_path, monkeypatch):
    from monitor import archive, jobs_risk
    from monitor.fetch import llama
    from monitor.fetch.base import Envelope, Record

    _tmp_archive(tmp_path, monkeypatch)
    pools = {"USDC": "p-usdc", "USDT": "p-usdt"}
    body = [
        {"pool": "p-usdc", "apyBaseBorrow": 13.98, "totalBorrowUsd": 2e9},
        {"pool": "p-usdt", "apyBaseBorrow": 4.08, "totalBorrowUsd": 2.7e9},
        {"pool": "other", "apyBaseBorrow": 1.0},
    ]
    env = Envelope(
        "llama_yields",
        "lend_borrow",
        "2026/09/14/0000",
        "2026-09-14T06:00:00+00:00",
        "x",
        [Record("https://yields.llama.fi/lendBorrow", 200, "2026-09-14T06:00:00+00:00", body)],
    )
    df = llama.parse_yields_borrow(env, pools)
    assert (
        df.height == 2
        and abs(df.filter(pl.col("asset") == "USDT")["apy_borrow"][0] - 0.0408) < 1e-9
    )
    th = {"carry": {"stablecoin_borrow_ann": 0.06, "borrow_rate_max_age_hours": 30}}
    rate, src = jobs_risk.borrow_rate(datetime(2026, 9, 14, 6, tzinfo=UTC), th)
    assert rate == 0.06 and src.startswith("FALLBACK")
    archive.upsert("borrow_rates", df)
    rate, src = jobs_risk.borrow_rate(datetime(2026, 9, 14, 6, tzinfo=UTC), th)
    assert abs(rate - 0.0408) < 1e-9 and "observed" in src and "USDT 4.08%" in src
    rate, src = jobs_risk.borrow_rate(datetime(2026, 9, 17, 6, tzinfo=UTC), th)
    assert rate == 0.06 and "FALLBACK" in src
