"""One fragility series for both the live job and the history (A3).

`build_series` takes daily component inputs (already signed as Definition 3.1 requires),
aligns them on one daily grid, carries each component forward by at most ONE day when a source
lags (stamped in `<c>_age_days`; older gaps stay missing — the "no silent forward-fill beyond
one period" rule), computes the 250-day robust z of each component walk-forward, and returns
the daily table. The live value for a date is the last row of this table for that date, so
live and history agree by construction.

Signs (notes Definition 3.1): z_fr on the OI-weighted Tier 1 funding; z_oi on aggregate OI /
Tier 1 cap; z_vrp_neg on −VRP; z_sc_neg on −(30-day stablecoin growth). The fourth component
is NOT standardised (work order 2, item 2, 2026-09-08): z_dd = 4·(pos₉₀ − ½) with pos₉₀ the
position of the close inside its 90-day range, so it lives in [−2, 2] by construction. The
old form, a robust z of P/max₉₀ − 1, was bounded at zero and made any day near the high a
tail event of its own downtrending history.

Every component carries `z_<c>_n`, the number of finite input days in the trailing window.
`check_components` turns a null component whose source is fresh into a build failure.
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
        "dd": _drawdown(_daily(btc_close, "close", "close")).select("date", "dd"),
        "pos": _drawdown(_daily(btc_close, "close", "close")).select("date", "pos"),
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
    ncols = {}
    for name, zname in (
        ("fr", "z_fr"),
        ("oi_rel", "z_oi"),
        ("vrp_neg", "z_vrp_neg"),
        ("pos", "z_dd"),
        ("sc_neg", "z_sc_neg"),
    ):
        x = out[name].to_numpy().astype(float)
        # z_dd is a bounded score, not a standardised one (work order 2, item 2)
        z = range_position_score(x) if zname == "z_dd" else robust_z_series(x, window, min_n)
        z[np.isnan(x)] = np.nan  # a missing input day has no z, whatever the history
        zcols[zname] = z
        ncols[f"{zname}_n"] = _trailing_count(x, window)
    for k, v in zcols.items():
        out = out.with_columns(
            pl.Series(k, [None if np.isnan(t) else float(t) for t in v], dtype=pl.Float64)
        )
    for k, v in ncols.items():
        out = out.with_columns(pl.Series(k, v.astype(int), dtype=pl.Int64))
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
            "pos": "pos_90",
            "sc_neg": "sc_growth_neg",
        }
    )


def _drawdown(close: pl.DataFrame, window: int = 90) -> pl.DataFrame:
    """Per day: dd = P/max₉₀ − 1 (kept for the reading) and pos = (P − min₉₀)/(max₉₀ − min₉₀),
    the position of the close inside its trailing 90-day range (null when the range is zero)."""
    if not close.height:
        return pl.DataFrame(schema={"date": pl.Date, "dd": pl.Float64, "pos": pl.Float64})
    c = close.sort("date")
    hi = pl.col("close").rolling_max(window_size=window, min_samples=1)
    lo = pl.col("close").rolling_min(window_size=window, min_samples=1)
    return c.with_columns(
        (pl.col("close") / hi - 1.0).alias("dd"),
        pl.when(hi > lo).then((pl.col("close") - lo) / (hi - lo)).otherwise(None).alias("pos"),
    ).select("date", "dd", "pos")


def range_position_score(pos: np.ndarray) -> np.ndarray:
    """z_dd := 4·(pos − ½): −2 at the bottom of the 90-day range, +2 at the top."""
    return 4.0 * (np.asarray(pos, dtype=float) - 0.5)


def _trailing_count(x: np.ndarray, window: int) -> np.ndarray:
    fin = np.isfinite(x).astype(int)
    cs = np.concatenate([[0], np.cumsum(fin)])
    idx = np.arange(1, len(x) + 1)
    return cs[idx] - cs[np.maximum(idx - window, 0)]


SOURCE_OF = {
    "z_fr": "funding_daily",
    "z_oi": "oi_daily",
    "z_vrp_neg": "dvol",
    "z_dd": "prices_daily",
    "z_sc_neg": "stablecoin_growth",
}


def check_components(
    row: dict, source_dates: dict[str, date | None], as_of: date, max_lag_days: int = 1
) -> list[dict]:
    """Return the gaps (component, reason) for the null components of a live row. A null
    component whose source table is fresh (last date within `max_lag_days` of as_of) is a
    build failure and raises, so the job fails instead of publishing Φ on fewer components."""
    gaps: list[dict] = []
    for c in COMPONENTS:
        if row.get(c) is not None:
            continue
        src = SOURCE_OF[c]
        last = source_dates.get(c)
        if last is not None and (as_of - last).days <= max_lag_days:
            raise RuntimeError(
                f"fragility component {c} is null although its source {src} is fresh "
                f"(last {last}, as_of {as_of}); refusing to publish Φ on fewer components"
            )
        reason = f"{src} stale: last {last}" if last is not None else f"{src} has no rows"
        gaps.append({"component": c, "reason": reason})
    return gaps


def last_row(series: pl.DataFrame, on: date | None = None) -> dict | None:
    if series is None or not series.height:
        return None
    s = series.filter(pl.col("date") <= on) if on else series
    return s.sort("date").tail(1).to_dicts()[0] if s.height else None


def window_end(today: date, hour_utc: int) -> date:
    """The daily grid date the live job reports on: today."""
    _ = hour_utc
    return today - timedelta(days=0)
