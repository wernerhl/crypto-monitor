"""One fragility series for both the live job and the history (A3).

`build_series` takes daily component inputs (already signed as Definition 3.1 requires),
aligns them on one daily grid, carries each component forward by at most ONE day when a source
lags (stamped in `<c>_age_days`; older gaps stay missing — the "no silent forward-fill beyond
one period" rule), computes the 250-day robust z of each component walk-forward, and returns
the daily table. The live value for a date is the last row of this table for that date, so
live and history agree by construction.

Signs (notes Definition 3.1): z_fr on the OI-weighted Tier 1 funding; z_oi on aggregate OI /
Tier 1 cap; z_vrp_neg on −VRP; z_dd on DD = P/max₉₀ P − 1 (≤ 0, largest at the high, so being at
the 90-day high raises fragility); z_sc_neg on −(30-day stablecoin growth).
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from monitor.compute.positioning import robust_z_series

COMPONENTS = ("z_fr", "z_oi", "z_vrp_neg", "z_dd", "z_sc_neg")


def _daily(df: pl.DataFrame | None, col: str, name: str) -> pl.DataFrame:
    schema = {"date": pl.Date, name: pl.Float64}
    if df is None or not df.height or col not in df.columns:
        return pl.DataFrame(schema=schema)
    return (
        df.select(pl.col("date"), pl.col(col).cast(pl.Float64).alias(name))
        .drop_nulls()
        .sort("date")
        .unique(subset=["date"], keep="last")
    )


def build_series(
    funding_t1: pl.DataFrame | None,  # date, fr (OI-weighted annualised funding across Tier 1)
    oi_rel_t1: pl.DataFrame | None,  # date, oi_rel (Σ OKX OI / Σ Tier 1 cap)
    vrp: pl.DataFrame | None,  # date, vrp
    btc_close: pl.DataFrame | None,  # date, close
    stable_growth: pl.DataFrame | None,  # date, growth_30d
    end: date,
    window: int = 250,
    min_n: int = 30,
    max_carry_days: int = 1,
) -> pl.DataFrame:
    parts = {
        "fr": _daily(funding_t1, "fr", "fr"),
        "oi_rel": _daily(oi_rel_t1, "oi_rel", "oi_rel"),
        "vrp_neg": _daily(vrp, "vrp", "vrp_neg").with_columns(-pl.col("vrp_neg")),
        "dd": _drawdown(_daily(btc_close, "close", "close")),
        "sc_neg": _daily(stable_growth, "growth_30d", "sc_neg").with_columns(-pl.col("sc_neg")),
    }
    start = min([p["date"].min() for p in parts.values() if p.height] or [end])
    grid = pl.DataFrame({"date": pl.date_range(start, end, "1d", eager=True)})
    out = grid
    for name, p in parts.items():
        raw = grid.join(p, on="date", how="left")
        raw_has = raw[name].is_not_null().to_list()
        filled = raw.with_columns(pl.col(name).fill_null(strategy="forward", limit=max_carry_days))
        has = filled[name].is_not_null().to_list()
        age = [0 if r else (1 if h else None) for r, h in zip(raw_has, has, strict=True)]
        out = out.with_columns(filled[name], pl.Series(f"{name}_age_days", age, dtype=pl.Int64))
    zcols = {}
    for name, zname in (
        ("fr", "z_fr"),
        ("oi_rel", "z_oi"),
        ("vrp_neg", "z_vrp_neg"),
        ("dd", "z_dd"),
        ("sc_neg", "z_sc_neg"),
    ):
        x = out[name].to_numpy().astype(float)
        z = robust_z_series(x, window, min_n)
        z[np.isnan(x)] = np.nan  # a missing input day has no z, whatever the history
        zcols[zname] = z
    for k, v in zcols.items():
        out = out.with_columns(
            pl.Series(k, [None if np.isnan(t) else float(t) for t in v], dtype=pl.Float64)
        )
    zs = np.column_stack([out[c].fill_nan(None).to_numpy().astype(float) for c in COMPONENTS])
    n = np.sum(~np.isnan(zs), axis=1)
    with np.errstate(all="ignore"):
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            phi = np.where(n > 0, np.nanmean(zs, axis=1), np.nan)
    out = out.with_columns(
        pl.Series("phi", [None if np.isnan(t) else float(t) for t in phi], dtype=pl.Float64),
        pl.Series("n_components", n.astype(int), dtype=pl.Int64),
    )
    return out.rename(
        {
            "fr": "funding_ann_t1",
            "oi_rel": "oi_rel_t1",
            "vrp_neg": "vrp_neg",
            "dd": "dd_90",
            "sc_neg": "sc_growth_neg",
        }
    )


def _drawdown(close: pl.DataFrame, window: int = 90) -> pl.DataFrame:
    if not close.height:
        return pl.DataFrame(schema={"date": pl.Date, "dd": pl.Float64})
    c = close.sort("date")
    return c.with_columns(
        (
            pl.col("close") / pl.col("close").rolling_max(window_size=window, min_samples=1) - 1.0
        ).alias("dd")
    ).select("date", "dd")


def last_row(series: pl.DataFrame, on: date | None = None) -> dict | None:
    if series is None or not series.height:
        return None
    s = series.filter(pl.col("date") <= on) if on else series
    return s.sort("date").tail(1).to_dicts()[0] if s.height else None


def window_end(today: date, hour_utc: int) -> date:
    """The daily grid date the live job reports on: today."""
    _ = hour_utc
    return today - timedelta(days=0)
