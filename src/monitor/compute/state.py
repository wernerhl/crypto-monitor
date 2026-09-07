"""Market state (notes Section 3).

* `fragility_index` — Φ_t = ⅕ (z^FR + z^OI + z^{VRP−} + z^{DD} + z^{SC−}) (Definition 3.1), each
  component a robust z over 250 days (median / 1.4826·MAD). When a component is unavailable
  the index is the mean of the available components and `n_components` says how many.
* `drawdown_from_high` — DD_t = P_t / max_{90d} P − 1 (the z^{DD} input is −DD so that being at
  the highs raises fragility).
* `net_liquidity` — L_t = WALCL − TGA − RRP in USD bn with units reconciled (§3.3): WALCL and
  WTREGEN are millions, RRPONTSYD is billions (verified in docs/data_sources.md).
* `vol_state_model` — two-state Markov switching on log realised vol (eq. 3.3),
  log RV_t = μ_{S_t} + φ log RV_{t−1} + σ_{S_t} ε_t, constant transition matrix, filtered
  probabilities only, expected duration 1/(1 − p_jj), parameter standard errors reported.
"""

from __future__ import annotations

import math
import warnings

import numpy as np

from monitor.compute.positioning import robust_z

COMPONENTS = ("z_fr", "z_oi", "z_vrp_neg", "z_dd", "z_sc_neg")


def drawdown_from_high(close: np.ndarray, window: int = 90) -> float | None:
    c = np.asarray(close, float)
    c = c[np.isfinite(c)]
    if c.size < 2:
        return None
    return float(c[-1] / np.max(c[-window:]) - 1.0)


def fragility_components(series: dict[str, np.ndarray], window: int = 250, min_n: int = 30) -> dict:
    """`series` maps component name -> raw history ending at t (already signed: pass −VRP,
    −DD and −stablecoin growth for the negative components). Returns z per component (None
    when unavailable) and the sample size used."""
    out = {}
    for name in COMPONENTS:
        x = series.get(name)
        if x is None or len(x) == 0:
            out[name], out[f"{name}_n"] = None, 0
            continue
        z, n = robust_z(np.asarray(x, float), window, min_n)
        out[name], out[f"{name}_n"] = z, n
    return out


def fragility_index(z: dict) -> tuple[float | None, int]:
    """Φ = mean of the available z components (all five in the full definition)."""
    vals = [z[c] for c in COMPONENTS if z.get(c) is not None]
    return (float(np.mean(vals)) if vals else None), len(vals)


def net_liquidity(walcl_musd: float, wtregen_musd: float, rrp_busd: float) -> float:
    """L = WALCL/1e3 − WTREGEN/1e3 − RRPONTSYD, in USD billions."""
    return walcl_musd / 1e3 - wtregen_musd / 1e3 - rrp_busd


def realised_vol_daily(log_returns: np.ndarray, window: int = 20) -> np.ndarray:
    """Annualised realised vol from a rolling window of daily log returns (√365 scaling)."""
    r = np.asarray(log_returns, float)
    out = np.full(r.size, np.nan)
    for i in range(window - 1, r.size):
        w = r[i - window + 1 : i + 1]
        out[i] = math.sqrt(np.mean(w**2) * 365.0)
    return out


def realised_vol_weekly(log_returns: np.ndarray, days: int = 7) -> np.ndarray:
    """Annualised realised vol on non-overlapping `days`-day blocks (√365 scaling); one value
    per block so the switching model sees independent observations."""
    r = np.asarray(log_returns, float)
    n = r.size // days
    if n == 0:
        return np.array([])
    blocks = r[r.size - n * days :].reshape(n, days)
    return np.sqrt(np.mean(blocks**2, axis=1) * 365.0)


def vol_state_model(log_rv: np.ndarray, min_obs: int = 250) -> dict:
    """Fit eq. 3.3 with statsmodels `MarkovAutoregression(k_regimes=2, order=1,
    switching_variance=True)`. Returns filtered P(high) for the last observation and its path,
    expected durations, parameters with standard errors, and a status string. Never uses
    smoothed probabilities."""
    x = np.asarray(log_rv, float)
    x = x[np.isfinite(x)]
    if x.size < min_obs:
        return {"status": f"insufficient history ({x.size} < {min_obs})", "p_high": None}
    try:
        from statsmodels.tsa.regime_switching.markov_autoregression import MarkovAutoregression
    except ImportError as e:  # pragma: no cover
        return {"status": f"statsmodels unavailable: {e}", "p_high": None}
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            mod = MarkovAutoregression(
                x, k_regimes=2, order=1, switching_variance=True, switching_ar=False
            )
            res = mod.fit(disp=False, maxiter=500)
        except Exception as e:
            return {"status": f"fit failed: {e}", "p_high": None}
    params = dict(zip(res.model.param_names, res.params, strict=False))
    bse = dict(zip(res.model.param_names, res.bse, strict=False))
    # identify the high-vol regime as the one with the larger constant (mean level)
    consts = [params.get(f"const[{k}]", 0.0) for k in range(2)]
    high = int(np.argmax(consts))
    filt = np.asarray(res.filtered_marginal_probabilities)
    p_high_path = filt[:, high] if filt.ndim == 2 else filt
    p = {}
    for j in range(2):
        pj = params.get(f"p[{j}->{j}]")
        if pj is None:  # statsmodels names transition params p[i->j] for the first k-1 columns
            pj = params.get(f"p[{j}->0]") if j == 0 else 1.0 - params.get("p[1->0]", 0.0)
        p[j] = float(pj)
    return {
        "status": "ok",
        "high_regime": high,
        "p_high": float(p_high_path[-1]),
        "p_high_path": p_high_path.tolist(),
        "expected_duration_high": 1.0 / max(1.0 - p[high], 1e-9),
        "expected_duration_low": 1.0 / max(1.0 - p[1 - high], 1e-9),
        "params": {k: float(v) for k, v in params.items()},
        "std_errors": {k: (float(v) if np.isfinite(v) else None) for k, v in bse.items()},
        "n_obs": int(x.size),
        "llf": float(res.llf),
    }
