"""Work order 10 §2 — demand-side flows, the buying the system did not see.

Stablecoin supply grew +0.7% through a +7% rally: the proxy missed the buyer. This adds the demand
series the notes named but never put on the page. Two are computable from data already held:
  - Coinbase premium: Coinbase spot vs Binance spot, in bp, z-scored — a US-demand proxy;
  - exchange net flow: CoinMetrics FlowOut − FlowIn (net coin leaving exchanges = accumulation).
Two are not fetched: US spot ETF net creations (no reliable free daily source; the notes forbid
scraping fragile issuer pages) and exchange stablecoin inflow / SIF (not in the CoinMetrics
community tier). They are named as covariates and marked unavailable rather than scraped.
"""

from __future__ import annotations

import numpy as np
import polars as pl

ASSETS = ("BTC", "ETH")
PREMIUM_Z_WINDOW = 90
FLOW_SUMS = (7, 20)


def coinbase_premium(prices: pl.DataFrame) -> list[dict]:
    """Daily Coinbase-vs-Binance close premium in basis points, z-scored over a trailing window.
    A persistent positive premium is US spot demand leading offshore — the buyer the stablecoin
    proxy missed. Causal: the z uses only the trailing window up to each day."""
    if prices is None or not prices.height:
        return []
    out = []
    for base in ASSETS:
        p = prices.filter((pl.col("base") == base) & pl.col("venue").is_in(["coinbase", "binance"]))
        if not p.height:
            continue
        w = (
            p.select("date", "venue", "close")
            .unique(subset=["date", "venue"], keep="last")
            .pivot(values="close", index="date", on="venue")
            .sort("date")
        )
        if "coinbase" not in w.columns or "binance" not in w.columns:
            continue
        w = w.drop_nulls(["coinbase", "binance"])
        if w.height < PREMIUM_Z_WINDOW + 5:
            continue
        prem = (w["coinbase"].to_numpy() / w["binance"].to_numpy() - 1.0) * 1e4  # bp
        n = len(prem)
        z = np.nan
        if n >= PREMIUM_Z_WINDOW:
            win = prem[-PREMIUM_Z_WINDOW:]
            sd = win.std()
            z = float((prem[-1] - win.mean()) / sd) if sd > 0 else 0.0
        dates = w["date"].to_list()
        out.append({
            "base": base, "date": str(dates[-1]), "premium_bp": round(float(prem[-1]), 2),
            "premium_z": round(z, 2) if np.isfinite(z) else None,
            "premium_z_window": PREMIUM_Z_WINDOW,
            "series": [{"date": str(dates[i]), "bp": round(float(prem[i]), 2)} for i in range(max(0, n - 120), n)],
        })
    return out


def exchange_net_flow(onchain: pl.DataFrame) -> list[dict]:
    """Net USD value of coin leaving exchanges (FlowOut − FlowIn), summed over 7 and 20 days.
    Positive = net withdrawal = accumulation / reduced sell-side inventory; negative = coins moving
    onto exchanges = potential sell pressure."""
    if onchain is None or not onchain.height:
        return []
    out = []
    for base in ASSETS:
        o = (
            onchain.filter(pl.col("asset") == base)
            .select("date", "FlowInExUSD", "FlowOutExUSD")
            .drop_nulls()
            .sort("date")
        )
        if o.height < max(FLOW_SUMS) + 1:
            continue
        net = (o["FlowOutExUSD"].to_numpy() - o["FlowInExUSD"].to_numpy())
        row = {"base": base, "date": str(o["date"].to_list()[-1])}
        for s in FLOW_SUMS:
            row[f"net_flow_{s}d_usd"] = round(float(net[-s:].sum()), 0)
        out.append(row)
    return out


def block(prices: pl.DataFrame, onchain: pl.DataFrame) -> dict:
    """The panel-3 demand block: the two computable series plus the two named-but-unavailable ones,
    each flagged so a reader knows what is measured and what is missing (§2)."""
    return {
        "coinbase_premium": coinbase_premium(prices),
        "exchange_net_flow": exchange_net_flow(onchain),
        "etf_flow": {"available": False,
                     "note": "US spot ETF net creations: named as a demand covariate but not fetched "
                             "— no reliable free daily source and the notes forbid scraping fragile "
                             "issuer pages. Candidate regressor for the resistance model once a "
                             "source clears."},
        "stablecoin_inflow_sif": {"available": False,
                                  "note": "exchange stablecoin inflow (SIF): not in the CoinMetrics "
                                          "community tier; exchange BTC/ETH net flow is shown instead."},
    }
