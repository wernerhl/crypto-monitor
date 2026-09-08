"""Rule 4.3 by its driver (work order 3, item 2). No threshold changes.

The rule flags VRP < 0 and Φ > 1. A negative variance risk premium arises two ways:
(a) complacency — implied vol is low (IV₁ₘ below its trailing 250-day median at the flag);
(b) post-shock — realised vol is high (RV₃₀ above its trailing 250-day 90th percentile at
the flag); (c) both, or (d) neither. The hit definition is the implemented one
(`compute.hitrates`): realised vol over the next 30 days exceeds the implied vol at the flag.
Flags separated by fewer than 30 days belong to one episode; the number of episodes is the
honest sample size for a 30-day-horizon rule."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

DRIVERS = ("complacency", "post-shock", "both", "neither")


def _trailing_quantile(x: np.ndarray, q: float, window: int = 250, min_n: int = 60) -> np.ndarray:
    """Quantile of the previous `window` values (excluding today), NaN until `min_n` exist."""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        h = x[max(0, i - window) : i]
        h = h[np.isfinite(h)]
        if h.size >= min_n:
            out[i] = float(np.quantile(h, q))
    return out


def driver_series(vrp_hist: pl.DataFrame, currency: str = "BTC") -> pl.DataFrame:
    """Per day: iv30, rv30, their trailing-250-day reference levels, percentiles and the driver."""
    v = vrp_hist.filter(pl.col("currency") == currency).sort("date")
    if not v.height:
        return pl.DataFrame(schema={"date": pl.Date})
    iv = v["iv30"].to_numpy().astype(float)
    rv = np.sqrt(np.clip(v["rv30_var"].to_numpy().astype(float), 0, None))
    iv_med = _trailing_quantile(iv, 0.5)
    rv_90 = _trailing_quantile(rv, 0.9)
    iv_pct = np.full(len(iv), np.nan)
    rv_pct = np.full(len(rv), np.nan)
    for i in range(len(iv)):
        h = iv[max(0, i - 250) : i]
        h = h[np.isfinite(h)]
        if h.size >= 60:
            iv_pct[i] = float(np.mean(h <= iv[i]))
        h = rv[max(0, i - 250) : i]
        h = h[np.isfinite(h)]
        if h.size >= 60:
            rv_pct[i] = float(np.mean(h <= rv[i]))
    comp = iv < iv_med
    shock = rv > rv_90
    driver = [
        None
        if not (np.isfinite(m) and np.isfinite(s))
        else ("both" if (c and k) else "complacency" if c else "post-shock" if k else "neither")
        for m, s, c, k in zip(iv_med, rv_90, comp, shock, strict=True)
    ]
    return v.select("date").with_columns(
        pl.Series("iv30", iv),
        pl.Series("rv30", rv),
        pl.Series("iv_median_250", iv_med),
        pl.Series("rv_p90_250", rv_90),
        pl.Series("iv_pctile_250", iv_pct),
        pl.Series("rv_pctile_250", rv_pct),
        pl.Series("driver", driver, dtype=pl.Utf8),
    )


def driver_label(row: dict | None) -> str | None:
    """One-line driver text for the panel and the reading."""
    if not row or row.get("driver") is None:
        return None
    ivp, rvp = row.get("iv_pctile_250"), row.get("rv_pctile_250")
    parts = []
    if row["driver"] in ("post-shock", "both"):
        parts.append(f"post-shock, RV at the {_ord(rvp)} percentile")
    if row["driver"] in ("complacency", "both"):
        parts.append(f"implied vol at the {_ord(ivp)} percentile")
    if row["driver"] == "neither":
        parts.append(f"neither: implied vol at the {_ord(ivp)}, RV at the {_ord(rvp)} percentile")
    return "; ".join(parts)


def _ord(p: float | None) -> str:
    if p is None or not np.isfinite(p):
        return "n/a"
    n = round(100 * p)
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def classify_flags(
    fires: pl.DataFrame,
    vrp_hist: pl.DataFrame,
    prices: pl.DataFrame,
    as_of: date,
    currency: str = "BTC",
) -> pl.DataFrame:
    """One row per historical 4.3 flag day for `currency`: driver, IV at the flag, RV over the
    next 30 days, hit, episode id (flags < 30 days apart share an episode)."""
    f = (
        fires.filter((pl.col("rule_id") == "4.3") & pl.col("fired") & (pl.col("asset") == currency))
        .with_columns(pl.col("ts").dt.date().alias("d"))
        .unique(subset=["d"])
        .sort("d")
    )
    schema = {
        "date": pl.Date,
        "currency": pl.Utf8,
        "driver": pl.Utf8,
        "iv_flag": pl.Float64,
        "rv30_flag": pl.Float64,
        "iv_pctile_250": pl.Float64,
        "rv_pctile_250": pl.Float64,
        "rv_next30": pl.Float64,
        "excess": pl.Float64,
        "hit": pl.Boolean,
        "episode": pl.Int64,
    }
    if not f.height:
        return pl.DataFrame(schema=schema)
    ds = driver_series(vrp_hist, currency)
    dmap = {r["date"]: r for r in ds.to_dicts()}
    px = prices.filter(pl.col("base") == currency).sort("date")
    pdates = px["date"].to_list()
    pclose = px["close"].to_numpy().astype(float)
    rows = []
    episode, last = 0, None
    for d in f["d"].to_list():
        if last is None or (d - last).days >= 30:
            episode += 1
        last = d
        r = dmap.get(d)
        if r is None or r.get("driver") is None:
            continue
        # realised vol over the next 30 calendar days (≥ 20 closes), as in compute.hitrates
        i0 = np.searchsorted(
            np.array(pdates, dtype="datetime64[D]"), np.datetime64(d), side="right"
        )
        i1 = np.searchsorted(
            np.array(pdates, dtype="datetime64[D]"),
            np.datetime64(d + timedelta(days=30)),
            side="right",
        )
        seg = pclose[i0:i1]
        rv_next = (
            float(np.sqrt(np.mean(np.diff(np.log(seg)) ** 2) * 365))
            if seg.size >= 20 and d + timedelta(days=30) <= as_of
            else None
        )
        rows.append(
            {
                "date": d,
                "currency": currency,
                "driver": r["driver"],
                "iv_flag": r["iv30"],
                "rv30_flag": r["rv30"],
                "iv_pctile_250": r["iv_pctile_250"],
                "rv_pctile_250": r["rv_pctile_250"],
                "rv_next30": rv_next,
                "excess": (rv_next - r["iv30"]) if rv_next is not None else None,
                "hit": (rv_next > r["iv30"]) if rv_next is not None else None,
                "episode": episode,
            }
        )
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def driver_table(flags: pl.DataFrame) -> pl.DataFrame:
    """Hit rate, mean excess (RV_next30 − IV_flag), n flags and n episodes per driver class."""
    schema = {
        "driver": pl.Utf8,
        "n": pl.Int64,
        "n_episodes": pl.Int64,
        "n_elapsed": pl.Int64,
        "hits": pl.Int64,
        "hit_rate": pl.Float64,
        "mean_excess": pl.Float64,
        "median_excess": pl.Float64,
    }
    if not flags.height:
        return pl.DataFrame(schema=schema)
    rows = []
    for drv in (*DRIVERS, "all"):
        g = flags if drv == "all" else flags.filter(pl.col("driver") == drv)
        e = g.drop_nulls("hit")
        n_e = e.height
        rows.append(
            {
                "driver": drv,
                "n": g.height,
                "n_episodes": int(g["episode"].n_unique()) if g.height else 0,
                "n_elapsed": n_e,
                "hits": int(e["hit"].sum()) if n_e else 0,
                "hit_rate": float(e["hit"].mean()) if n_e else None,
                "mean_excess": float(e["excess"].mean()) if n_e else None,
                "median_excess": float(e["excess"].median()) if n_e else None,
            }
        )
    return pl.DataFrame(rows, schema=schema)
