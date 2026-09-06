"""State-weighted Ledoit–Wolf covariances and the stress covariance (notes §8.2).

Σ_t = ξ_high Σ̂^(high) + (1 − ξ_high) Σ̂^(low) + η Φ⁺ (Σ̂^(high) − Σ̂^(low)),
where Σ̂^(j) are Ledoit–Wolf shrunk covariances estimated with observations weighted by the
filtered probability of state j (`sklearn.covariance.LedoitWolf` on re-weighted returns).
Effective number of bets (Proposition 2.1 generalised): N_eff = (Σ_i w_i²σ_i²)/(w'Σw) ... we use
the entropy-free definition N_eff = (w'diag(Σ)w) / (w'Σw), which equals 1/(ρ + (1−ρ)/N) for
equal weights, equal variances and common correlation ρ."""

from __future__ import annotations

import numpy as np
from sklearn.covariance import LedoitWolf


def weighted_ledoit_wolf(
    returns: np.ndarray, weights: np.ndarray, min_eff_n: float = 30.0
) -> np.ndarray | None:
    """Ledoit–Wolf on rows re-weighted by `weights` (state probabilities). Rows are scaled by
    sqrt(w_t / mean w) so that the sample covariance equals the weighted covariance; returns
    None when the effective sample Σw / max w is below `min_eff_n`."""
    r = np.asarray(returns, float)
    w = np.asarray(weights, float)
    ok = np.isfinite(r).all(axis=1) & np.isfinite(w) & (w > 0)
    r, w = r[ok], w[ok]
    if r.shape[0] < 5 or w.sum() / w.max() < min_eff_n:
        return None
    w = w / w.mean()
    mu = (r * w[:, None]).sum(axis=0) / w.sum()
    x = (r - mu) * np.sqrt(w)[:, None]
    lw = LedoitWolf(assume_centered=True).fit(x)
    return lw.covariance_


def stress_covariance(
    sigma_high: np.ndarray | None,
    sigma_low: np.ndarray | None,
    xi_high: float,
    phi: float | None,
    eta: float,
) -> tuple[np.ndarray | None, str]:
    """Eq. 8.2. Falls back to whichever state covariance exists (flagged in the note)."""
    if sigma_high is None and sigma_low is None:
        return None, "no covariance (insufficient history)"
    if sigma_high is None:
        return sigma_low, "low-state covariance only (high state has too few weighted observations)"
    if sigma_low is None:
        return sigma_high, "high-state covariance only"
    phi_plus = max(phi or 0.0, 0.0)
    return xi_high * sigma_high + (1 - xi_high) * sigma_low + eta * phi_plus * (
        sigma_high - sigma_low
    ), "ok"


def effective_bets(w: np.ndarray, sigma: np.ndarray) -> float | None:
    """N_eff = (Σ_i |w_i| σ_i)² / (w'Σw): equals 1/(ρ + (1−ρ)/N) for equal weights, equal
    variances and common correlation ρ (Proposition 2.1), ranging from 1 (one bet) to N."""
    w = np.asarray(w, float)
    var = float(w @ sigma @ w)
    if var <= 0:
        return None
    return float((np.abs(w) * np.sqrt(np.diag(sigma))).sum() ** 2 / var)


def portfolio_vol(w: np.ndarray, sigma: np.ndarray, periods_per_year: float = 365.0) -> float:
    return float(np.sqrt(max(w @ sigma @ w, 0.0) * periods_per_year))
