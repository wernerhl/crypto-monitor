"""Scenario P&L (notes §8.4): liquidation cascade, systemic 50 % drawdowns, venue halts."""

from __future__ import annotations

import numpy as np


def cascade_pnl(
    positions: list[dict],
    lambda_minus: dict[str, float],
    depth: dict[str, float],
    sigma_d: dict[str, float],
    nav: float,
    kappa: float = 2.0,
) -> dict:
    """P&L if all liquidations within κσ of current prices trigger simultaneously. The price
    impact of the forced flow is approximated by the square-root law on the liquidation mass
    relative to 2 % depth: move = −min(κσ, 0.02 × sqrt(Λ⁻ / D(0.02))) for longs (and the mirror
    for shorts), applied to each position's exposure."""
    rows, total = [], 0.0
    for p in positions:
        a = p["asset_symbol"]
        lam, d, s = lambda_minus.get(a), depth.get(a), sigma_d.get(a)
        if lam is None or not d or s is None:
            rows.append(
                {
                    "asset": a,
                    "weight": p["weight"],
                    "move": None,
                    "pnl_share_nav": None,
                    "note": "unavailable: no liquidation density / depth",
                }
            )
            continue
        move = -min(kappa * s, 0.02 * np.sqrt(lam / d))
        pnl = p["weight"] * move
        total += pnl
        rows.append(
            {
                "asset": a,
                "weight": p["weight"],
                "move": move,
                "pnl_share_nav": pnl,
                "lambda_minus": lam,
                "depth": d,
                "note": None,
            }
        )
    return {"rows": rows, "total_share_nav": total, "total_usd": total * nav}


def systemic_pnl(
    positions: list[dict],
    systemic: list[dict],
    collateral: list[dict],
    nav: float,
    drawdown: float = 0.5,
) -> list[dict]:
    """P&L under a 50 % drawdown in each hand-tracked systemic exposure: direct exposure to
    the asset itself plus collateral posted in it (a stablecoin de-peg of 50 % halves the
    collateral value)."""
    out = []
    for s in systemic:
        name = s["name"]
        direct = sum(p["weight"] for p in positions if p["asset_symbol"] == name) * nav
        coll = sum(float(c["usd"]) for c in collateral if c.get("stablecoin") == name)
        loss = -drawdown * (abs(direct) + coll)
        out.append(
            {
                "exposure": name,
                "kind": s.get("kind"),
                "direct_usd": direct,
                "collateral_usd": coll,
                "pnl_usd": loss,
                "pnl_share_nav": loss / nav,
                "note": s.get("note"),
            }
        )
    return out


def venue_halt_pnl(exposure_by_venue: dict[str, float], recovery: float, nav: float) -> list[dict]:
    """Withdrawal halt at each venue: exposure marked to the recovery assumption."""
    return [
        {
            "venue": v,
            "exposure_usd": x,
            "recovery": recovery,
            "pnl_usd": -(1 - recovery) * x,
            "pnl_share_nav": -(1 - recovery) * x / nav,
        }
        for v, x in sorted(exposure_by_venue.items())
    ]


def kelly_bound(
    mu: np.ndarray, sigma: np.ndarray, shrink: float = 0.5, cap: float = 0.25
) -> np.ndarray:
    """Upper bound on position size (notes §8.3): cap × Kelly weight Σ⁻¹(shrink × μ)."""
    try:
        w = np.linalg.solve(sigma, shrink * np.asarray(mu, float))
    except np.linalg.LinAlgError:
        w = np.linalg.pinv(sigma) @ (shrink * np.asarray(mu, float))
    return cap * w
