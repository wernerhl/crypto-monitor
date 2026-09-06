"""Parquet + DuckDB archive management.

* `data/processed/<table>.parquet` — current state of each table (full history for daily
  tables; the hourly tables keep a rolling 90-day window here).
* `data/archive/<table>/YYYY-MM.parquet` — monthly partitions written on every daily run
  (the current month is overwritten, older months untouched), so `make all` can rebuild
  `processed` from `archive` + raw files and a DuckDB query can read everything with a glob.

Writes are idempotent: `upsert` de-duplicates on the table's key columns, keeping the row
with the latest `fetched_at`.
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path

import duckdb
import polars as pl

from monitor.paths import ARCHIVE, PROCESSED

KEYS: dict[str, list[str]] = {
    "markets": ["as_of", "id", "source"],
    "coin_meta": ["id"],
    "category_members": ["as_of", "category_id", "id"],
    "perp_snapshot": ["ts", "venue", "symbol"],
    "prices_daily": ["date", "venue", "symbol"],
    "venue_listings": ["ts", "venue", "market", "symbol"],
    "universe": ["as_of", "id"],
    "orderbook_depth": ["ts", "venue", "symbol"],
    "trade_stats": ["ts", "venue", "symbol"],
    "liquidations": ["ts", "venue", "symbol", "side_closed", "price", "size_base"],
    "options": ["ts", "instrument"],
    "futures_marks": ["ts", "venue", "symbol"],
    "prices_hourly": ["ts", "venue", "symbol"],
    "long_short": ["ts", "venue", "symbol"],
    "dvol": ["ts", "currency"],
    "funding_daily": ["date", "base"],
    "wash_filters": ["date", "base", "venue"],
    "liquidity": ["date", "id"],
    "positioning": ["ts", "base"],
    "options_metrics": ["ts", "currency"],
    "fragility": ["ts"],
    "rule_fires": ["ts", "rule_id", "asset"],
    "vol_state": ["date"],
}
TIME_COL: dict[str, str] = {
    "markets": "as_of",
    "coin_meta": "fetched_at",
    "category_members": "as_of",
    "perp_snapshot": "ts",
    "prices_daily": "date",
    "venue_listings": "ts",
    "universe": "as_of",
    "orderbook_depth": "ts",
    "trade_stats": "ts",
    "liquidations": "ts",
    "options": "ts",
    "futures_marks": "ts",
    "prices_hourly": "ts",
    "long_short": "ts",
    "dvol": "ts",
    "funding_daily": "date",
    "wash_filters": "date",
    "liquidity": "date",
    "positioning": "ts",
    "options_metrics": "ts",
    "fragility": "ts",
    "rule_fires": "ts",
    "vol_state": "date",
}
# hourly tables keep a rolling window in data/processed; the archive keeps everything
ROLLING_DAYS: dict[str, int] = {
    "orderbook_depth": 90,
    "trade_stats": 90,
    "liquidations": 90,
    "options": 45,
    "prices_hourly": 120,
    "futures_marks": 90,
    "long_short": 90,
    "perp_snapshot": 120,
}


def processed_path(table: str) -> Path:
    return PROCESSED / f"{table}.parquet"


def read(table: str) -> pl.DataFrame | None:
    p = processed_path(table)
    return pl.read_parquet(p) if p.exists() else None


def upsert(table: str, new: pl.DataFrame) -> pl.DataFrame:
    """Merge `new` into the processed table on KEYS[table], newest fetched_at wins."""
    keys = KEYS[table]
    old = read(table)
    if old is not None and old.height:
        new = new.select(old.columns) if set(old.columns) == set(new.columns) else new
        df = pl.concat([old, new], how="diagonal_relaxed")
    else:
        df = new
    df = df.sort("fetched_at").unique(subset=keys, keep="last").sort(keys)
    PROCESSED.mkdir(parents=True, exist_ok=True)
    # archive partitions first (everything), then trim the processed copy to its window
    write_partitions(table, df)
    if table in ROLLING_DAYS:
        tcol = TIME_COL[table]
        cutoff = df[tcol].max() - _dt.timedelta(days=ROLLING_DAYS[table])
        df = df.filter(pl.col(tcol) >= cutoff)
    df.write_parquet(processed_path(table), compression="zstd")
    return df


def write_partitions(table: str, df: pl.DataFrame | None = None) -> list[Path]:
    """Repartition a processed table into monthly files under data/archive/<table>/."""
    df = df if df is not None else read(table)
    if df is None or not df.height:
        return []
    tcol = TIME_COL[table]
    out: list[Path] = []
    d = df.with_columns(
        pl.col(tcol).cast(pl.Datetime("us", "UTC")).dt.strftime("%Y-%m").alias("_ym")
    )
    for (ym,), part in d.group_by("_ym", maintain_order=True):
        p = ARCHIVE / table / f"{ym}.parquet"
        p.parent.mkdir(parents=True, exist_ok=True)
        part.drop("_ym").write_parquet(p, compression="zstd")
        out.append(p)
    return out


def rebuild_from_archive(table: str) -> pl.DataFrame | None:
    files = sorted((ARCHIVE / table).glob("*.parquet"))
    if not files:
        return None
    df = pl.concat([pl.read_parquet(f) for f in files], how="diagonal_relaxed")
    df = df.sort("fetched_at").unique(subset=KEYS[table], keep="last").sort(KEYS[table])
    PROCESSED.mkdir(parents=True, exist_ok=True)
    df.write_parquet(processed_path(table), compression="zstd")
    return df


def query(sql: str) -> pl.DataFrame:
    """Run SQL over the archive with DuckDB. Tables are exposed as views named after the
    processed parquet files, e.g. `SELECT * FROM universe WHERE tier = 1`."""
    con = duckdb.connect()
    for p in PROCESSED.glob("*.parquet"):
        con.execute(f"CREATE VIEW {p.stem} AS SELECT * FROM read_parquet('{p.as_posix()}')")
    return con.execute(sql).pl()
