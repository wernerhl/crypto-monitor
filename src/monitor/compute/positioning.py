"""Positioning (notes Section 4): the signals that work.

* `aggregate_funding` — OI-weighted funding across venues per base and timestamp, annualised
  as FR_ann = (24/interval_h) × 365 × FR (eq. 4.1 uses 3 × 365 for 8-hour funding; venues with
  4-hour or 1-hour intervals are annualised with their own interval).
* `robust_z` — (x − median_w) / (1.4826 × MAD_w) over a trailing window (eq. 4.2 with w = 90 d
  for funding; Definition 3.1 with w = 250 d for the fragility components).
* `oi_relative` — OI^rel = OI$ / MarketCap (§4.2).
* `oi_price_quadrant` — sign(Δlog P), sign(Δlog OI) over d days and the 20-day slope of a
  regression of Δlog OI on Δlog P (§4.2 table).
* `liquidation_density` — λ(p) from OI increments by price bucket and an assumed leverage
  distribution (eq. 4.3); `liquidation_mass` — Λ^±(κ) (eq. 4.4).
* `options_metrics` — RR25, ATM term points (1w/1m/3m), VRP = IV²₃₀ − RV̂²₃₀ (§4.4), largest-OI strikes.
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import numpy as np
import polars as pl

MAD_SCALE = 1.4826


# --------------------------------------------------------------------------- funding
def annualise(rate: pl.Expr, interval_h: pl.Expr) -> pl.Expr:
    """FR_ann = FR × (24 / interval_h) × 365. With 8-hour funding this is 3 × 365 × FR (eq. 4.1)."""
    return rate * (24.0 / interval_h.fill_null(8.0)) * 365.0


def aggregate_funding(perps: pl.DataFrame) -> pl.DataFrame:
    """Per (ts, base): OI-weighted mean of annualised funding across venues that report both
    funding and OI, Σ OI$, venue count. Rows without funding (e.g. OKX daily snapshot) are
    excluded from the weighted mean but counted in OI."""
    p = perps.filter(pl.col("oi_usd").is_not_null() & (pl.col("oi_usd") > 0)).with_columns(
        annualise(pl.col("funding_rate"), pl.col("funding_interval_h")).alias("fr_ann"),
        pl.col("ts").alias("ts_orig"),
        pl.col("ts").dt.truncate("1h").alias("ts"),  # venues are polled seconds apart
    )
    # one row per venue-symbol per hour bucket (forced re-fetches write several snapshots)
    key = ["ts", "venue", "symbol"] if "symbol" in p.columns else ["ts", "venue", "base"]
    p = p.sort("ts_orig").unique(subset=key, keep="last").drop("ts_orig")
    return p.group_by("ts", "base").agg(
        (
            (pl.col("fr_ann") * pl.col("oi_usd")).filter(pl.col("fr_ann").is_not_null()).sum()
            / pl.col("oi_usd").filter(pl.col("fr_ann").is_not_null()).sum()
        ).alias("funding_ann"),
        pl.col("oi_usd").sum().alias("oi_usd"),
        pl.col("oi_usd").filter(pl.col("venue") == "okx").sum().alias("oi_usd_okx"),
        pl.col("venue").n_unique().cast(pl.Int64).alias("n_venues"),
        pl.col("venue")
        .filter(pl.col("fr_ann").is_not_null())
        .n_unique()
        .cast(pl.Int64)
        .alias("n_venues_funding"),
    )


def daily_from_snapshots(agg: pl.DataFrame) -> pl.DataFrame:
    """One row per (date, base): mean funding_ann and last OI of the day (the OI-weighted
    funding series is standardised on a daily grid so hourly repeats of the same 8-hour rate
    do not inflate the sample). `oi_usd_okx` is the OKX-only OI (see backfill)."""
    return (
        agg.sort("ts")
        .with_columns(pl.col("ts").dt.date().alias("date"))
        .group_by("date", "base")
        .agg(
            pl.col("funding_ann").mean(),
            pl.col("oi_usd").last(),
            pl.col("oi_usd_okx").last(),
            pl.col("n_venues").max(),
            pl.col("ts").last(),
        )
    )


def robust_z(x: np.ndarray, window: int, min_n: int = 30) -> tuple[float | None, int]:
    """z of the last value against the trailing window (excluding it): (x_t − median) / (1.4826·MAD).
    Returns (z, n_used); z is None when fewer than `min_n` prior observations exist or MAD = 0."""
    x = np.asarray(
        [v for v in x if v is not None and not (isinstance(v, float) and math.isnan(v))],
        dtype=float,
    )
    if x.size < 2:
        return None, int(x.size)
    hist = x[-window - 1 : -1]
    if hist.size < min_n:
        return None, int(hist.size)
    med = float(np.median(hist))
    mad = float(np.median(np.abs(hist - med)))
    if mad == 0:
        return None, int(hist.size)
    return float((x[-1] - med) / (MAD_SCALE * mad)), int(hist.size)


def robust_z_series(x: np.ndarray, window: int, min_n: int = 30) -> np.ndarray:
    """Walk-forward robust z for every t (uses only data before t)."""
    out = np.full(len(x), np.nan)
    for i in range(len(x)):
        z, _ = robust_z(x[: i + 1], window, min_n)
        if z is not None:
            out[i] = z
    return out


# --------------------------------------------------------------------------- OI
def oi_relative(oi_usd: float | None, market_cap_usd: float | None) -> float | None:
    """OI^rel = OI$ / MarketCap (§4.2)."""
    if oi_usd is None or not market_cap_usd:
        return None
    return oi_usd / market_cap_usd


def percentile_rank(x: np.ndarray, window: int) -> float | None:
    """Share of the trailing window (excluding the last value) at or below the last value."""
    x = np.asarray(x, dtype=float)
    x = x[~np.isnan(x)]
    if x.size < 2:
        return None
    hist = x[-window - 1 : -1]
    return float(np.mean(hist <= x[-1]))


def oi_price_quadrant(
    close: np.ndarray, oi: np.ndarray, d: int = 5, slope_window: int = 20
) -> dict:
    """Joint move of price and OI over the last `d` days (table §4.2) and the slope of the
    20-day regression Δlog OI = a + b Δlog P (b > 0: leverage chases the move)."""
    close, oi = np.asarray(close, float), np.asarray(oi, float)
    if len(close) <= d or len(oi) <= d:
        return {"dlogp": None, "dlogoi": None, "quadrant": None, "slope_20d": None}
    dlp = math.log(close[-1] / close[-1 - d])
    dlo = math.log(oi[-1] / oi[-1 - d]) if oi[-1] > 0 and oi[-1 - d] > 0 else None
    quad = None
    if dlo is not None:
        quad = {
            (True, True): "new longs",
            (True, False): "short covering",
            (False, True): "new shorts",
            (False, False): "long liquidation",
        }[(dlp >= 0, dlo >= 0)]
    slope = None
    if len(close) > slope_window + 1:
        rp = np.diff(np.log(close[-slope_window - 1 :]))
        ro = np.diff(np.log(oi[-slope_window - 1 :]))
        ok = np.isfinite(rp) & np.isfinite(ro)
        if ok.sum() >= 10 and np.var(rp[ok]) > 0:
            slope = float(np.cov(rp[ok], ro[ok])[0, 1] / np.var(rp[ok], ddof=1))
    return {"dlogp": dlp, "dlogoi": dlo, "quadrant": quad, "slope_20d": slope}


# --------------------------------------------------------------------------- liquidation structure
def liquidation_prices(p0: float, leverage: float, m: float) -> tuple[float, float]:
    """Eq. 4.3: P_liq,long ≈ P0(1 − 1/ℓ + m), P_liq,short ≈ P0(1 + 1/ℓ − m)."""
    return p0 * (1 - 1 / leverage + m), p0 * (1 + 1 / leverage - m)


def liquidation_density(
    entries: list[tuple[float, float]],
    leverage_dist: dict[float, float],
    m: float,
    long_share: float = 0.5,
) -> list[tuple[float, float, str]]:
    """Build λ(p) as a list of (liquidation price, notional, side) atoms.

    `entries` = [(entry price, notional opened)] — OI increments attributed to the price at
    which they were opened. Each atom is split into long/short by `long_share` and across the
    assumed leverage distribution (documented assumption, `thresholds.yaml → positioning`).
    """
    atoms = []
    for p0, notional in entries:
        if notional <= 0 or p0 <= 0:
            continue
        for lev, w in leverage_dist.items():
            pl_, ps_ = liquidation_prices(p0, float(lev), m)
            atoms.append((pl_, notional * w * long_share, "long"))
            atoms.append((ps_, notional * w * (1 - long_share), "short"))
    return atoms


def liquidation_mass(
    atoms: list[tuple[float, float, str]],
    p_t: float,
    sigma_d: float,
    kappa: float,
    h_days: float = 1.0,
) -> dict:
    """Eq. 4.4: Λ^−(κ) = notional of longs liquidated between P_t(1 − κσ√h) and P_t;
    Λ^+(κ) = shorts liquidated between P_t and P_t(1 + κσ√h)."""
    band = kappa * sigma_d * math.sqrt(h_days)
    lo, hi = p_t * (1 - band), p_t * (1 + band)
    lam_minus = sum(n for p, n, s in atoms if s == "long" and lo <= p <= p_t)
    lam_plus = sum(n for p, n, s in atoms if s == "short" and p_t <= p <= hi)
    return {
        "lambda_minus": lam_minus,
        "lambda_plus": lam_plus,
        "asymmetry": lam_minus - lam_plus,
        "band_lo": lo,
        "band_hi": hi,
    }


def oi_entries_from_history(daily: pl.DataFrame, lookback: int = 30) -> list[tuple[float, float]]:
    """Attribute positive daily OI increments to the day's close; scale all atoms so their
    total equals current OI (positions opened earlier and still open are assumed to be
    distributed like the recent increments). `daily` has date, close, oi_usd for one base."""
    d = daily.sort("date").tail(lookback + 1)
    if d.height < 2:
        return []
    oi = d["oi_usd"].to_numpy()
    close = d["close"].to_numpy()
    inc = np.diff(oi)
    pos = [
        (float(close[i + 1]), float(inc[i]))
        for i in range(len(inc))
        if inc[i] > 0 and np.isfinite(close[i + 1])
    ]
    total_inc = sum(n for _, n in pos)
    cur = float(oi[-1])
    if total_inc <= 0 or cur <= 0:
        return [(float(close[-1]), cur)] if cur > 0 else []
    scale = cur / total_inc
    return [(p, n * scale) for p, n in pos]


# --------------------------------------------------------------------------- options
def _interp_iv_at_delta(
    strikes: np.ndarray, ivs: np.ndarray, deltas: np.ndarray, target: float
) -> float | None:
    """IV at a target |delta| by linear interpolation in delta across strikes of one type."""
    ok = np.isfinite(ivs) & np.isfinite(deltas)
    if ok.sum() < 2:
        return None
    d, v = np.abs(deltas[ok]), ivs[ok]
    order = np.argsort(d)
    d, v = d[order], v[order]
    if target < d[0] or target > d[-1]:
        return None
    return float(np.interp(target, d, v))


def expiry_metrics(chain: pl.DataFrame) -> pl.DataFrame:
    """Per expiry: ATM IV (call/put average at the strike nearest the underlying), 25-delta
    call IV, 25-delta put IV, RR25 = IV(25Δ call) − IV(25Δ put), total OI, largest-OI strike."""
    out = []
    for (exp,), g in chain.group_by("expiry", maintain_order=True):
        g = g.filter(pl.col("mark_iv").is_not_null() & (pl.col("mark_iv") > 0))
        if g.height < 4:
            continue
        f = float(g["underlying_price"].drop_nulls().median())
        k = g["strike"].to_numpy()
        atm_strike = float(k[np.argmin(np.abs(k - f))])
        atm = g.filter(pl.col("strike") == atm_strike)["mark_iv"].mean()
        calls, puts = (
            g.filter(pl.col("option_type") == "call"),
            g.filter(pl.col("option_type") == "put"),
        )
        c25 = _interp_iv_at_delta(
            calls["strike"].to_numpy(),
            calls["mark_iv"].to_numpy(),
            calls["delta_est"].to_numpy(),
            0.25,
        )
        p25 = _interp_iv_at_delta(
            puts["strike"].to_numpy(),
            puts["mark_iv"].to_numpy(),
            puts["delta_est"].to_numpy(),
            0.25,
        )
        oi = (
            g.group_by("strike")
            .agg(pl.col("open_interest").sum().alias("oi"))
            .sort("oi", descending=True)
        )
        out.append(
            {
                "expiry": exp,
                "t_years": float(g["t_years"].max()),
                "underlying": f,
                "atm_iv": float(atm) if atm is not None else None,
                "iv_call25": c25,
                "iv_put25": p25,
                "rr25": (c25 - p25) if (c25 is not None and p25 is not None) else None,
                "total_oi": float(g["open_interest"].fill_null(0).sum()),
                "max_oi_strike": float(oi["strike"][0]) if oi.height else None,
                "max_oi": float(oi["oi"][0]) if oi.height else None,
            }
        )
    return (
        pl.DataFrame(out)
        if out
        else pl.DataFrame(
            schema={
                "expiry": pl.Datetime("us", "UTC"),
                "t_years": pl.Float64,
                "underlying": pl.Float64,
                "atm_iv": pl.Float64,
                "iv_call25": pl.Float64,
                "iv_put25": pl.Float64,
                "rr25": pl.Float64,
                "total_oi": pl.Float64,
                "max_oi_strike": pl.Float64,
                "max_oi": pl.Float64,
            }
        )
    )


def term_point(em: pl.DataFrame, t_target: float, col: str = "atm_iv") -> float | None:
    """Interpolate `col` in time-to-expiry (years) at t_target; None if outside the range."""
    e = em.filter(pl.col(col).is_not_null() & (pl.col("t_years") > 0)).sort("t_years")
    if e.height < 2:
        return None
    t, v = e["t_years"].to_numpy(), e[col].to_numpy()
    if t_target < t[0] or t_target > t[-1]:
        return None
    return float(np.interp(t_target, t, v))


def realised_var_30(daily_returns: np.ndarray) -> float | None:
    """RV̂²₃₀ = (365/30) Σ_{k=1}^{30} f²_{t−k} (§4.4), in annualised variance units."""
    r = np.asarray(daily_returns, float)
    r = r[np.isfinite(r)]
    if r.size < 30:
        return None
    return float(365.0 / 30.0 * np.sum(r[-30:] ** 2))


def options_metrics(chain: pl.DataFrame, daily_log_returns: np.ndarray) -> dict:
    """Headline metrics for one currency at one snapshot. IVs in decimals."""
    em = expiry_metrics(chain.with_columns(pl.col("mark_iv") / 100.0))
    iv_1w, iv_1m, iv_3m = (
        term_point(em, 7 / 365),
        term_point(em, 30 / 365),
        term_point(em, 90 / 365),
    )
    rr_1m = term_point(em, 30 / 365, "rr25")
    rv30 = realised_var_30(daily_log_returns)
    vrp = (iv_1m**2 - rv30) if (iv_1m is not None and rv30 is not None) else None
    top = (
        em.sort("total_oi", descending=True)
        .head(4)
        .select("expiry", "max_oi_strike", "max_oi", "total_oi")
        .to_dicts()
        if em.height
        else []
    )
    return {
        "iv_1w": iv_1w,
        "iv_1m": iv_1m,
        "iv_3m": iv_3m,
        "term_slope": (iv_3m - iv_1w) if (iv_1w is not None and iv_3m is not None) else None,
        "rr25_1m": rr_1m,
        "rv30_var": rv30,
        "vrp": vrp,
        "largest_oi": top,
        "n_expiries": em.height,
    }


def trailing_window(df: pl.DataFrame, as_of: date, days: int, col: str = "date") -> pl.DataFrame:
    return df.filter((pl.col(col) > as_of - timedelta(days=days)) & (pl.col(col) <= as_of))
