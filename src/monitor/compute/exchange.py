"""Exchange-token sub-module (work order 7).

The single-factor design is right on average and wrong for a sector with equity-like cash
flows. This module computes, for the exchange-token sector only, a fundamental layer (fees,
revenue, burns, price-to-fees within the peer set), a within-sector relative-value test, and
a solvency-sensor read from the token itself. It changes nothing in the market-state layer,
Phi, or any threshold.

Two honesty rails run through everything:
* `revenue_quality`: `verified` for on-chain protocols (DefiLlama fees, exact) and `estimated`
  for CEX (wash-filtered volume x a blended fee tier, wide error bar). An estimated figure
  never drives a published trade.
* nothing is published from the relative-value model until its within-sector residual IC
  clears the walk-forward bar; until then the panel and the RV structure are descriptive.

Designed so the same machinery extends to any fee-generating protocol: the registry
(config/exchange_tokens.yaml) carries a `subtype`, and `exchange-cex` / `exchange-onchain` are
the two cases, not hard-coded exchange names.
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import polars as pl

from monitor.paths import CONFIG

SECTOR = "exchange-token"


# --------------------------------------------------------------------------- config
def registry() -> dict:
    import yaml

    return yaml.safe_load((CONFIG / "exchange_tokens.yaml").read_text())


def fee_cfg() -> dict:
    import yaml

    return yaml.safe_load((CONFIG / "exchange_fees.yaml").read_text())["fees"]


def tokens() -> dict[str, dict]:
    """id -> {symbol, subtype, venue?, defillama?} for every registered exchange token."""
    return registry()["tokens"]


def universe_members(uni: pl.DataFrame) -> pl.DataFrame:
    """The exchange-token rows of the latest universe, joined to the registry (subtype, venue)."""
    reg = tokens()
    u = uni.filter((pl.col("as_of") == uni["as_of"].max()) & (pl.col("sector") == SECTOR))
    rows = []
    for r in u.to_dicts():
        meta = reg.get(r["id"], {})
        rows.append(
            {
                "id": r["id"],
                "symbol": r["symbol"],
                "tier": r["tier"],
                "market_cap_usd": r.get("market_cap_usd"),
                "subtype": meta.get("subtype", "exchange-cex"),
                "venue": meta.get("venue"),
                "defillama": meta.get("defillama"),
            }
        )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


# --------------------------------------------------------------------------- on-chain fees
def onchain_token_fees(exchange_fees: pl.DataFrame) -> pl.DataFrame:
    """Map the DefiLlama parent-slug fees to token ids (verified). date, id, symbol,
    fees_usd, revenue_usd, holders_revenue_usd, revenue_quality=verified."""
    reg = tokens()
    slug_to = {m["defillama"]: (tid, m["symbol"]) for tid, m in reg.items() if m.get("defillama")}
    if exchange_fees is None or not exchange_fees.height:
        return pl.DataFrame()
    rows = []
    for r in exchange_fees.to_dicts():
        tid_sym = slug_to.get(r["slug"])
        if not tid_sym:
            continue
        tid, sym = tid_sym
        rows.append(
            {
                "date": r["date"],
                "id": tid,
                "symbol": sym,
                "fees_usd": r["fees_usd"],
                "revenue_usd": r["revenue_usd"],
                "holders_revenue_usd": r["holders_revenue_usd"],
                "revenue_quality": "verified",
            }
        )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


# --------------------------------------------------------------------------- CEX volume + revenue
def venue_spot_volume_wf(prices_daily: pl.DataFrame, wash: pl.DataFrame | None) -> pl.DataFrame:
    """date, venue, spot_vol_usd — daily spot quote volume summed over the venue's pairs that
    pass the wash filters (the filters finally pay off here). Unfiltered pairs are dropped, so
    the total is conservative (verified-clean volume only)."""
    p = prices_daily.with_columns(
        pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias("vq")
    )
    if wash is not None and wash.height:
        # the latest wash verdict per (base, venue): the filters are computed intermittently, so
        # a same-date row often does not exist. A pair that passed most recently is treated as
        # clean; a pair with a failing latest verdict, or never evaluated, is excluded.
        verdict = (
            wash.sort("date").group_by("base", "venue").agg(pl.col("pass").last().alias("pass"))
        )
        p = p.join(verdict, on=["base", "venue"], how="left").filter(pl.col("pass"))
    return (
        p.group_by("date", "venue")
        .agg(pl.col("vq").sum().alias("spot_vol_usd"))
        .sort("date", "venue")
    )


def venue_deriv_volume(perp: pl.DataFrame) -> pl.DataFrame:
    """date, venue, deriv_vol_usd — the last hourly snapshot of each day summed over the
    venue's perps (`volume_24h_usd`, exchange-reported; not wash-filtered, flagged downstream)."""
    if perp is None or not perp.height or "volume_24h_usd" not in perp.columns:
        return pl.DataFrame(schema={"date": pl.Date, "venue": pl.Utf8, "deriv_vol_usd": pl.Float64})
    p = perp.with_columns(pl.col("ts").dt.date().alias("date"))
    last = p.group_by("date", "venue", "symbol").agg(
        pl.col("volume_24h_usd").filter(pl.col("ts") == pl.col("ts").max()).first().alias("v")
    )
    return (
        last.group_by("date", "venue")
        .agg(pl.col("v").sum().alias("deriv_vol_usd"))
        .sort("date", "venue")
    )


def volume_share(spot: pl.DataFrame, deriv: pl.DataFrame, trend_days: int = 30) -> pl.DataFrame:
    """Per venue per day: spot and derivative wash-filtered volume as a share of the
    tracked-venue total, and the change in total share over `trend_days`."""
    s = spot.join(deriv, on=["date", "venue"], how="full", coalesce=True).fill_null(0.0)
    tot = s.group_by("date").agg(
        pl.col("spot_vol_usd").sum().alias("t_spot"), pl.col("deriv_vol_usd").sum().alias("t_deriv")
    )
    s = s.join(tot, on="date", how="left").with_columns(
        pl.when(pl.col("t_spot") > 0)
        .then(pl.col("spot_vol_usd") / pl.col("t_spot"))
        .otherwise(None)
        .alias("spot_share"),
        pl.when(pl.col("t_deriv") > 0)
        .then(pl.col("deriv_vol_usd") / pl.col("t_deriv"))
        .otherwise(None)
        .alias("deriv_share"),
    )
    s = s.with_columns(
        (
            (pl.col("spot_vol_usd") + pl.col("deriv_vol_usd"))
            / (pl.col("t_spot") + pl.col("t_deriv"))
        ).alias("total_share")
    )
    out = []
    for _venue, g in s.sort("date").group_by("venue"):
        g = g.sort("date")
        ts = g["total_share"].to_numpy()
        trend = [None] * g.height
        for i in range(g.height):
            j = i - trend_days
            if j >= 0 and ts[j] is not None and np.isfinite(ts[j]):
                trend[i] = float(ts[i] - ts[j])
        out.append(g.with_columns(pl.Series("share_trend", trend, dtype=pl.Float64)))
    return (
        pl.concat(out, how="vertical") if out else s.with_columns(pl.lit(None).alias("share_trend"))
    )


def cex_token_revenue(
    members: pl.DataFrame, spot: pl.DataFrame, deriv: pl.DataFrame
) -> pl.DataFrame:
    """Per CEX token whose venue the system collects volume for: daily estimated revenue and a
    wide error bar (work order 7, §2b). date, id, symbol, venue, est_revenue_usd, rev_lo,
    rev_hi, revenue_quality=estimated. Tokens whose venue is untracked get no rows (n/a)."""
    cfg = fee_cfg()
    cex = members.filter((pl.col("subtype") == "exchange-cex") & pl.col("venue").is_in(list(cfg)))
    if not cex.height:
        return pl.DataFrame()
    vol = spot.join(deriv, on=["date", "venue"], how="full", coalesce=True).fill_null(0.0)
    rows = []
    for m in cex.to_dicts():
        f = cfg[m["venue"]]
        v = vol.filter(pl.col("venue") == m["venue"]).sort("date")
        for r in v.to_dicts():
            sv, dv = r["spot_vol_usd"], r["deriv_vol_usd"]
            rows.append(
                {
                    "date": r["date"],
                    "id": m["id"],
                    "symbol": m["symbol"],
                    "venue": m["venue"],
                    "spot_vol_usd": sv,
                    "deriv_vol_usd": dv,
                    "est_revenue_usd": sv * f["spot"] + dv * f["deriv"],
                    "rev_lo": sv * f["spot_lo"] + dv * f["deriv_lo"],
                    "rev_hi": sv * f["spot_hi"] + dv * f["deriv_hi"],
                    "revenue_quality": "estimated",
                }
            )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


# --------------------------------------------------------------------------- burns
def burn_events(prices_daily: pl.DataFrame) -> pl.DataFrame:
    """Per token: published token retirements (config, EXACT for CEX like BNB) plus the
    on-chain buyback implied by holders-revenue (tokens = holders_revenue_usd / price). date,
    id, symbol, tokens, usd, kind, revenue_quality. The shareholder-yield numerator (§2c)."""
    reg = registry()
    close = _daily_close(prices_daily)
    rows = []
    for tid, b in (reg.get("burns") or {}).items():
        sym = reg["tokens"].get(tid, {}).get("symbol", tid.upper())
        for e in b.get("events", []):
            d = e["date"] if isinstance(e["date"], date) else date.fromisoformat(str(e["date"]))
            px = close.get((sym, d)) or close.get((sym, d - timedelta(days=1)))
            rows.append(
                {
                    "date": d,
                    "id": tid,
                    "symbol": sym,
                    "tokens": float(e["tokens"]),
                    "usd": float(e["tokens"]) * px if px else None,
                    "kind": b.get("kind", "burn"),
                    "revenue_quality": "verified",
                    "source": b.get("source", "config"),
                }
            )
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "date": pl.Date,
                "id": pl.Utf8,
                "symbol": pl.Utf8,
                "tokens": pl.Float64,
                "usd": pl.Float64,
                "kind": pl.Utf8,
                "revenue_quality": pl.Utf8,
                "source": pl.Utf8,
            }
        )
    )


def _daily_close(prices_daily: pl.DataFrame) -> dict:
    if prices_daily is None or not prices_daily.height:
        return {}
    c = prices_daily.group_by("date", "base").agg(pl.col("close").median().alias("c")).to_dicts()
    return {(r["base"], r["date"]): r["c"] for r in c}


# --------------------------------------------------------------------------- fundamentals (panel 8)
def _annualise(series: pl.DataFrame, col: str, as_of: date, days: int = 30) -> float | None:
    """Trailing-`days` sum of `col`, annualised (× 365/days). None if too few days."""
    w = series.filter((pl.col("date") > as_of - timedelta(days=days)) & (pl.col("date") <= as_of))
    v = w[col].drop_nulls()
    if v.len() < days // 2:
        return None
    return float(v.sum()) * 365.0 / days


def _growth(series: pl.DataFrame, col: str, as_of: date, days: int) -> float | None:
    """Log growth of the trailing-`days` mean of `col` vs the prior `days` window."""

    def mean(a, b):
        w = series.filter((pl.col("date") > a) & (pl.col("date") <= b))[col].drop_nulls()
        return float(w.mean()) if w.len() >= days // 2 else None

    now = mean(as_of - timedelta(days=days), as_of)
    prev = mean(as_of - timedelta(days=2 * days), as_of - timedelta(days=days))
    if now is None or prev is None or prev <= 0 or now <= 0:
        return None
    return float(np.log(now / prev))


def _price_solvency(symbol: str, prices_daily: pl.DataFrame, as_of: date, window: int = 90) -> dict:
    """The token's own price signal: position in its 90-day range and a robust z of it."""
    from monitor.compute.positioning import robust_z

    p = (
        prices_daily.filter((pl.col("base") == symbol) & (pl.col("date") <= as_of))
        .group_by("date")
        .agg(pl.col("close").median().alias("c"))
        .sort("date")
    )
    if p.height < 20:
        return {"price_vs_90d_range": None, "price_z": None}
    c = p["c"].to_numpy().astype(float)
    win = c[-window:]
    lo, hi = float(win.min()), float(win.max())
    pos = (float(c[-1]) - lo) / (hi - lo) if hi > lo else None
    z, _ = robust_z(c, window)
    return {"price_vs_90d_range": pos, "price_z": z}


def fundamentals(
    now_date: date,
    uni: pl.DataFrame,
    markets: pl.DataFrame,
    exchange_fees: pl.DataFrame,
    prices_daily: pl.DataFrame,
    wash: pl.DataFrame | None,
    perp: pl.DataFrame,
    funding_daily: pl.DataFrame | None,
) -> pl.DataFrame:
    """The panel-8 snapshot, one row per exchange-token member of the universe (§3). Descriptive
    (no trade) until the within-sector residual IC clears the walk-forward bar."""
    import yaml

    mem = universe_members(uni)
    if not mem.height:
        return pl.DataFrame()
    otf = onchain_token_fees(exchange_fees)  # date,id,fees_usd,revenue_usd,holders_revenue_usd
    spot = venue_spot_volume_wf(prices_daily, wash)
    deriv = venue_deriv_volume(perp)
    vs = volume_share(spot, deriv)
    cex = cex_token_revenue(mem, spot, deriv)  # date,id,est_revenue_usd,...
    burns = burn_events(prices_daily)
    venues = yaml.safe_load((CONFIG / "venues.yaml").read_text())["venues"]
    mk = markets.filter(pl.col("as_of") == markets["as_of"].max()).unique(
        subset=["id"], keep="last"
    )
    mk_by = {r["id"]: r for r in mk.to_dicts()}
    fd_z = {}
    if funding_daily is not None and funding_daily.height:
        f = funding_daily.filter(pl.col("date") > now_date - timedelta(days=120))
        from monitor.compute.positioning import robust_z

        for base, g in f.group_by("base"):
            b = base[0] if isinstance(base, tuple) else base
            z, _ = robust_z(g.sort("date")["funding_ann"].to_numpy().astype(float), 90)
            fd_z[b] = z

    rows = []
    for m in mem.to_dicts():
        tid, sym, subtype, venue = m["id"], m["symbol"], m["subtype"], m["venue"]
        mkr = mk_by.get(tid, {})
        mcap, fdv, supply = (
            mkr.get("market_cap_usd"),
            mkr.get("fdv_usd"),
            mkr.get("circulating_supply"),
        )
        # fees series (verified on-chain) or estimated CEX revenue
        if subtype == "exchange-onchain":
            fees_s = otf.filter(pl.col("id") == tid).select("date", pl.col("fees_usd").alias("f"))
            rev_col = otf.filter(pl.col("id") == tid).select(
                "date", pl.col("holders_revenue_usd").alias("h")
            )
            quality = "verified"
        else:
            fees_s = (
                cex.filter(pl.col("id") == tid).select("date", pl.col("est_revenue_usd").alias("f"))
                if cex.height
                else pl.DataFrame(schema={"date": pl.Date, "f": pl.Float64})
            )
            rev_col = fees_s.rename({"f": "h"})
            quality = "estimated" if fees_s.height else "na"
        ann_fees = _annualise(fees_s, "f", now_date) if fees_s.height else None
        holders_ttm = (
            float(
                rev_col.filter(pl.col("date") > now_date - timedelta(days=365))["h"]
                .drop_nulls()
                .sum()
            )
            if rev_col.height
            else None
        )
        pf = (fdv / ann_fees) if (fdv and ann_fees and ann_fees > 0) else None
        # burn / distribution yield (trailing 4 quarters ~ 365 d)
        bt = burns.filter(
            (pl.col("id") == tid)
            & (pl.col("date") > now_date - timedelta(days=365))
            & (pl.col("date") <= now_date)
        )
        burn_tokens_ttm = float(bt["tokens"].sum()) if bt.height else None
        burn_yield = None
        if burn_tokens_ttm and supply:
            burn_yield = burn_tokens_ttm / supply
        elif holders_ttm and mcap:  # on-chain buyback as the shareholder-yield proxy
            burn_yield = holders_ttm / mcap
        # volume share (cex with tracked venue)
        share = share_trend = None
        if venue:
            vr = (
                vs.filter(
                    (pl.col("venue") == venue)
                    & (pl.col("date") == vs.filter(pl.col("venue") == venue)["date"].max())
                )
                if vs.filter(pl.col("venue") == venue).height
                else pl.DataFrame()
            )
            if vr.height:
                share, share_trend = vr["total_share"][0], vr["share_trend"][0]
        sol = _price_solvency(sym, prices_daily, now_date)
        v = venues.get(venue, {}) if venue else {}
        rows.append(
            {
                "as_of": now_date,
                "id": tid,
                "symbol": sym,
                "tier": m["tier"],
                "subtype": subtype,
                "venue": venue,
                "market_cap_usd": mcap,
                "fdv_usd": fdv,
                "annual_fees_usd": ann_fees,
                "pf_ratio": pf,
                "fee_growth_30d": _growth(fees_s, "f", now_date, 30) if fees_s.height else None,
                "fee_growth_90d": _growth(fees_s, "f", now_date, 90) if fees_s.height else None,
                "burn_yield": burn_yield,
                "burn_tokens_ttm": burn_tokens_ttm,
                "holders_rev_ttm_usd": holders_ttm,
                "volume_share": share,
                "volume_share_trend_30d": share_trend,
                "price_vs_90d_range": sol["price_vs_90d_range"],
                "price_z": sol["price_z"],
                "funding_z": fd_z.get(sym),
                "por_quality": v.get("proof_of_reserves"),
                "licensing": v.get("licensing"),
                "revenue_quality": quality,
                "descriptive": True,  # no trade until the residual IC clears (§4a)
            }
        )
    df = pl.DataFrame(rows)
    # price-to-fees RANK within the peer set (§3), not an absolute
    df = df.with_columns(pl.col("pf_ratio").rank().over("subtype").alias("pf_rank"))
    return df


# --------------------------------------------------------------------------- weekly returns
def _weekly_returns(prices_daily: pl.DataFrame, symbols: list[str]) -> pl.DataFrame:
    """Weekly log returns per symbol on a COMMON Monday grid, so cross-sections align across
    names. week (the Monday), symbol, ret = log(close_week / close_prev_week)."""
    p = (
        prices_daily.filter(pl.col("base").is_in(symbols))
        .group_by("date", "base")
        .agg(pl.col("close").median().alias("c"))
        .with_columns(
            (pl.col("date") - pl.duration(days=pl.col("date").dt.weekday() - 1)).alias("week")
        )
    )
    wk = (
        p.sort("date")
        .group_by("week", "base")
        .agg(pl.col("c").last().alias("c"))
        .sort("base", "week")
    )
    rows = []
    for base, g in wk.group_by("base"):
        b = base[0] if isinstance(base, tuple) else base
        g = g.sort("week")
        wks = g["week"].to_list()
        c = g["c"].to_numpy().astype(float)
        for k in range(1, len(wks)):
            if c[k - 1] > 0 and c[k] > 0 and (wks[k] - wks[k - 1]).days <= 14:
                rows.append({"week": wks[k], "symbol": b, "ret": float(np.log(c[k] / c[k - 1]))})
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(schema={"week": pl.Date, "symbol": pl.Utf8, "ret": pl.Float64})
    )


def residual_ic(exchange_fees: pl.DataFrame, prices_daily: pl.DataFrame) -> dict:
    """Within-sector relative-value test (§4a). On the on-chain sub-sector (verified fees), the
    predictor is each token's trailing 30-day fee growth as of week t; the outcome is its
    next-week return in excess of the equal-weight sector return that week (this within-sector
    demeaning removes the common market and sector component, the residual §4 asks for). The
    weekly cross-sectional Spearman IC is averaged with a Newey-West standard error. Reported,
    never assumed: with a handful of names the sector is too small to establish a signal, and
    the decision to publish or stay descriptive is stated from the numbers."""
    otf = onchain_token_fees(exchange_fees)
    if otf is None or not otf.height:
        return {
            "n_names": 0,
            "n_weeks": 0,
            "ic": None,
            "ic_se": None,
            "published": False,
            "reason": "no on-chain fee history",
        }
    syms = otf["symbol"].unique().to_list()
    rets = _weekly_returns(prices_daily, syms)
    names_priced = rets["symbol"].n_unique() if rets.height else 0
    if names_priced < 3:
        return {
            "n_names": len(syms),
            "n_names_priced": names_priced,
            "n_weeks": 0,
            "ic": None,
            "ic_se": None,
            "published": False,
            "reason": f"{len(syms)} on-chain names have verified fees but only {names_priced} have price history in the tracked universe (DYDX and GMX are not collected); a within-sector cross-section needs at least three names, so no IC is estimable and the panel stays descriptive",
        }
    # predictor: trailing-30d fee growth per (symbol, week)
    fees = otf.select("date", "symbol", "fees_usd").sort("date")
    weeks = sorted(rets["week"].unique().to_list())
    pred_rows = []
    for sym in syms:
        fs = fees.filter(pl.col("symbol") == sym)
        for w in weeks:
            g = _growth(fs.rename({"fees_usd": "f"}), "f", w, 30)
            if g is not None:
                pred_rows.append({"week": w, "symbol": sym, "pred": g})
    pred = (
        pl.DataFrame(pred_rows)
        if pred_rows
        else pl.DataFrame(schema={"week": pl.Date, "symbol": pl.Utf8, "pred": pl.Float64})
    )
    if not pred.height:
        return {
            "n_names": len(syms),
            "n_weeks": 0,
            "ic": None,
            "ic_se": None,
            "published": False,
            "reason": "no fee-growth predictor",
        }
    # residual = ret - sector-mean ret, per week
    sector_mean = rets.group_by("week").agg(pl.col("ret").mean().alias("mkt"))
    res = rets.join(sector_mean, on="week").with_columns(
        (pl.col("ret") - pl.col("mkt")).alias("resid")
    )
    ics = []
    wk = sorted(set(pred["week"].to_list()))
    from scipy.stats import spearmanr

    for i in range(len(wk) - 1):
        w0, w1 = wk[i], wk[i + 1]
        p0 = pred.filter(pl.col("week") == w0).select("symbol", "pred")
        r1 = res.filter(pl.col("week") == w1).select("symbol", pl.col("resid"))
        j = p0.join(r1, on="symbol", how="inner")
        if j.height >= 3:
            ic, _ = spearmanr(j["pred"].to_numpy(), j["resid"].to_numpy())
            if np.isfinite(ic):
                ics.append(ic)
    n_weeks = len(ics)
    if n_weeks < 8:
        return {
            "n_names": len(syms),
            "n_names_priced": names_priced,
            "n_weeks": n_weeks,
            "ic": float(np.mean(ics)) if ics else None,
            "ic_se": None,
            "published": False,
            "reason": f"only {names_priced} priced on-chain names and {n_weeks} weekly cross-sections; too small to establish a within-sector signal",
        }
    a = np.array(ics)
    ic = float(a.mean())
    lag = max(1, int(np.floor(4 * (n_weeks / 100) ** (2 / 9))))
    var = a.var(ddof=1)
    for k in range(1, lag + 1):
        cov = np.cov(a[:-k], a[k:])[0, 1] if n_weeks - k > 1 else 0.0
        var += 2 * (1 - k / (lag + 1)) * cov
    se = float(np.sqrt(max(var, 0) / n_weeks))
    published = abs(ic / se) > 2 and len(syms) >= 8 if se else False
    return {
        "n_names": len(syms),
        "n_names_priced": names_priced,
        "n_weeks": n_weeks,
        "ic": ic,
        "ic_se": se,
        "published": published,
        "reason": "IC/SE clears 2 and the cross-section is wide enough"
        if published
        else "held descriptive: the IC does not clear the walk-forward bar on a sector this small",
    }


def dispersion(prices_daily: pl.DataFrame, uni: pl.DataFrame) -> dict:
    """Dispersion evidence (§7): the exchange-token sector's average pairwise weekly-return
    correlation and its correlation to BTC, against the all-universe figure. Shown, not
    assumed."""
    mem = universe_members(uni)
    syms = mem["symbol"].to_list()
    all_syms = (
        uni.filter(pl.col("as_of") == uni["as_of"].max())
        .filter(pl.col("tier").is_not_null())["symbol"]
        .to_list()
    )

    def avg_pairwise(symbols):
        r = _weekly_returns(prices_daily, symbols)
        if not r.height:
            return None, 0
        wide = r.pivot(values="ret", index="week", on="symbol")
        cols = [c for c in wide.columns if c != "week"]
        m = wide.select(cols).to_numpy()
        cors = []
        for a in range(len(cols)):
            for b in range(a + 1, len(cols)):
                x, y = m[:, a], m[:, b]
                ok = np.isfinite(x) & np.isfinite(y)
                if ok.sum() >= 12:
                    c = np.corrcoef(x[ok], y[ok])[0, 1]
                    if np.isfinite(c):
                        cors.append(c)
        return (float(np.mean(cors)) if cors else None), len(cors)

    sec_corr, n_sec = avg_pairwise(syms)
    all_corr, _n_all = avg_pairwise(all_syms[:60])
    # sector-mean return vs BTC
    r = _weekly_returns(prices_daily, [*syms, "BTC"])
    btc_corr = None
    if r.height:
        wide = r.pivot(values="ret", index="week", on="symbol")
        if "BTC" in wide.columns:
            sec_cols = [c for c in wide.columns if c not in ("week", "BTC")]
            if sec_cols:
                secm = wide.select(sec_cols).to_numpy()
                secm = np.nanmean(secm, axis=1)
                btc = wide["BTC"].to_numpy()
                ok = np.isfinite(secm) & np.isfinite(btc)
                if ok.sum() >= 12:
                    btc_corr = float(np.corrcoef(secm[ok], btc[ok])[0, 1])
    events = [
        {"date": "2022-11-08", "event": "FTX / FTT collapse"},
        {"date": "2023-11-21", "event": "Binance DOJ settlement, CZ steps down"},
        {"date": "2022-10-08", "event": "HTX (Huobi) stablecoin depeg scare"},
    ]
    return {
        "sector_avg_pairwise_corr": sec_corr,
        "sector_n_pairs": n_sec,
        "all_universe_avg_pairwise_corr": all_corr,
        "sector_return_corr_to_btc": btc_corr,
        "n_names": len(syms),
        "event_dates": events,
        "note": "exchange tokens with a long enough weekly history are few; the correlation is computed on the names that have it.",
    }


# --------------------------------------------------------------------------- solvency sensor (§5)
def solvency_signals(
    now_date: date,
    uni: pl.DataFrame,
    prices_daily: pl.DataFrame,
    funding_daily: pl.DataFrame | None,
    threshold: float,
) -> list[dict]:
    """Per venue whose token is in the universe (config/venues.yaml `token`): the token-implied
    distress read for the venue panel (§5). token price vs 90-day range and z, perp funding z,
    5-day return vs the exchange-token sector, and a divergence flag when the token
    underperforms the sector by more than `threshold` over 5 days (the FTX-before-the-halt
    input). Basis and on-chain net flows are not available for these tokens and are null."""
    import yaml

    venues = yaml.safe_load((CONFIG / "venues.yaml").read_text())["venues"]
    mem = universe_members(uni)
    sym_by_id = {m["id"]: m["symbol"] for m in mem.to_dicts()}
    # 5-day sector return (equal weight)
    sec_syms = mem["symbol"].to_list()

    def ret5(sym):
        p = (
            prices_daily.filter((pl.col("base") == sym) & (pl.col("date") <= now_date))
            .group_by("date")
            .agg(pl.col("close").median().alias("c"))
            .sort("date")
        )
        if p.height < 6:
            return None
        c = p["c"].to_numpy().astype(float)
        return float(np.log(c[-1] / c[-6])) if c[-6] > 0 else None

    sec_r5 = [x for x in (ret5(s) for s in sec_syms) if x is not None]
    sector_5d = float(np.median(sec_r5)) if sec_r5 else None
    fd_z = {}
    if funding_daily is not None and funding_daily.height:
        from monitor.compute.positioning import robust_z

        f = funding_daily.filter(pl.col("date") > now_date - timedelta(days=120))
        for base, g in f.group_by("base"):
            b = base[0] if isinstance(base, tuple) else base
            z, _ = robust_z(g.sort("date")["funding_ann"].to_numpy().astype(float), 90)
            fd_z[b] = z
    out = []
    for vname, v in venues.items():
        tid = v.get("token")
        if not tid or tid not in sym_by_id:
            continue
        sym = sym_by_id[tid]
        sol = _price_solvency(sym, prices_daily, now_date)
        r5 = ret5(sym)
        vs_sector = (r5 - sector_5d) if (r5 is not None and sector_5d is not None) else None
        diverges = bool(vs_sector is not None and vs_sector < -abs(threshold))
        out.append(
            {
                "venue": vname,
                "token": sym,
                "price_vs_90d_range": sol["price_vs_90d_range"],
                "price_z": sol["price_z"],
                "funding_z": fd_z.get(sym),
                "basis": None,
                "onchain_netflow": None,
                "ret_5d": r5,
                "sector_ret_5d": sector_5d,
                "vs_sector_5d": vs_sector,
                "por_quality": v.get("proof_of_reserves"),
                "divergence": diverges,
            }
        )
    return out
