"""FRED adapter. Primary: keyless CSV `fredgraph.csv?id=SERIES` (verified; must be fetched
serially — concurrent requests time out). Optional: JSON API with `FRED_API_KEY` for series
metadata. Units reconciled from magnitudes (docs/data_sources.md §4): WALCL and WTREGEN in
USD millions, RRPONTSYD in USD billions."""

from __future__ import annotations

import io
import os
from datetime import datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset

SERIES = {
    "WALCL": ("fed_balance_sheet", "musd", "weekly"),
    "WTREGEN": ("tga", "musd", "weekly"),
    "RRPONTSYD": ("rrp", "busd", "daily"),
    "DFII10": ("tips_10y", "pct", "daily"),
    "DTWEXBGS": ("dxy_broad", "index", "daily"),
    "VIXCLS": ("vix", "index", "daily"),
}


def fetch_series(ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get("", params={"id": s}) for s in SERIES]

    return run_dataset(
        "fred_csv", "series", "daily", go, ts=ts, force=force, meta={"series": list(SERIES)}
    )


def fetch_meta(ts: datetime | None = None, force: bool = False) -> Path | None:
    """Series metadata (units, last updated) — only when FRED_API_KEY is set."""
    key = os.environ.get("FRED_API_KEY")
    if not key:
        return None

    def go(c) -> list[Record]:
        return [
            c.get("/series", params={"series_id": s, "file_type": "json", "api_key": key})
            for s in SERIES
        ]

    return run_dataset(
        "fred_api", "meta", "daily", go, ts=ts, force=force, meta={"series": list(SERIES)}
    )


def parse_series(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    frames = []
    for rec, sid in zip(env.records, env.meta["series"], strict=True):
        text = rec.body if isinstance(rec.body, str) else str(rec.body)
        df = pl.read_csv(io.StringIO(text), null_values=["."], schema_overrides={sid: pl.Float64})
        name, unit, freq = SERIES[sid]
        frames.append(
            df.rename({"observation_date": "date", sid: "value"})
            .with_columns(
                pl.col("date").str.to_date(),
                pl.lit(sid).alias("series_id"),
                pl.lit(name).alias("series"),
                pl.lit(unit).alias("unit"),
                pl.lit(freq).alias("freq"),
                pl.lit("fred_csv").alias("source"),
                pl.lit(fetched).alias("fetched_at"),
                pl.lit(env.git_sha).alias("git_sha"),
            )
            .drop_nulls("value")
        )
    out = pl.concat(frames)
    if out.group_by("series_id").len()["len"].min() < 500:
        raise SanityError("fred: a series has fewer than 500 observations")
    return out
