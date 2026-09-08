"""Freshness markers (build prompt 0.8): every table gets fresh / stale / unavailable with a reason."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from monitor import archive, jobs


def test_table_status_marks_fresh_stale_unavailable(tmp_path, monkeypatch):
    monkeypatch.setattr(archive, "PROCESSED", tmp_path)
    now = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)
    # markets: as_of today -> covers the whole day -> fresh
    pl.DataFrame(
        {
            "as_of": [date(2026, 9, 6)],
            "id": ["bitcoin"],
            "source": ["coingecko"],
            "fetched_at": [now],
        }
    ).write_parquet(tmp_path / "markets.parquet")
    # perp_snapshot: 40 h old, budget 30 h -> stale
    pl.DataFrame(
        {
            "ts": [now - timedelta(hours=40)],
            "venue": ["okx"],
            "symbol": ["BTC-USDT-SWAP"],
            "source": ["okx"],
            "fetched_at": [now],
        }
    ).write_parquet(tmp_path / "perp_snapshot.parquet")
    # A9: a date-only stamp for "today" fetched 3 h ago must give age 3 h, never a negative age
    pl.DataFrame(
        {
            "as_of": [date(2026, 9, 6)],
            "id": ["x"],
            "source": ["coingecko"],
            "fetched_at": [now - timedelta(hours=3)],
        }
    ).write_parquet(tmp_path / "universe.parquet")
    rows = {r["table"]: r for r in jobs.table_status(now)}
    assert rows["universe"]["age_hours"] == pytest.approx(3.0)
    assert all(r["age_hours"] is None or r["age_hours"] >= 0 for r in rows.values())
    assert rows["markets"]["status"] == "fresh" and rows["markets"]["reason"] is None
    assert (
        rows["perp_snapshot"]["status"] == "stale" and "40 h old" in rows["perp_snapshot"]["reason"]
    )
    assert (
        rows["category_members"]["status"] == "unavailable" and rows["category_members"]["reason"]
    )
    assert rows["perp_snapshot"]["sources"] == "okx"
