"""Does Φ predict the damage after a shock? (work order 3, item 5; notes §6.2.)

Walk-forward, filtered values only. A shock day t is a daily BTC log return below its rolling
250-day 5th percentile (computed on the prior 250 days, so the threshold is known at t). For
each shock: Φ_{t−1} and its components (the pre-shock state, with the component count), the
maximum drawdown from P_{t−1} over the next 5 and 20 days, realised vol over t+1..t+20
(annualised), and days to recover P_{t−1} (censored at 60). Outcomes are regressed on Φ_{t−1}
with Newey–West (HAC) errors because shocks cluster; the same per component; a placebo draws
non-shock days matched to the shock days' Φ distribution (by Φ tercile). Conditional
distributions by Φ tercile are reported next to the slopes. No threshold, no definition of Φ
is touched."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

COMPONENTS = ("z_fr", "z_oi", "z_vrp_neg", "z_dd", "z_sc_neg")
OUTCOMES = ("mdd_5", "mdd_20", "rv_20", "days_to_recover")


def shock_days(
    btc: pl.DataFrame, window: int = 250, q: float = 0.05, min_n: int = 120
) -> pl.DataFrame:
    """Daily BTC rows with `ret`, the rolling 5th-percentile threshold of the prior window and
    `shock`. `btc` has date, close."""
    b = btc.sort("date").with_columns(
        (pl.col("close").log() - pl.col("close").log().shift(1)).alias("ret")
    )
    r = b["ret"].to_numpy().astype(float)
    thr = np.full(len(r), np.nan)
    for i in range(len(r)):
        h = r[max(0, i - window) : i]
        h = h[np.isfinite(h)]
        if h.size >= min_n:
            thr[i] = float(np.quantile(h, q))
    # NaN thresholds (warm-up) must not compare: polars orders NaN above every number
    return b.with_columns(pl.Series("thr", thr).fill_nan(None)).with_columns(
        pl.when(pl.col("thr").is_null())
        .then(None)
        .otherwise(pl.col("ret") < pl.col("thr"))
        .alias("shock")
    )


def _outcomes(close: np.ndarray, i: int, max_recover: int = 60) -> dict:
    """Outcomes after index i (the shock day), relative to P_{i−1}."""
    p0 = close[i - 1]
    out = {}
    for h, name in ((5, "mdd_5"), (20, "mdd_20")):
        seg = close[i : i + h]
        out[name] = float(seg.min() / p0 - 1.0) if seg.size == h else None
    seg = close[i + 1 : i + 21]
    lr = np.diff(np.log(close[i : i + 21]))
    out["rv_20"] = float(np.sqrt(np.mean(lr**2) * 365)) if seg.size == 20 else None
    after = close[i : i + max_recover + 1]
    rec = np.argmax(after >= p0) if np.any(after >= p0) else None
    out["days_to_recover"] = (
        int(rec)
        if rec is not None and after.size == max_recover + 1
        else (max_recover if after.size == max_recover + 1 else None)
    )
    out["recovered"] = bool(rec is not None) if after.size == max_recover + 1 else None
    return out


def event_table(btc: pl.DataFrame, frag: pl.DataFrame, as_of: date) -> pl.DataFrame:
    """One row per shock day with Φ_{t−1}, components, n_components and the outcomes."""
    sd = shock_days(btc)
    close = sd["close"].to_numpy().astype(float)
    dates = sd["date"].to_list()
    fmap = {
        r["date"]: r for r in frag.select("date", "phi", "n_components", *COMPONENTS).to_dicts()
    }
    rows = []
    for i, d in enumerate(dates):
        if i < 1 or not sd["shock"][i]:
            continue
        f = fmap.get(d - timedelta(days=1)) or fmap.get(dates[i - 1])
        if f is None or f.get("phi") is None:
            continue
        if d + timedelta(days=61) > as_of:
            continue  # the 60-day recovery window has not elapsed
        rows.append(
            {
                "date": d,
                "ret": float(sd["ret"][i]),
                "thr": float(sd["thr"][i]),
                "phi_pre": f["phi"],
                "n_components": f["n_components"],
                **{c: f.get(c) for c in COMPONENTS},
                **_outcomes(close, i),
            }
        )
    schema = {
        "date": pl.Date,
        "ret": pl.Float64,
        "thr": pl.Float64,
        "phi_pre": pl.Float64,
        "n_components": pl.Int64,
        **{c: pl.Float64 for c in COMPONENTS},
        "mdd_5": pl.Float64,
        "mdd_20": pl.Float64,
        "rv_20": pl.Float64,
        "days_to_recover": pl.Int64,
        "recovered": pl.Boolean,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def placebo_table(
    btc: pl.DataFrame,
    frag: pl.DataFrame,
    events: pl.DataFrame,
    as_of: date,
    k_per_event: int = 5,
    seed: int = 20260908,
) -> pl.DataFrame:
    """Non-shock days drawn to match the shock days' Φ_{t−1} tercile distribution: for each
    shock, `k_per_event` non-shock days from the same Φ tercile (a day counts as non-shock
    when it is not a shock and no shock occurred in the previous 20 days)."""
    if not events.height:
        return pl.DataFrame(schema=events.schema)
    sd = shock_days(btc)
    close = sd["close"].to_numpy().astype(float)
    dates = sd["date"].to_list()
    shock_idx = set(np.flatnonzero(sd["shock"].fill_null(False).to_numpy()).tolist())
    quiet = [
        i
        for i in range(21, len(dates) - 61)
        if not any((i - j) in shock_idx for j in range(0, 21))
        and dates[i] + timedelta(days=61) <= as_of
    ]
    fmap = {
        r["date"]: r for r in frag.select("date", "phi", "n_components", *COMPONENTS).to_dicts()
    }
    cand = []
    for i in quiet:
        f = fmap.get(dates[i - 1])
        if f and f.get("phi") is not None:
            cand.append((i, f))
    if not cand:
        return pl.DataFrame(schema=events.schema)
    terc = np.quantile(events["phi_pre"].to_numpy(), [1 / 3, 2 / 3])
    bucket = lambda v: 0 if v <= terc[0] else 1 if v <= terc[1] else 2  # noqa: E731
    pools = {b: [c for c in cand if bucket(c[1]["phi"]) == b] for b in (0, 1, 2)}
    rng = np.random.default_rng(seed)
    rows = []
    for e in events.to_dicts():
        pool = pools[bucket(e["phi_pre"])]
        if not pool:
            continue
        for j in rng.choice(len(pool), size=min(k_per_event, len(pool)), replace=False):
            i, f = pool[int(j)]
            rows.append(
                {
                    "date": dates[i],
                    "ret": float(sd["ret"][i]),
                    "thr": float(sd["thr"][i]) if sd["thr"][i] is not None else None,
                    "phi_pre": f["phi"],
                    "n_components": f["n_components"],
                    **{c: f.get(c) for c in COMPONENTS},
                    **_outcomes(close, i),
                }
            )
    return pl.DataFrame(rows, schema=events.schema) if rows else pl.DataFrame(schema=events.schema)


def newey_west_slope(y: np.ndarray, x: np.ndarray, lags: int | None = None) -> dict:
    """OLS slope of y on x with Newey–West (HAC, Bartlett) standard errors."""
    import statsmodels.api as sm

    ok = np.isfinite(y) & np.isfinite(x)
    y, x = y[ok], x[ok]
    n = int(y.size)
    if n < 8:
        return {"n": n, "slope": None, "se": None, "t": None, "intercept": None, "r2": None}
    lags = lags if lags is not None else max(1, int(np.floor(4 * (n / 100) ** (2 / 9))))
    res = sm.OLS(y, sm.add_constant(x)).fit(cov_type="HAC", cov_kwds={"maxlags": lags})
    return {
        "n": n,
        "slope": float(res.params[1]),
        "se": float(res.bse[1]),
        "t": float(res.tvalues[1]),
        "intercept": float(res.params[0]),
        "r2": float(res.rsquared),
        "lags": lags,
    }


def regressions(events: pl.DataFrame, label: str) -> pl.DataFrame:
    """Every outcome on Φ_{t−1} and on each component; one row per (outcome, regressor)."""
    schema = {
        "sample": pl.Utf8,
        "outcome": pl.Utf8,
        "regressor": pl.Utf8,
        "n": pl.Int64,
        "slope": pl.Float64,
        "se": pl.Float64,
        "t": pl.Float64,
        "intercept": pl.Float64,
        "r2": pl.Float64,
        "lags": pl.Int64,
    }
    rows = []
    if not events.height:
        return pl.DataFrame(schema=schema)
    for out in OUTCOMES:
        y = events[out].cast(pl.Float64).to_numpy().astype(float)
        for reg in ("phi_pre", *COMPONENTS):
            x = events[reg].cast(pl.Float64).to_numpy().astype(float)
            r = newey_west_slope(y, x)
            rows.append(
                {
                    "sample": label,
                    "outcome": out,
                    "regressor": reg,
                    **{k: r.get(k) for k in ("n", "slope", "se", "t", "intercept", "r2", "lags")},
                }
            )
    return pl.DataFrame(rows, schema=schema)


def terciles(
    events: pl.DataFrame, label: str, edges: np.ndarray | None = None
) -> tuple[pl.DataFrame, np.ndarray]:
    """Conditional distributions of the outcomes by Φ_{t−1} tercile (edges from the shock
    sample; the placebo uses the same edges)."""
    schema = {
        "sample": pl.Utf8,
        "tercile": pl.Utf8,
        "phi_lo": pl.Float64,
        "phi_hi": pl.Float64,
        "n": pl.Int64,
        **{f"{o}_{s}": pl.Float64 for o in OUTCOMES for s in ("mean", "median", "p25", "p75")},
        "recovered_share": pl.Float64,
    }
    if not events.height:
        return pl.DataFrame(schema=schema), np.array([np.nan, np.nan])
    phi = events["phi_pre"].to_numpy()
    edges = edges if edges is not None else np.quantile(phi, [1 / 3, 2 / 3])
    labels = ["low Φ", "mid Φ", "high Φ"]
    rows = []
    for k in range(3):
        lo = -np.inf if k == 0 else edges[k - 1]
        hi = np.inf if k == 2 else edges[k]
        g = events.filter((pl.col("phi_pre") > lo) & (pl.col("phi_pre") <= hi))
        row = {
            "sample": label,
            "tercile": labels[k],
            "phi_lo": None if k == 0 else float(edges[k - 1]),
            "phi_hi": None if k == 2 else float(edges[k]),
            "n": g.height,
        }
        for o in OUTCOMES:
            v = g[o].drop_nulls().cast(pl.Float64).to_numpy()
            row.update(
                {
                    f"{o}_mean": float(v.mean()) if v.size else None,
                    f"{o}_median": float(np.median(v)) if v.size else None,
                    f"{o}_p25": float(np.quantile(v, 0.25)) if v.size else None,
                    f"{o}_p75": float(np.quantile(v, 0.75)) if v.size else None,
                }
            )
        rec = g["recovered"].drop_nulls()
        row["recovered_share"] = float(rec.mean()) if rec.len() else None
        rows.append(row)
    return pl.DataFrame(rows, schema=schema), edges


CLAIM_SIGN = {"mdd_5": -1, "mdd_20": -1, "rv_20": +1, "days_to_recover": +1}


def verdict(reg: pl.DataFrame, t_crit: float = 2.0) -> dict:
    """Which outcomes move with Φ₍t−1₎ in the direction §6.2 predicts on the shock sample
    (|t| ≥ t_crit and the claimed sign) and whether the placebo shows the same. Returns the
    per-outcome status and one sentence."""
    r = {
        (x["sample"], x["outcome"]): x
        for x in reg.filter(pl.col("regressor") == "phi_pre").to_dicts()
    }
    status = {}
    for o, sign in CLAIM_SIGN.items():
        sh, pl_ = r.get(("shocks", o)), r.get(("placebo", o))
        if not sh or sh.get("t") is None:
            status[o] = "n/a"
            continue
        supported = abs(sh["t"]) >= t_crit and np.sign(sh["slope"]) == sign
        opposite = abs(sh["t"]) >= t_crit and np.sign(sh["slope"]) == -sign
        on_placebo = bool(
            pl_
            and pl_.get("t") is not None
            and abs(pl_["t"]) >= t_crit
            and np.sign(pl_["slope"]) == np.sign(sh["slope"])
        )
        status[o] = ("supported" if supported else "opposite sign" if opposite else "null") + (
            ", also on placebo" if on_placebo and (supported or opposite) else ""
        )
    n_sup = sum(v.startswith("supported") for v in status.values())
    n_opp = sum(v.startswith("opposite") for v in status.values())
    if n_sup == 0 and n_opp == 0:
        text = "Null: no outcome moves with pre-shock Φ at |t| ≥ 2 in the claimed direction; the tercile tables show no ordering of damage by Φ."
    elif n_sup and not n_opp:
        text = f"Partly supported: {n_sup} of 4 outcomes move with pre-shock Φ in the claimed direction at |t| ≥ 2."
    elif n_opp and not n_sup:
        text = f"Opposite: {n_opp} of 4 outcomes move against the claim at |t| ≥ 2; higher pre-shock Φ was followed by less damage, not more."
    else:
        text = f"Mixed: {n_sup} outcome(s) in the claimed direction, {n_opp} against, at |t| ≥ 2."
    return {"status": status, "text": text}
