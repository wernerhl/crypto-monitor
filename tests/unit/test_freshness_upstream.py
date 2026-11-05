"""Freshness contract, 2026-11-04: `upstream_stale` for a source table whose data stopped advancing
while its own fetch keeps succeeding (blockchain.info's frozen charts; Snapshot days with no active
proposal), and vol_state's budget no tighter than its daily parent's."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest


def _tmp_archive(tmp_path, monkeypatch):
    from monitor import archive

    monkeypatch.setattr(archive, "PROCESSED", tmp_path / "processed")
    monkeypatch.setattr(archive, "ARCHIVE", tmp_path / "archive")
    return archive


def _btc_chain(archive, day: date, fetched: datetime) -> None:
    archive.upsert(
        "btc_chain",
        pl.DataFrame(
            {
                "date": [day],
                "asset": ["BTC"],
                "metric": ["hash-rate"],
                "value": [1.0e9],
                "unit": ["TH/s"],
                "source": ["blockchain_info"],
                "fetched_at": [fetched],
                "git_sha": ["x"],
            }
        ),
    )


def _fetch(name: str, ts: datetime, ok: bool) -> None:
    from monitor.fetch.resilient import FetchRun

    fr = FetchRun("daily", ts)

    def boom():
        raise RuntimeError("HTTP 503 from api.blockchain.info")

    fr.run(name, (lambda: "raw.json.gz") if ok else boom)
    fr.flush()


NOW = datetime(2026, 11, 4, 7, 45, tzinfo=UTC)


def test_frozen_upstream_with_ok_fetch_is_upstream_stale_not_a_violation(tmp_path, monkeypatch):
    from monitor import freshness

    archive = _tmp_archive(tmp_path, monkeypatch)
    _btc_chain(archive, date(2026, 10, 29), NOW - timedelta(minutes=9))  # 128 h old, as on 4 Nov
    _fetch("btc_chain", NOW - timedelta(minutes=9), ok=True)
    rows = {r["table"]: r for r in freshness.table_rows(NOW)}
    r = rows["btc_chain"]
    assert r["status"] == "upstream_stale", r
    assert "has not advanced" in r["reason"] and "btc_chain fetch succeeded" in r["reason"]
    assert not [v for v in freshness.violations("daily", NOW) if v.startswith("btc_chain")]
    # the tiles still say the component is behind
    assert freshness.reason_for("btc_chain", NOW).startswith("source stale since 2026-10-29")


def test_failed_fetch_keeps_the_table_stale(tmp_path, monkeypatch):
    from monitor import freshness

    archive = _tmp_archive(tmp_path, monkeypatch)
    _btc_chain(archive, date(2026, 10, 29), NOW - timedelta(days=1))
    _fetch("btc_chain", NOW - timedelta(minutes=9), ok=False)
    assert {r["table"]: r for r in freshness.table_rows(NOW)}["btc_chain"]["status"] == "stale"
    with pytest.raises(freshness.FreshnessError, match="btc_chain"):
        freshness.assert_job("daily", NOW)


def test_upstream_silent_past_the_grace_is_fatal_again(tmp_path, monkeypatch):
    from monitor import freshness

    archive = _tmp_archive(tmp_path, monkeypatch)
    spec = freshness.contract()["tables"]["btc_chain"]
    limit = spec["budget_hours"] + spec["upstream_grace_hours"]
    old = (NOW - timedelta(hours=limit + 48)).date()
    _btc_chain(archive, old, NOW - timedelta(minutes=9))
    _fetch("btc_chain", NOW - timedelta(minutes=9), ok=True)
    assert {r["table"]: r for r in freshness.table_rows(NOW)}["btc_chain"]["status"] == "stale"
    assert any(v.startswith("btc_chain") for v in freshness.violations("daily", NOW))


def test_old_ok_fetch_does_not_excuse_a_stale_table(tmp_path, monkeypatch):
    """The latest fetch must itself be within the budget: a job that stopped running is not upstream."""
    from monitor import freshness

    archive = _tmp_archive(tmp_path, monkeypatch)
    _btc_chain(archive, date(2026, 10, 29), NOW - timedelta(days=4))
    _fetch("btc_chain", NOW - timedelta(days=4), ok=True)
    assert {r["table"]: r for r in freshness.table_rows(NOW)}["btc_chain"]["status"] == "stale"


def test_no_active_proposal_day_is_not_a_violation(tmp_path, monkeypatch):
    from monitor import freshness

    archive = _tmp_archive(tmp_path, monkeypatch)
    last = NOW - timedelta(days=6)  # the last day any tracked space had an active proposal
    archive.upsert(
        "proposals",
        pl.DataFrame(
            {
                "id": ["0xabc"],
                "space": ["aave.eth"],
                "space_name": ["Aave"],
                "title": ["t"],
                "state": ["active"],
                "start": [last - timedelta(days=3)],
                "end": [last],
                "scores_total": [1.0],
                "link": [None],
                "source": ["snapshot"],
                "fetched_at": [last],
                "git_sha": ["x"],
            },
            schema_overrides={"link": pl.Utf8},
        ),
    )
    _fetch("snapshot", NOW - timedelta(minutes=9), ok=True)  # today's query: ok, zero rows
    assert {r["table"]: r for r in freshness.table_rows(NOW)}["proposals"][
        "status"
    ] == "upstream_stale"
    assert not [v for v in freshness.violations("daily", NOW) if v.startswith("proposals")]


def test_vol_state_budget_is_not_tighter_than_its_daily_parent():
    """GitHub starts the 01:25 UTC daily job 4-6.5 h late; with 30 h, vol_state (built hourly from
    prices_daily) went stale at 06:00 UTC on most mornings before the daily run refreshed its parent."""
    from monitor import freshness

    t = freshness.contract()["tables"]
    assert t["vol_state"]["parent"] == "prices_daily"
    assert t["vol_state"]["budget_hours"] >= t["prices_daily"]["budget_hours"]


def test_every_fetch_dataset_names_a_real_fetch_step():
    """fetch_dataset must match a FetchRun step name, or the rule silently never applies."""
    import re
    from pathlib import Path

    from monitor import freshness

    src = (Path(freshness.__file__).parent / "jobs_daily_ctx.py").read_text()
    steps = set(re.findall(r'\(\s*"([a-z0-9_]+)",\s*lambda', src))
    for name, spec in freshness.contract()["tables"].items():
        if spec.get("fetch_dataset"):
            assert spec["fetch_dataset"] in steps, (name, spec["fetch_dataset"])
            assert spec.get("upstream_grace_hours"), name
