"""Walk-forward hit rates of the trigger rules (notes §4.5, §10) computed on the archive.

A rule "hits" when the forward return over the rule's horizon has the sign the rule's
action implies: 4.1 crowded long → negative h-day return; 4.2 capitulation → positive 1–5 day
return; 4.3 vol underpricing → realised vol over the next 30 days exceeds the implied vol
at the flag; 5.1 cliff → negative return over the 14 days before the cliff. Only rule
evaluations with `fired = True` count; the sample size is reported with every rate."""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

HORIZONS = {"4.1": 5, "4.1p": 5, "4.2": 5, "4.2p": 5, "4.3": 30, "5.1": 14}


def forward_return(prices: pl.DataFrame, base: str, d: date, h: int) -> float | None:
    p = prices.filter(pl.col("base") == base).sort("date")
    if not p.height:
        return None
    p0 = p.filter(pl.col("date") <= d).tail(1)
    p1 = p.filter(pl.col("date") <= d + timedelta(days=h)).tail(1)
    if not p0.height or not p1.height or p1["date"][0] <= p0["date"][0]:
        return None
    return float(np.log(p1["close"][0] / p0["close"][0]))


def hit_rates(
    fires: pl.DataFrame,
    prices: pl.DataFrame,
    iv_at_flag: dict | None = None,
    as_of: date | None = None,
) -> pl.DataFrame:
    """One row per rule: n fired, hits, hit rate, mean forward return, horizon."""
    rows = []
    f = (
        fires.filter(pl.col("fired"))
        .with_columns(pl.col("ts").dt.date().alias("d"))
        .unique(subset=["rule_id", "asset", "d"])
    )
    for rid, h in HORIZONS.items():
        g = f.filter(pl.col("rule_id") == rid)
        outcomes = []
        for r in g.to_dicts():
            if as_of is not None and r["d"] + timedelta(days=h) > as_of:
                continue  # horizon not yet observable
            if rid == "5.1":
                # cliff date is in the inputs; evaluate the 14 days before it
                try:
                    import json

                    cd = date.fromisoformat(json.loads(r["inputs"])["unlock_date"])
                except Exception:
                    continue
                if as_of is not None and cd > as_of:
                    continue
                fr = forward_return(prices, r["asset"], cd - timedelta(days=14), 14)
                if fr is not None:
                    outcomes.append(fr < 0)
            elif rid == "4.3":
                p = prices.filter(pl.col("base") == r["asset"]).sort("date")
                fut = p.filter(
                    (pl.col("date") > r["d"]) & (pl.col("date") <= r["d"] + timedelta(days=30))
                )
                if fut.height >= 20:
                    rv = float(
                        np.sqrt(np.mean(np.diff(np.log(fut["close"].to_numpy())) ** 2) * 365)
                    )
                    iv = (iv_at_flag or {}).get((r["asset"], r["d"]))
                    if iv is not None:
                        outcomes.append(rv > iv)
            else:
                fr = forward_return(prices, r["asset"], r["d"], h)
                if fr is not None:
                    outcomes.append(fr < 0 if rid.startswith("4.1") else fr > 0)
        n = len(outcomes)
        rows.append(
            {
                "rule_id": rid,
                "horizon_days": h,
                "n": n,
                "hits": int(sum(outcomes)),
                "hit_rate": (sum(outcomes) / n) if n else None,
                "se": (
                    np.sqrt(max(sum(outcomes) / n * (1 - sum(outcomes) / n), 0) / n) if n else None
                ),
            }
        )
    return pl.DataFrame(rows)
