"""Venue and counterparty risk (notes Section 6). The score is a deterministic function of
`config/venues.yaml`; reviewing that file is the data entry."""

from __future__ import annotations

import polars as pl

POINTS = {
    "licensing": {"full": 2, "partial": 1, "none": 0},
    "segregation": {"regulated": 2, "claimed": 1, "none": 0},
    "proof_of_reserves": {
        "audited_financials": 2,
        "merkle_with_liabilities": 2,
        "merkle_assets_only": 1,
        "none": 0,
    },
    "own_token_concentration": {"none": 1, "low": 1, "medium": 0.5, "high": 0},
}


def score_venue(v: dict, weights: dict, wash_share: float | None) -> tuple[float, str, dict]:
    """Ordinal score A–D from weighted points (max = Σ weights × 2 for the two-point criteria).
    Withdrawal halts subtract 2 per halt; wash share (from the filters) subtracts its weight
    when > 25 %. Returns (points, grade, breakdown)."""
    b = {}
    b["licensing"] = POINTS["licensing"].get(v.get("licensing"), 0) * weights["licensing"]
    b["segregation"] = POINTS["segregation"].get(v.get("segregation"), 0) * weights["segregation"]
    b["proof_of_reserves"] = (
        POINTS["proof_of_reserves"].get(v.get("proof_of_reserves"), 0)
        * weights["proof_of_reserves"]
    )
    b["withdrawal_history"] = (
        max(2 - 2 * int(v.get("withdrawal_halts_last_3y") or 0), 0) * weights["withdrawal_history"]
    )
    b["wash_share"] = (0 if (wash_share is not None and wash_share > 0.25) else 1) * weights[
        "wash_share"
    ]
    b["own_token_concentration"] = (
        POINTS["own_token_concentration"].get(v.get("own_token_concentration"), 0)
        * weights["own_token_concentration"]
    )
    total = sum(b.values())
    maxpts = (
        2
        * (
            weights["licensing"]
            + weights["segregation"]
            + weights["proof_of_reserves"]
            + weights["withdrawal_history"]
        )
        + weights["wash_share"]
        + weights["own_token_concentration"]
    )
    share = total / maxpts
    grade = "A" if share >= 0.85 else "B" if share >= 0.65 else "C" if share >= 0.45 else "D"
    return total, grade, b


def venue_table(cfg: dict, wash_by_venue: dict[str, float]) -> pl.DataFrame:
    rows = []
    for name, v in cfg["venues"].items():
        pts, grade, b = score_venue(v, cfg["score_weights"], wash_by_venue.get(name))
        rows.append(
            {
                "venue": name,
                "score_points": pts,
                "grade": grade,
                "limit_share_nav": cfg["exposure_limit_by_score"][grade],
                "wash_share": wash_by_venue.get(name),
                "reviewed_on": str(cfg["reviewed_on"]),
                **{f"pts_{k}": val for k, val in b.items()},
            }
        )
    return pl.DataFrame(rows)


def exposure_table(
    book: dict,
    venues: pl.DataFrame,
    prices: dict[str, float] | None = None,
    cfg: dict | None = None,
) -> pl.DataFrame:
    """X_v = |positions| + collateral at each venue vs x̄_v × NAV; the low-score aggregate limit."""
    nav = book["nav_usd"]
    exp: dict[str, float] = {}
    for p in book["positions"]:
        exp[p["venue"]] = exp.get(p["venue"], 0.0) + abs(p["weight"]) * nav
    for c in book.get("collateral", []):
        exp[c["venue"]] = exp.get(c["venue"], 0.0) + float(c["usd"])
    vmap = {r["venue"]: r for r in venues.to_dicts()}
    rows = []
    for v, x in sorted(exp.items()):
        r = vmap.get(v, {})
        lim = r.get("limit_share_nav", 0.0) * nav
        rows.append(
            {
                "venue": v,
                "grade": r.get("grade"),
                "exposure_usd": x,
                "share_nav": x / nav,
                "limit_usd": lim,
                "limit_share_nav": r.get("limit_share_nav"),
                "utilisation": (x / lim) if lim else None,
                "breach": (x > lim) if lim is not None else None,
            }
        )
    df = (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "venue": pl.Utf8,
                "grade": pl.Utf8,
                "exposure_usd": pl.Float64,
                "share_nav": pl.Float64,
                "limit_usd": pl.Float64,
                "limit_share_nav": pl.Float64,
                "utilisation": pl.Float64,
                "breach": pl.Boolean,
            }
        )
    )
    return df


def low_score_aggregate(exposure: pl.DataFrame, cfg: dict, nav: float) -> dict:
    thr = cfg["low_score_threshold"]
    order = "ABCD"
    low = (
        exposure.filter(
            pl.col("grade").map_elements(
                lambda g: g is not None and order.index(g) >= order.index(thr),
                return_dtype=pl.Boolean,
            )
        )
        if exposure.height
        else exposure
    )
    total = float(low["exposure_usd"].sum()) if low.height else 0.0
    return {
        "low_score_exposure_usd": total,
        "share_nav": total / nav,
        "limit_share_nav": cfg["low_score_aggregate_limit"],
        "breach": total > cfg["low_score_aggregate_limit"] * nav,
    }


def stablecoin_exposure(book: dict, stables: pl.DataFrame | None) -> pl.DataFrame:
    """Balances and collateral per stablecoin with the current discount to par."""
    rows = []
    disc = {}
    mech = {}
    if stables is not None and stables.height:
        for r in stables.to_dicts():
            disc[r["symbol"]] = r.get("discount_to_par")
            mech[r["symbol"]] = r.get("peg_mechanism")
    agg: dict[str, float] = {}
    for c in book.get("collateral", []):
        agg[c["stablecoin"]] = agg.get(c["stablecoin"], 0.0) + float(c["usd"])
    for s, usd in agg.items():
        rows.append(
            {
                "stablecoin": s,
                "exposure_usd": usd,
                "share_nav": usd / book["nav_usd"],
                "discount_to_par": disc.get(s),
                "peg_mechanism": mech.get(s),
                "note": "a de-peg is a margin event on every position collateralised in it",
            }
        )
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "stablecoin": pl.Utf8,
                "exposure_usd": pl.Float64,
                "share_nav": pl.Float64,
                "discount_to_par": pl.Float64,
                "peg_mechanism": pl.Utf8,
                "note": pl.Utf8,
            }
        )
    )
