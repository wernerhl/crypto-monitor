"""Trade structures (notes Section 5 in the numbering of the front page, "Trade Structures").

* `annualised_basis` — b = (F − P)/P × 365/(T − t) (eq. 5.1 of that section).
* `funding_carry` — expected funding stream approximated by the 30-day trailing mean of the
  OI-weighted annualised funding shrunk toward zero (`shrink_factor`).
* `vol_premium` — VRP in variance units and as a vol spread √IV² − √RV² for the majors.
* `unlock_short` — candidates: flagged cliffs (Rule 5.1) 2–4 weeks ahead with the crowding
  warning z^FR < −1.
Every structure carries gross rate, cost, net rate, execution venue and that venue's score,
and the dominant risk named in the notes.
"""

from __future__ import annotations

import math
from datetime import date, datetime

import polars as pl


def annualised_basis(f: float, p: float, expiry: datetime, now: datetime) -> float | None:
    days = (expiry - now).total_seconds() / 86400.0
    if days <= 1 or p <= 0:
        return None
    return (f - p) / p * 365.0 / days


def basis_table(
    marks: pl.DataFrame,
    spot: dict[str, float],
    now: datetime,
    fees: dict[str, dict],
    venue_scores: dict[str, str],
    stable_borrow: float,
    funding_ann: dict[str, float] | None = None,
) -> pl.DataFrame:
    """One row per dated future with ≥ 7 days to expiry. b > 0: cash-and-carry (long spot, short
    future) with cost = stablecoin financing + fees. b < 0 (backwardation): the live structure is
    the REVERSE carry (short spot or short perp, long future): gross = −b, cost = fees plus the
    funding paid on a short perp when funding is positive (or the spot borrow), dominant risk a
    short squeeze on the short leg and the venue (A6)."""
    rows = []
    fa = funding_ann or {}
    for r in marks.to_dicts():
        p = spot.get(r["base"])
        if not p:
            continue
        b = annualised_basis(r["mark_price"], p, r["expiry"], now)
        if b is None:
            continue
        days = (r["expiry"] - now).total_seconds() / 86400.0
        fee = fees.get(r["venue"], {}).get("taker", 0.0005)
        fee_ann = 4 * fee * 365.0 / days  # open + close on both legs
        if b >= 0:
            cost = stable_borrow + fee_ann
            rows.append(
                {
                    "structure": "cash-and-carry basis",
                    "asset": r["base"],
                    "instrument": r["symbol"],
                    "venue": r["venue"],
                    "venue_score": venue_scores.get(r["venue"]),
                    "expiry": r["expiry"],
                    "days": days,
                    "gross_ann": b,
                    "cost_ann": cost,
                    "net_ann": b - cost,
                    "dominant_risk": "counterparty risk on the futures venue; margin calls on the short leg in a squeeze; basis blow-out if closed early",
                    "source": r["source"],
                }
            )
        else:
            f_paid = max(fa.get(r["base"], 0.0), 0.0)  # a short perp pays positive funding
            cost = fee_ann + f_paid
            rows.append(
                {
                    "structure": "reverse carry (backwardation)",
                    "asset": r["base"],
                    "instrument": f"long {r['symbol']} / short perp",
                    "venue": r["venue"],
                    "venue_score": venue_scores.get(r["venue"]),
                    "expiry": r["expiry"],
                    "days": days,
                    "gross_ann": -b,
                    "cost_ann": cost,
                    "net_ann": -b - cost,
                    "dominant_risk": "short squeeze on the short leg (spot borrow recall or perp funding spike); venue risk on both legs; front-month backwardation is a post-deleveraging reading, not a carry to hold through a rally",
                    "source": r["source"],
                }
            )
    schema = {
        "structure": pl.Utf8,
        "asset": pl.Utf8,
        "instrument": pl.Utf8,
        "venue": pl.Utf8,
        "venue_score": pl.Utf8,
        "expiry": pl.Datetime("us", "UTC"),
        "days": pl.Float64,
        "gross_ann": pl.Float64,
        "cost_ann": pl.Float64,
        "net_ann": pl.Float64,
        "dominant_risk": pl.Utf8,
        "source": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def funding_carry(
    fd: pl.DataFrame,
    as_of: date,
    window: int,
    shrink: float,
    fees: dict[str, dict],
    venue_scores: dict[str, str],
    best_venue: dict[str, str],
    stable_borrow: float,
    z_fr: dict[str, float | None],
) -> pl.DataFrame:
    """Per base: gross = shrink × mean_{30d}(funding_ann); cost = stablecoin borrow + annualised
    fees (assume one round trip per 30 days); net; the venue where the short perp sits."""
    rows = []
    for (base,), g in fd.sort("date").group_by("base", maintain_order=True):
        h = g.filter(pl.col("date") <= as_of).tail(window)
        if not h.height:
            continue
        mean = (
            float(h["funding_ann"].drop_nulls().mean())
            if h["funding_ann"].drop_nulls().len()
            else None
        )
        if mean is None or (isinstance(mean, float) and math.isnan(mean)):
            continue
        gross = shrink * mean
        venue = best_venue.get(base, "binance")
        fee = fees.get(venue, {}).get("taker", 0.0005)
        cost = stable_borrow + 4 * fee * 365.0 / 30.0
        rows.append(
            {
                "structure": "funding carry",
                "asset": base,
                "instrument": f"{base} spot long / perp short",
                "venue": venue,
                "venue_score": venue_scores.get(venue),
                "expiry": None,
                "days": None,
                "gross_ann": gross,
                "cost_ann": cost,
                "net_ann": gross - cost,
                "dominant_risk": "funding turns negative; venue risk; margin on the short leg. Most attractive when Rule 4.1 fires, least after capitulation",
                "source": f"funding_daily (n={h.height}, mean {mean:.3f} shrunk ×{shrink}); z_fr={z_fr.get(base)}",
            }
        )
    schema = {
        "structure": pl.Utf8,
        "asset": pl.Utf8,
        "instrument": pl.Utf8,
        "venue": pl.Utf8,
        "venue_score": pl.Utf8,
        "expiry": pl.Datetime("us", "UTC"),
        "days": pl.Float64,
        "gross_ann": pl.Float64,
        "cost_ann": pl.Float64,
        "net_ann": pl.Float64,
        "dominant_risk": pl.Utf8,
        "source": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def vol_premium(
    options_metrics: list[dict],
    phi: float | None,
    rule_43_fired: dict[str, bool | None],
    venue_scores: dict[str, str],
) -> pl.DataFrame:
    """Short-vol on the majors: gross = VRP (variance units) expressed as the vol spread
    IV − √RV; forbidden when Rule 4.3 fires. Cost: Deribit fees (0.03 % of underlying per leg,
    capped at 12.5 % of premium) approximated as 0.06 % of notional per month."""
    rows = []
    for m in options_metrics:
        if m.get("vrp") is None or m.get("iv_1m") is None or m.get("rv30_var") is None:
            continue
        iv, rv = m["iv_1m"], math.sqrt(max(m["rv30_var"], 0.0))
        forbidden = rule_43_fired.get(m["currency"])
        rows.append(
            {
                "structure": "volatility selling",
                "asset": m["currency"],
                "instrument": f"{m['currency']} 1-month strangle / variance",
                "venue": "deribit",
                "venue_score": venue_scores.get("deribit"),
                "expiry": None,
                "days": 30.0,
                "gross_ann": (iv - rv) * 12 / 12,
                "cost_ann": 0.0006 * 12,
                "net_ann": (iv - rv) - 0.0006 * 12,
                "dominant_risk": (
                    "FORBIDDEN by Rule 4.3 (VRP < 0 and Φ > 1)"
                    if forbidden
                    else "short gamma: size by the 3σ loss under the high-vol state's σ, not the current one"
                )
                + f"; VRP={m['vrp']:.4f}, Φ={phi}",
                "source": "options_metrics",
            }
        )
    schema = {
        "structure": pl.Utf8,
        "asset": pl.Utf8,
        "instrument": pl.Utf8,
        "venue": pl.Utf8,
        "venue_score": pl.Utf8,
        "expiry": pl.Datetime("us", "UTC"),
        "days": pl.Float64,
        "gross_ann": pl.Float64,
        "cost_ann": pl.Float64,
        "net_ann": pl.Float64,
        "dominant_risk": pl.Utf8,
        "source": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def unlock_short(
    cliffs: pl.DataFrame,
    fires: dict[tuple[str, str], bool | None],
    as_of: date,
    tiers: dict[str, int],
    symbols: dict[str, str],
    z_fr: dict[str, float | None],
    fd: pl.DataFrame | None,
    best_venue: dict[str, str],
    venue_scores: dict[str, str],
    fees: dict[str, dict],
) -> pl.DataFrame:
    """RETIRED (review decision 1, 2026-09-08): not built into the trade table. Kept so the
    cliff study can re-run it. Exhibit: docs/notes/pre_unlock_drift.md. Short the perp on a Tier 1/2 asset with a flagged cliff 14–28 days ahead, sized by
    ESP^vol. Cost: funding paid if funding is negative (crowded short) + fees. Warning when
    z^FR < −1."""
    rows = []
    for r in cliffs.to_dicts():
        sym = symbols.get(r["id"], r["id"])
        days = (r["date"] - as_of).days
        if (
            tiers.get(r["id"]) not in (1, 2)
            or not (14 <= days <= 28)
            or not fires.get((sym, str(r["date"])))
        ):
            continue
        z = z_fr.get(sym)
        fr = None
        if fd is not None:
            h = fd.filter((pl.col("base") == sym) & (pl.col("date") <= as_of)).sort("date").tail(30)
            fr = (
                float(h["funding_ann"].drop_nulls().mean())
                if h.height and h["funding_ann"].drop_nulls().len()
                else None
            )
        venue = best_venue.get(sym, "binance")
        fee = fees.get(venue, {}).get("taker", 0.0005)
        # a short receives funding when positive and pays it when negative
        cost = (max(-(fr or 0.0), 0.0)) + 4 * fee * 365.0 / max(days, 1)
        rows.append(
            {
                "structure": "unlock short",
                "asset": sym,
                "instrument": f"{sym} perp short into {r['date']}",
                "venue": venue,
                "venue_score": venue_scores.get(venue),
                "expiry": datetime.combine(r["date"], datetime.min.time()),
                "days": float(days),
                "gross_ann": None,
                "cost_ann": cost,
                "net_ann": None,
                "dominant_risk": (
                    "WARNING crowded short (z_FR < −1); " if (z is not None and z < -1) else ""
                )
                + f"squeeze on a crowded short; unlock re-locked or OTC-placed; beta of a short in a rising market (hedge with sector/index). Cliff {r['share_of_float'] * 100 if r['share_of_float'] else float('nan'):.2f} % of float, {r['days_of_volume']:.1f} days of volume"
                if r.get("days_of_volume") is not None
                else "squeeze on a crowded short; unlock re-locked or OTC-placed; beta of a short in a rising market",
                "source": "cliffs+funding_daily",
            }
        )
    schema = {
        "structure": pl.Utf8,
        "asset": pl.Utf8,
        "instrument": pl.Utf8,
        "venue": pl.Utf8,
        "venue_score": pl.Utf8,
        "expiry": pl.Datetime("us"),
        "days": pl.Float64,
        "gross_ann": pl.Float64,
        "cost_ann": pl.Float64,
        "net_ann": pl.Float64,
        "dominant_risk": pl.Utf8,
        "source": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
