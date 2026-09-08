"""Historical study of unlock cliffs (work order A8; notes §5, Rule 5.1).

For every scheduled cliff since 2021 with price coverage: the return over the 14 days before
the cliff (the window Rule 5.1 trades) and the 14 days after, the size of the event as a
share of float and in days of average volume, and the dominant recipient class. A "hit" is a
negative 14-day pre-cliff return, the same definition the live hit-rate table uses. The
thresholds (1 % of float, 2 days of volume) come from config/thresholds.yaml and are not
touched here: the study reports how the fixed rule would have done, it does not fit it.

Honesty notes carried into every row:
* `float_basis`: float at the cliff date is backed out of today's circulating supply by
  removing every scheduled cliff between the cliff date and today and the current linear
  unlock rate times the elapsed days. That is an approximation (buybacks, burns and
  re-issuance are ignored) and is flagged as such.
* `adv_basis`: average daily quote volume over the 30 days ending 15 days before the cliff,
  summed over the venues that pass the CURRENT wash filters for that base when a wash row
  exists ("wash-filtered venues, current pass set"), otherwise over every venue with klines
  ("exchange klines, unfiltered"). Wash filters cannot be evaluated historically because the
  order-book depth they need is not archived.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

PRE_DAYS = 14
POST_DAYS = 14
ADV_DAYS = 30
ADV_GAP_DAYS = 15
SHARE_BUCKETS = [
    (0.0, 0.005, "< 0.5 %"),
    (0.005, 0.01, "0.5–1 %"),
    (0.01, 0.02, "1–2 %"),
    (0.02, 0.05, "2–5 %"),
    (0.05, 10.0, "> 5 %"),
]
DOV_BUCKETS = [(0.0, 1.0, "< 1 d"), (1.0, 2.0, "1–2 d"), (2.0, 5.0, "2–5 d"), (5.0, 1e9, "> 5 d")]


def _bucket(x: float | None, buckets) -> str | None:
    if x is None or not np.isfinite(x):
        return None
    for lo, hi, lab in buckets:
        if lo <= x < hi:
            return lab
    return None


class _Px:
    """Close and volume series per base with O(log n) date lookup."""

    def __init__(self, prices: pl.DataFrame) -> None:
        self.d: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        for base, g in prices.sort("date").group_by("base"):
            b = base[0] if isinstance(base, tuple) else base
            self.d[b] = (
                g["date"].cast(pl.Int32).to_numpy(),
                g["close"].to_numpy().astype(float),
                g["volume_quote"].fill_null(0.0).to_numpy().astype(float),
            )

    def close_at(self, base: str, d: date, tol_days: int = 3) -> float | None:
        s = self.d.get(base)
        if s is None:
            return None
        di = (d - date(1970, 1, 1)).days
        i = np.searchsorted(s[0], di, side="right") - 1
        if i < 0 or di - s[0][i] > tol_days:
            return None
        c = s[1][i]
        return float(c) if np.isfinite(c) and c > 0 else None

    def adv(self, base: str, end: date, days: int) -> tuple[float | None, int]:
        s = self.d.get(base)
        if s is None:
            return None, 0
        e = (end - date(1970, 1, 1)).days
        m = (s[0] > e - days) & (s[0] <= e)
        n = int(m.sum())
        if n < days // 2:
            return None, n
        return float(np.mean(s[2][m])), n


def event_table(
    cliffs: pl.DataFrame,
    venue_prices: pl.DataFrame,
    symbol_of: dict[str, str],
    float_now: dict[str, float],
    unlocks_per_day: dict[str, float],
    as_of: date,
    start: date = date(2021, 1, 1),
    wash_pass_venues: dict[str, list[str]] | None = None,
) -> pl.DataFrame:
    """One row per (id, cliff date). `cliffs` has id, date, kind, recipient_class, amount.
    `venue_prices` is the prices_daily table (date, venue, base, close, volume_quote)."""
    ev = (
        cliffs.filter(
            (pl.col("kind") == "cliff")
            & (pl.col("date") >= start)
            & (pl.col("date") <= as_of - timedelta(days=POST_DAYS))
        )
        .filter(pl.col("amount") > 0)  # negative schedule rows are lock-ups or burns, not cliffs
        .group_by("id", "date")
        .agg(
            pl.col("amount").sum().alias("unlock_tokens"),
            pl.col("recipient_class")
            .sort_by(pl.col("amount"), descending=True)
            .first()
            .alias("dominant_class"),
            pl.col("recipient_class").unique().sort().alias("classes"),
        )
        .sort("id", "date")
    )
    # future cliffs per id (for backing out float at the event date)
    later = (
        cliffs.filter((pl.col("kind") == "cliff") & (pl.col("date") <= as_of))
        .group_by("id", "date")
        .agg(pl.col("amount").sum().alias("amt"))
        .sort("id", "date")
    )
    later_by_id: dict[str, tuple[np.ndarray, np.ndarray]] = {}
    for i, g in later.group_by("id"):
        k = i[0] if isinstance(i, tuple) else i
        later_by_id[k] = (
            g["date"].cast(pl.Int32).to_numpy(),
            np.cumsum(g["amt"].to_numpy()[::-1])[::-1],
        )
    vp = venue_prices.with_columns(
        pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias(
            "volume_quote"
        )
    )
    px_all = _Px(
        vp.group_by("date", "base").agg(
            pl.col("close").median().alias("close"),
            pl.col("volume_quote").sum().alias("volume_quote"),
        )
    )
    px_wash: dict[str, _Px] = {}
    wash = wash_pass_venues or {}
    rows = []
    for r in ev.to_dicts():
        i, d = r["id"], r["date"]
        base = symbol_of.get(i) or (i.upper() if i.upper() in px_all.d else None)
        if base is None or base not in px_all.d:
            continue
        p_pre = px_all.close_at(base, d - timedelta(days=PRE_DAYS))
        p0 = px_all.close_at(base, d)
        p_post = px_all.close_at(base, d + timedelta(days=POST_DAYS))
        if p_pre is None or p0 is None:
            continue
        b_pre = px_all.close_at("BTC", d - timedelta(days=PRE_DAYS))
        b0 = px_all.close_at("BTC", d)
        pre = float(np.log(p0 / p_pre))
        post = float(np.log(p_post / p0)) if p_post else None
        pre_rel = pre - float(np.log(b0 / b_pre)) if (b_pre and b0) else None
        # float at the event date, backed out of today's circulating supply
        fl_now = float_now.get(i)
        fl = None
        if fl_now:
            fut = 0.0
            lb = later_by_id.get(i)
            if lb is not None:
                j = np.searchsorted(lb[0], (d - date(1970, 1, 1)).days, side="right")
                fut = float(lb[1][j]) if j < len(lb[1]) else 0.0
            lin = (unlocks_per_day.get(i) or 0.0) * (as_of - d).days
            fl = fl_now - fut - lin
            if fl <= 0 or fl < 0.05 * fl_now:
                fl = None  # the back-out broke down (issuance, burns): no float claim
        share = r["unlock_tokens"] / fl if fl else None
        venues = wash.get(base)
        if venues:
            if base not in px_wash:
                sub = vp.filter((pl.col("base") == base) & pl.col("venue").is_in(venues))
                px_wash[base] = _Px(
                    sub.group_by("date", "base").agg(
                        pl.col("close").median().alias("close"),
                        pl.col("volume_quote").sum().alias("volume_quote"),
                    )
                )
            adv, n_adv = px_wash[base].adv(base, d - timedelta(days=ADV_GAP_DAYS), ADV_DAYS)
            adv_basis = "wash-filtered venues, current pass set"
        else:
            adv, n_adv = px_all.adv(base, d - timedelta(days=ADV_GAP_DAYS), ADV_DAYS)
            adv_basis = "exchange klines, unfiltered"
        usd = r["unlock_tokens"] * p0
        dov = usd / adv if adv else None
        rows.append(
            {
                "id": i,
                "base": base,
                "date": d,
                "unlock_tokens": r["unlock_tokens"],
                "usd_at_cliff": usd,
                "dominant_class": r["dominant_class"] or "unknown",
                "classes": ",".join(r["classes"]),
                "share_of_float": share,
                "float_basis": "backed out of current supply via the schedule" if fl else None,
                "days_of_volume": dov,
                "adv_usd": adv,
                "adv_days": n_adv,
                "adv_basis": adv_basis if adv else None,
                "ret_pre_14d": pre,
                "ret_pre_14d_vs_btc": pre_rel,
                "ret_post_14d": post,
                "hit": pre < 0,
                "share_bucket": _bucket(share, SHARE_BUCKETS),
                "dov_bucket": _bucket(dov, DOV_BUCKETS),
            }
        )
    schema = {
        "id": pl.Utf8,
        "base": pl.Utf8,
        "date": pl.Date,
        "unlock_tokens": pl.Float64,
        "usd_at_cliff": pl.Float64,
        "dominant_class": pl.Utf8,
        "classes": pl.Utf8,
        "share_of_float": pl.Float64,
        "float_basis": pl.Utf8,
        "days_of_volume": pl.Float64,
        "adv_usd": pl.Float64,
        "adv_days": pl.Int64,
        "adv_basis": pl.Utf8,
        "ret_pre_14d": pl.Float64,
        "ret_pre_14d_vs_btc": pl.Float64,
        "ret_post_14d": pl.Float64,
        "hit": pl.Boolean,
        "share_bucket": pl.Utf8,
        "dov_bucket": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def base_rate(
    venue_prices: pl.DataFrame, bases: list[str], start: date, end: date
) -> tuple[float | None, int]:
    """Unconditional share of negative 14-day log returns over the same assets and period:
    the number a cliff hit rate has to beat."""
    p = (
        venue_prices.filter(
            pl.col("base").is_in(bases) & (pl.col("date") >= start) & (pl.col("date") <= end)
        )
        .group_by("date", "base")
        .agg(pl.col("close").median().alias("close"))
        .sort("base", "date")
        .with_columns(
            (pl.col("close").log() - pl.col("close").shift(PRE_DAYS).log()).over("base").alias("r")
        )
        .drop_nulls("r")
    )
    n = p.height
    return (float((p["r"] < 0).mean()) if n else None), n


def _summ(g: pl.DataFrame, kind: str, name: str) -> dict:
    n = g.height
    hits = int(g["hit"].sum()) if n else 0
    hr = hits / n if n else None
    post = g["ret_post_14d"].drop_nulls()
    return {
        "group_kind": kind,
        "group": name,
        "n": n,
        "hits": hits,
        "hit_rate": hr,
        "se": float(np.sqrt(hr * (1 - hr) / n)) if n and hr is not None else None,
        "mean_pre": float(g["ret_pre_14d"].mean()) if n else None,
        "median_pre": float(g["ret_pre_14d"].median()) if n else None,
        "mean_pre_vs_btc": float(g["ret_pre_14d_vs_btc"].drop_nulls().mean())
        if g["ret_pre_14d_vs_btc"].drop_nulls().len()
        else None,
        "mean_post": float(post.mean()) if post.len() else None,
        "median_post": float(post.median()) if post.len() else None,
        "n_post": int(post.len()),
    }


def summarise(events: pl.DataFrame, cliff_th: dict) -> pl.DataFrame:
    """Hit rate and pre/post returns: all cliffs, cliffs meeting the (frozen) Rule 5.1
    thresholds, and breakdowns by dominant recipient class, float-share bucket and
    days-of-volume bucket."""
    rows = []
    if not events.height:
        return pl.DataFrame(
            schema={
                "group_kind": pl.Utf8,
                "group": pl.Utf8,
                "n": pl.Int64,
                "hits": pl.Int64,
                "hit_rate": pl.Float64,
                "se": pl.Float64,
                "mean_pre": pl.Float64,
                "median_pre": pl.Float64,
                "mean_pre_vs_btc": pl.Float64,
                "mean_post": pl.Float64,
                "median_post": pl.Float64,
                "n_post": pl.Int64,
            }
        )
    rows.append(_summ(events, "all", "all cliffs with price coverage"))
    sized = events.filter(
        pl.col("share_of_float").is_not_null() & pl.col("days_of_volume").is_not_null()
    )
    rows.append(_summ(sized, "all", "cliffs with float and volume measured"))
    rule = sized.filter(
        (pl.col("share_of_float") > cliff_th["single_unlock_float_share_min"])
        & (pl.col("days_of_volume") > cliff_th["single_unlock_days_of_volume_min"])
    )
    rows.append(
        _summ(
            rule,
            "rule",
            f"Rule 5.1 (> {cliff_th['single_unlock_float_share_min']:.0%} of float and > {cliff_th['single_unlock_days_of_volume_min']:g} days of volume)",
        )
    )
    rows.append(
        _summ(
            sized.filter(
                ~(
                    (pl.col("share_of_float") > cliff_th["single_unlock_float_share_min"])
                    & (pl.col("days_of_volume") > cliff_th["single_unlock_days_of_volume_min"])
                )
            ),
            "rule",
            "below either threshold",
        )
    )
    for c in sorted(events["dominant_class"].unique().to_list()):
        rows.append(_summ(events.filter(pl.col("dominant_class") == c), "recipient class", c))
    for _, _, lab in SHARE_BUCKETS:
        g = events.filter(pl.col("share_bucket") == lab)
        if g.height:
            rows.append(_summ(g, "share of float", lab))
    for _, _, lab in DOV_BUCKETS:
        g = events.filter(pl.col("dov_bucket") == lab)
        if g.height:
            rows.append(_summ(g, "days of volume", lab))
    for y in sorted(events["date"].dt.year().unique().to_list()):
        rows.append(_summ(events.filter(pl.col("date").dt.year() == y), "year", str(y)))
    return pl.DataFrame(rows)
