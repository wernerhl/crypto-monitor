"""Liquidity gate (notes Section 7).

Pure functions:
* `depth_from_levels` — D_v(δ) = Σ p·q over resting orders within δ of mid (Definition 7.1),
  per side, with the coverage actually reached by the snapshot (books are truncated at the
  venue's level limit; a truncated side makes D a lower bound).
* `aggregate_depth` — Σ_v D_v(δ) over venues with genuine books, intraday median per day.
* `benford_stats` — first-digit χ² of trade sizes against Benford's law.
* `amihud` — mean(|r_d| / volume_d) over a window.
* `impact_cost` — Cost(Q) ≈ s/2 + Y σ_d sqrt(Q / ADV_real)  (eq. 7.2).
* `days_to_liquidate` — DTL = Q / (ρ ADV_real)  (eq. 7.3).
* `gate` — Rule 7.1: DTL(ρ=0.1) ≤ 3 days and round-trip impact ≤ ¼ of expected net return.
* `wash_filters` — volume/depth ratio vs reference venues, Benford p-value, corr(volume, |r|).
"""

from __future__ import annotations

import math
from datetime import date, timedelta

import polars as pl

BENFORD = [math.log10(1 + 1 / d) for d in range(1, 10)]


def depth_from_levels(
    bids: list[tuple[float, float]], asks: list[tuple[float, float]], delta: float = 0.02
) -> dict:
    """bids/asks as (price, qty) lists, best first. Returns mid, spread_bps, per-side USD depth
    within δ, coverage reached per side (%), truncated flag, and level count."""
    if not bids or not asks:
        raise ValueError("empty book side")
    bb, ba = bids[0][0], asks[0][0]
    mid = (bb + ba) / 2.0
    lo, hi = mid * (1 - delta), mid * (1 + delta)
    bid_usd = sum(p * q for p, q in bids if p >= lo)
    ask_usd = sum(p * q for p, q in asks if p <= hi)
    cov_bid = (1 - bids[-1][0] / mid) * 100
    cov_ask = (asks[-1][0] / mid - 1) * 100
    truncated = cov_bid < delta * 100 or cov_ask < delta * 100
    return {
        "mid": mid,
        "spread_bps": (ba - bb) / mid * 1e4,
        "bid_depth_usd": bid_usd,
        "ask_depth_usd": ask_usd,
        "depth_usd": bid_usd + ask_usd,
        "delta": delta,
        "coverage_bid_pct": cov_bid,
        "coverage_ask_pct": cov_ask,
        "truncated": truncated,
        "levels": len(bids) + len(asks),
    }


def benford_stats(sizes_usd: list[float]) -> dict:
    """First-digit test on trade notionals. χ² with 8 dof against Benford shares; p-value via
    the survival function of χ²₈ (closed form for even dof). Fewer than 50 trades -> p = None."""
    digits = [int(str(f"{abs(s):.6e}")[0]) for s in sizes_usd if s and abs(s) >= 1e-9]
    n = len(digits)
    shares = [digits.count(d) / n if n else 0.0 for d in range(1, 10)]
    if n < 50:
        return {"n": n, "chi2": float("nan"), "p": None, "shares": shares}
    chi2 = sum((n * s - n * b) ** 2 / (n * b) for s, b in zip(shares, BENFORD, strict=True))
    # survival of chi-square with k=8 (even): P = e^{-x/2} Σ_{i=0}^{3} (x/2)^i / i!
    x = chi2 / 2.0
    p = math.exp(-x) * sum(x**i / math.factorial(i) for i in range(4))
    return {"n": n, "chi2": chi2, "p": p, "shares": shares}


def aggregate_depth(
    depth: pl.DataFrame, venues_ok: dict[str, set[str]] | None = None
) -> pl.DataFrame:
    """Per base and day: intraday median of Σ_v D_v(0.02) across snapshots, only venues whose
    books are genuine (`venues_ok[base]` when given; else all). Also whether any venue was
    truncated (→ the aggregate is a lower bound)."""
    if depth is None or not depth.height:
        return pl.DataFrame(
            schema={
                "date": pl.Date,
                "base": pl.Utf8,
                "depth_2pct_usd": pl.Float64,
                "depth_lower_bound": pl.Boolean,
                "n_snapshots": pl.Int64,
                "venues": pl.List(pl.Utf8),
            }
        )
    d = depth.with_columns(pl.col("ts").dt.date().alias("date"))
    if venues_ok is not None:
        d = d.filter(
            pl.struct(["base", "venue"]).map_elements(
                lambda s: s["venue"] in venues_ok.get(s["base"], set()), return_dtype=pl.Boolean
            )
        )
    d = d.with_columns(
        pl.col("ts").dt.truncate("1h").alias("ts")
    )  # venues are polled seconds apart
    snap = d.group_by("date", "base", "ts").agg(
        pl.col("depth_usd").sum().alias("d"),
        pl.col("truncated").any().alias("trunc"),
        pl.col("venue").unique().sort().alias("venues"),
    )
    return snap.group_by("date", "base").agg(
        pl.col("d").median().alias("depth_2pct_usd"),
        pl.col("trunc").any().alias("depth_lower_bound"),
        pl.len().cast(pl.Int64).alias("n_snapshots"),
        pl.col("venues").first(),
    )


def amihud(daily: pl.DataFrame, window: int = 30) -> pl.DataFrame:
    """Amihud illiquidity per base: mean over the window of |r_d| / volume_d (USD), returns
    (base, amihud, n). `daily` needs base, date, close, volume_quote (summed across venues)."""
    d = daily.sort("base", "date").with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("base")).log().abs().alias("absr")
    )
    d = d.filter(pl.col("volume_quote") > 0).with_columns(
        (pl.col("absr") / pl.col("volume_quote")).alias("ratio")
    )
    return d.group_by("base").agg(
        pl.col("ratio").tail(window).mean().alias("amihud"),
        pl.col("ratio").tail(window).count().cast(pl.Int64).alias("n"),
    )


def impact_cost(
    q_usd: float, adv_real_usd: float, sigma_d: float, spread_bps: float, y: float
) -> float:
    """Eq. 7.2: Cost(Q) ≈ s/2 + Y σ_d sqrt(Q / ADV_real), as a fraction of notional."""
    if adv_real_usd <= 0:
        return float("inf")
    return spread_bps / 2e4 + y * sigma_d * math.sqrt(q_usd / adv_real_usd)


def days_to_liquidate(q_usd: float, adv_real_usd: float, rho: float) -> float:
    """Eq. 7.3: DTL = Q / (ρ ADV_real)."""
    return float("inf") if adv_real_usd <= 0 else q_usd / (rho * adv_real_usd)


def gate(
    q_usd: float,
    adv_real_usd: float,
    sigma_d: float,
    spread_bps: float,
    expected_net_return: float | None,
    cfg: dict,
) -> dict:
    """Rule 7.1. `cfg` = thresholds.yaml['rules']['gate'] + ['liquidity']. Round-trip cost =
    2 × Cost(Q). When no expected return is available only the DTL leg is tested."""
    dtl = days_to_liquidate(q_usd, adv_real_usd, cfg["participation_rate"])
    cost = impact_cost(q_usd, adv_real_usd, sigma_d, spread_bps, cfg["impact_Y"])
    rt = 2 * cost
    dtl_ok = dtl <= cfg["dtl_max_days"]
    cost_ok = (
        None
        if expected_net_return is None
        else rt <= cfg["impact_cost_share_of_net_return_max"] * expected_net_return
    )
    passed = dtl_ok and (cost_ok is not False)
    return {
        "dtl_days": dtl,
        "impact_cost": cost,
        "round_trip_cost": rt,
        "dtl_ok": dtl_ok,
        "cost_ok": cost_ok,
        "pass": passed,
    }


def wash_filters(
    venue_day: pl.DataFrame,
    benford_multiple_max: float = 3.0,
    vol_depth_multiple_max: float = 5.0,
    corr_min: float = 0.0,
    reference_venues: tuple[str, ...] = ("coinbase", "kraken"),
) -> pl.DataFrame:
    """Per (date, base, venue): three filters, each null when its input is missing.

    * volume/depth: venue's daily volume ÷ its median D(0.02), compared with the median of the
      same ratio on reference venues; fails when > vol_depth_multiple_max × reference.
    * Benford: trade notionals cluster on the price's leading digit, so an absolute χ² rejects
      every venue; the test is relative: the venue's deviation χ²/n fails when it exceeds
      benford_multiple_max × the median deviation on the reference venues.
    * volume-|return| correlation over 7 days of hourly bars (genuine volume rises with |r|);
      fails when ≤ corr_min.
    `pass` = at most one filter failed (a single failing test occurs on genuine venues, e.g.
    Binance's quantised BTC trade sizes; two independent failures mark the venue). Missing
    filters do not fail. Columns in: date, base, venue,
    volume_quote, depth_usd, benford_dev (χ²/n), vol_absret_corr."""
    v = venue_day.with_columns(
        pl.when(
            pl.col("truncated").fill_null(False)
            if "truncated" in venue_day.columns
            else pl.lit(False)
        )
        .then(None)
        .otherwise(pl.col("volume_quote") / pl.col("depth_usd"))
        .alias("vol_depth_ratio")  # a truncated book understates depth: skip the test
    )
    ref = (
        v.filter(pl.col("venue").is_in(reference_venues))
        .group_by("date", "base")
        .agg(
            pl.col("vol_depth_ratio").median().alias("ref_ratio"),
            pl.col("benford_dev").median().alias("ref_benford_dev"),
        )
    )
    v = v.join(ref, on=["date", "base"], how="left")
    return (
        v.with_columns(
            pl.when(pl.col("ref_ratio").is_null() | pl.col("vol_depth_ratio").is_null())
            .then(None)
            .otherwise(pl.col("vol_depth_ratio") > vol_depth_multiple_max * pl.col("ref_ratio"))
            .alias("vol_depth_fail"),
            pl.when(pl.col("benford_dev").is_null() | pl.col("ref_benford_dev").is_null())
            .then(None)
            .otherwise(pl.col("benford_dev") > benford_multiple_max * pl.col("ref_benford_dev"))
            .alias("benford_fail"),
            pl.when(pl.col("vol_absret_corr").is_null())
            .then(None)
            .otherwise(pl.col("vol_absret_corr") <= corr_min)
            .alias("corr_fail"),
        )
        .with_columns(
            (
                pl.col("vol_depth_fail").fill_null(False).cast(pl.Int64)
                + pl.col("benford_fail").fill_null(False).cast(pl.Int64)
                + pl.col("corr_fail").fill_null(False).cast(pl.Int64)
            ).alias("n_fail")
        )
        .with_columns((pl.col("n_fail") <= 1).alias("pass"))
    )


def volume_absret_corr(hourly: pl.DataFrame, days: int = 7) -> pl.DataFrame:
    """Pearson corr(volume_quote, |Δlog close|) per (venue, base) over the last `days` of hourly bars."""
    if hourly is None or not hourly.height:
        return pl.DataFrame(
            schema={"venue": pl.Utf8, "base": pl.Utf8, "vol_absret_corr": pl.Float64, "n": pl.Int64}
        )
    cutoff = hourly["ts"].max() - timedelta(days=days)
    h = hourly.filter(pl.col("ts") > cutoff).sort("venue", "base", "ts")
    h = h.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("venue", "base")).log().abs().alias("absr")
    )
    h = h.with_columns(
        pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias("vq")
    ).drop_nulls(["absr", "vq"])
    return h.group_by("venue", "base").agg(
        pl.corr("vq", "absr").alias("vol_absret_corr"), pl.len().cast(pl.Int64).alias("n")
    )


def real_adv(
    daily_venue_volume: pl.DataFrame, passing: pl.DataFrame, as_of: date, window: int = 30
) -> pl.DataFrame:
    """ADV_real per base: trailing mean of Σ_v volume over venues that pass the wash filters on
    that day (`passing`: date, base, venue, pass). Also the reported ADV over all venues."""
    d = daily_venue_volume.filter(pl.col("date") > as_of - timedelta(days=window))
    j = d.join(
        passing.select("date", "base", "venue", "pass"), on=["date", "base", "venue"], how="left"
    )
    # days without a filter row (before the book history started) use the venue's latest status
    latest = (
        passing.sort("date")
        .group_by("base", "venue")
        .agg(pl.col("pass").last().alias("pass_latest"))
    )
    j = (
        j.join(latest, on=["base", "venue"], how="left")
        .with_columns(pl.coalesce(pl.col("pass"), pl.col("pass_latest")).alias("pass"))
        .drop("pass_latest")
    )
    day = j.group_by("date", "base").agg(
        pl.col("volume_quote").sum().alias("reported"),
        pl.col("volume_quote").filter(pl.col("pass").fill_null(False)).sum().alias("real"),
        pl.col("venue")
        .filter(pl.col("pass").fill_null(False))
        .unique()
        .sort()
        .alias("venues_passing"),
    )
    return day.group_by("base").agg(
        pl.col("reported").mean().alias("adv_reported_usd"),
        pl.col("real").mean().alias("adv_real_usd"),
        pl.col("date").n_unique().cast(pl.Int64).alias("n_days"),
        pl.col("venues_passing").last(),
    )
