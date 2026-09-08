"""Historical study of unlock cliffs (work order A8; notes §5, Rule 5.1).

For every scheduled cliff since 2021 with price coverage: the return over the 14 days before
the cliff (the window Rule 5.1 trades) and the 14 days after, the size of the event as a
share of float and in days of average volume, and the dominant recipient class. A "hit" is a
negative 14-day pre-cliff return, the same definition the live hit-rate table uses. The
thresholds (1 % of float, 2 days of volume) come from config/thresholds.yaml and are not
touched here: the study reports how the fixed rule would have done, it does not fit it.

Work order 2 (item 3) additions: standard errors clustered by cliff week (cliffs bunch on the
1st and 15th and share the market factor), a β-adjusted pre-cliff return using the factor
model's rolling β_MKT for the token as of the cliff date (BTC's 14-day return as the market
move), a placebo of 20 pseudo-cliff dates per token and year drawn from non-cliff days, and a
base rate computed on the same tokens that form the Rule 5.1 subset.

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
    betas: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """One row per (id, cliff date). `cliffs` has id, date, kind, recipient_class, amount.
    `venue_prices` is the prices_daily table (date, venue, base, close, volume_quote).
    `betas` (id, week, beta_MKT) gives the β-adjusted pre-cliff return."""
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
    beta_of = _BetaLookup(betas)
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
        btc_pre = float(np.log(b0 / b_pre)) if (b_pre and b0) else None
        pre_rel = pre - btc_pre if btc_pre is not None else None
        beta = beta_of.at(i, d)
        pre_beta = pre - beta * btc_pre if (beta is not None and btc_pre is not None) else None
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
                "week": d - timedelta(days=d.weekday()),
                "ret_pre_14d": pre,
                "ret_pre_14d_vs_btc": pre_rel,
                "beta_mkt": beta,
                "ret_pre_14d_beta_adj": pre_beta,
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
        "week": pl.Date,
        "ret_pre_14d": pl.Float64,
        "ret_pre_14d_vs_btc": pl.Float64,
        "beta_mkt": pl.Float64,
        "ret_pre_14d_beta_adj": pl.Float64,
        "ret_post_14d": pl.Float64,
        "hit": pl.Boolean,
        "share_bucket": pl.Utf8,
        "dov_bucket": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


class _BetaLookup:
    """β_MKT of a token as of a date: the latest weekly estimate on or before the date."""

    def __init__(self, betas: pl.DataFrame | None) -> None:
        self.d: dict[str, tuple[np.ndarray, np.ndarray]] = {}
        if betas is None or not betas.height or "beta_MKT" not in betas.columns:
            return
        for i, g in betas.drop_nulls("beta_MKT").sort("week").group_by("id"):
            k = i[0] if isinstance(i, tuple) else i
            self.d[k] = (
                g["week"].cast(pl.Int32).to_numpy(),
                g["beta_MKT"].to_numpy().astype(float),
            )

    def at(self, token: str, d: date) -> float | None:
        s = self.d.get(token)
        if s is None:
            return None
        j = np.searchsorted(s[0], (d - date(1970, 1, 1)).days, side="right") - 1
        return float(s[1][j]) if j >= 0 else None


def clustered_se(values: np.ndarray, clusters: np.ndarray) -> float | None:
    """Standard error of the mean with clusters (cliff weeks): sqrt(Σ_g (Σ_{i∈g} e_i)²)/n with
    e_i the deviation from the mean. Equals the i.i.d. formula when every cluster has one
    member."""
    v = np.asarray(values, dtype=float)
    ok = np.isfinite(v)
    v, c = v[ok], np.asarray(clusters)[ok]
    n = v.size
    if n < 2:
        return None
    e = v - v.mean()
    sums: dict = {}
    for ei, ci in zip(e, c, strict=True):
        sums[ci] = sums.get(ci, 0.0) + ei
    return float(np.sqrt(sum(x * x for x in sums.values())) / n)


PLACEBO_SCHEMA = {
    "base": pl.Utf8,
    "date": pl.Date,
    "week": pl.Date,
    "ret_pre_14d": pl.Float64,
    "ret_pre_14d_vs_btc": pl.Float64,
    "beta_mkt": pl.Float64,
    "ret_pre_14d_beta_adj": pl.Float64,
    "ret_post_14d": pl.Float64,
    "hit": pl.Boolean,
}


def placebo_table(
    events: pl.DataFrame,
    venue_prices: pl.DataFrame,
    k: int = 20,
    seed: int = 20260908,
    betas: pl.DataFrame | None = None,
) -> pl.DataFrame:
    """For every token and year with at least one cliff, `k` pseudo-cliff dates drawn uniformly
    from that token's non-cliff days with price coverage in the same year; the same pre/post
    windows and hit definition. One row per pseudo event, with `week` for clustering."""
    if not events.height:
        return pl.DataFrame(schema=PLACEBO_SCHEMA)
    vp = venue_prices.with_columns(
        pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias(
            "volume_quote"
        )
    )
    px = _Px(
        vp.group_by("date", "base").agg(
            pl.col("close").median().alias("close"),
            pl.col("volume_quote").sum().alias("volume_quote"),
        )
    )
    rng = np.random.default_rng(seed)
    cliff_days = {(r["base"], r["date"]) for r in events.select("base", "date").to_dicts()}
    id_of = {r["base"]: r["id"] for r in events.select("base", "id").unique().to_dicts()}
    beta_of = _BetaLookup(betas)
    rows = []
    keys = (
        events.with_columns(pl.col("date").dt.year().alias("y"))
        .select("base", "y")
        .unique()
        .sort("base", "y")
    )
    for r in keys.to_dicts():
        base, year = r["base"], r["y"]
        s_ = px.d.get(base)
        if s_ is None:
            continue
        days = [date(1970, 1, 1) + timedelta(days=int(x)) for x in s_[0]]
        cand = [
            d
            for d in days
            if d.year == year
            and (base, d) not in cliff_days
            and d - timedelta(days=PRE_DAYS) >= days[0]
            and d + timedelta(days=POST_DAYS) <= days[-1]
        ]
        if not cand:
            continue
        for j in rng.choice(len(cand), size=min(k, len(cand)), replace=False):
            d = cand[int(j)]
            p_pre, p0 = px.close_at(base, d - timedelta(days=PRE_DAYS)), px.close_at(base, d)
            p_post = px.close_at(base, d + timedelta(days=POST_DAYS))
            b_pre, b0 = px.close_at("BTC", d - timedelta(days=PRE_DAYS)), px.close_at("BTC", d)
            if not (p_pre and p0):
                continue
            pre = float(np.log(p0 / p_pre))
            btc_pre = float(np.log(b0 / b_pre)) if (b_pre and b0) else None
            beta = beta_of.at(id_of.get(base, ""), d)
            rows.append(
                {
                    "base": base,
                    "date": d,
                    "week": d - timedelta(days=d.weekday()),
                    "ret_pre_14d": pre,
                    "ret_pre_14d_vs_btc": pre - btc_pre if btc_pre is not None else None,
                    "beta_mkt": beta,
                    "ret_pre_14d_beta_adj": pre - beta * btc_pre
                    if (beta is not None and btc_pre is not None)
                    else None,
                    "ret_post_14d": float(np.log(p_post / p0)) if p_post else None,
                    "hit": pre < 0,
                }
            )
    return (
        pl.DataFrame(rows, schema=PLACEBO_SCHEMA) if rows else pl.DataFrame(schema=PLACEBO_SCHEMA)
    )


SUMMARY_SCHEMA = {
    "group_kind": pl.Utf8,
    "group": pl.Utf8,
    "n": pl.Int64,
    "hits": pl.Int64,
    "hit_rate": pl.Float64,
    "se": pl.Float64,
    "se_cluster": pl.Float64,
    "n_clusters": pl.Int64,
    "se_pre_cluster": pl.Float64,
    "mean_pre_beta_adj": pl.Float64,
    "n_beta": pl.Int64,
    "mean_pre": pl.Float64,
    "median_pre": pl.Float64,
    "mean_pre_vs_btc": pl.Float64,
    "mean_post": pl.Float64,
    "median_post": pl.Float64,
    "n_post": pl.Int64,
}


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
    weeks = g["week"].to_numpy() if "week" in g.columns else np.arange(n)
    badj = (
        g["ret_pre_14d_beta_adj"].drop_nulls()
        if "ret_pre_14d_beta_adj" in g.columns
        else pl.Series([], dtype=pl.Float64)
    )
    return {
        "group_kind": kind,
        "group": name,
        "n": n,
        "hits": hits,
        "hit_rate": hr,
        "se": float(np.sqrt(hr * (1 - hr) / n)) if n and hr is not None else None,
        "se_cluster": clustered_se(g["hit"].cast(pl.Float64).to_numpy(), weeks) if n else None,
        "n_clusters": len(set(weeks.tolist())) if n else 0,
        "se_pre_cluster": clustered_se(g["ret_pre_14d"].to_numpy(), weeks) if n else None,
        "mean_pre_beta_adj": float(badj.mean()) if badj.len() else None,
        "n_beta": int(badj.len()),
        "mean_pre": float(g["ret_pre_14d"].mean()) if n else None,
        "median_pre": float(g["ret_pre_14d"].median()) if n else None,
        "mean_pre_vs_btc": float(g["ret_pre_14d_vs_btc"].drop_nulls().mean())
        if g["ret_pre_14d_vs_btc"].drop_nulls().len()
        else None,
        "mean_post": float(post.mean()) if post.len() else None,
        "median_post": float(post.median()) if post.len() else None,
        "n_post": int(post.len()),
    }


def summarise(
    events: pl.DataFrame, cliff_th: dict, placebo: pl.DataFrame | None = None
) -> pl.DataFrame:
    """Hit rate and pre/post returns: by year first (the pooled number is carried by one year),
    then all cliffs, cliffs meeting the (frozen) Rule 5.1 thresholds, placebo rows, and
    breakdowns by dominant recipient class, float-share bucket and days-of-volume bucket."""
    rows = []
    if not events.height:
        return pl.DataFrame(schema=SUMMARY_SCHEMA)
    for y in sorted(events["date"].dt.year().unique().to_list()):
        rows.append(_summ(events.filter(pl.col("date").dt.year() == y), "year", str(y)))
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
    if placebo is not None and placebo.height:
        for y in sorted(placebo["date"].dt.year().unique().to_list()):
            rows.append(
                _summ(placebo.filter(pl.col("date").dt.year() == y), "placebo year", str(y))
            )
        rows.append(
            _summ(placebo, "placebo", "pseudo-cliffs: 20 non-cliff days per token and year")
        )
        sub = placebo.filter(pl.col("base").is_in(rule["base"].unique().to_list()))
        if sub.height:
            rows.append(_summ(sub, "placebo", "pseudo-cliffs on the Rule 5.1 tokens only"))
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
    return pl.DataFrame(rows, schema=SUMMARY_SCHEMA)
