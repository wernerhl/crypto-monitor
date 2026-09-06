"""Expected shortfall by simulation (notes §8.4): daily P&L under the mixture of the two vol
states with Student-t innovations and the stress covariance; ES_5% = −E[r | r ≤ q_5%]."""

from __future__ import annotations

import numpy as np


def simulate_es(
    w: np.ndarray,
    sigma: np.ndarray,
    df: float,
    n: int = 100_000,
    alpha: float = 0.05,
    seed: int = 7,
    horizon_days: int = 1,
) -> dict:
    """Multivariate Student-t returns with covariance `sigma` (scaled so the covariance of the
    draws equals sigma for df > 2). Returns VaR and ES as positive fractions of NAV."""
    rng = np.random.default_rng(seed)
    w = np.asarray(w, float)
    k = len(w)
    scale = sigma * (df - 2) / df if df > 2 else sigma
    try:
        chol = np.linalg.cholesky(scale + 1e-12 * np.eye(k))
    except np.linalg.LinAlgError:
        vals, vecs = np.linalg.eigh(scale)
        chol = vecs @ np.diag(np.sqrt(np.clip(vals, 0, None)))
    z = rng.standard_normal((n, k)) @ chol.T
    g = rng.chisquare(df, n) / df
    r = z / np.sqrt(g)[:, None] * np.sqrt(horizon_days)
    pnl = r @ w
    q = np.quantile(pnl, alpha)
    es = -float(pnl[pnl <= q].mean())
    return {
        "var": -float(q),
        "es": es,
        "alpha": alpha,
        "df": df,
        "n": n,
        "horizon_days": horizon_days,
    }
