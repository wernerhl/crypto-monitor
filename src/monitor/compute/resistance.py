"""Work order 8 — resistance/support-test conditional probability model.

The framing is fixed and not negotiable in code: Phi is not a direction forecaster. This module
estimates two SEPARATE objects and multiplies them (§3):

  (1) P(direction | state at a test)  — a multinomial break / reject / chop, and
  (2) E(move | direction, state)      — the size of each move.

The published expected move is (1)x(2). It is honest even when (1) is a coin flip, because the
asymmetry lives in (2). Everything here is descriptive measurement: it publishes panel 9 and a
methods note, and adds no trigger, changes no threshold, and does not touch Phi's definition.

The two-sample split (§7) is the crux and is enforced here: the base-rate model (7a) is fit on
FULL price history with price/structure + market context only; the crowding overlay (7b) adds the
short-history leverage/Phi regressors and is always labelled preliminary until a cell clears the
frozen minimum event count (§6). Nothing crowding-conditioned is presented as established below
that floor.

All event and state construction is causal: reference levels and state are taken as of t-1, and no
smoothed or look-ahead value is used anywhere (a hard "Do not" of the order).
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from monitor.paths import CONFIG

SIDE_RESISTANCE = "resistance"
SIDE_SUPPORT = "support"
SIDES = (SIDE_RESISTANCE, SIDE_SUPPORT)
R_DEFS = ("hi", "swing", "vp")
LABELS = ("break", "reject", "chop")

# regressors for the base-rate model (7a): price/structure + market context ONLY (§7a). Phi and the
# leverage state are deliberately excluded here — they belong to the overlay (7b) and the §4 answer.
BASE_REGRESSORS = ("dist", "level_age_log", "range_width", "rv", "close_cleared", "btc_ret20", "btc_dd90")
# added by the overlay (7b), each on its own shorter sample:
OVERLAY_REGRESSORS = ("oi_pctile", "funding_z", "oi_change_5d", "lambda_over_depth", "phi")


def cfg() -> dict:
    import yaml

    return yaml.safe_load((CONFIG / "resistance_model.yaml").read_text())


# --------------------------------------------------------------------------- price series

def canonical_ohlc(prices: pl.DataFrame, assets: list[str], venue_priority: list[str]) -> pl.DataFrame:
    """One OHLCV row per (base, date), choosing the first venue present per day by priority so we
    never double-count bars across venues. Returns base, date, open, high, low, close, volume."""
    if prices is None or not prices.height:
        return pl.DataFrame()
    pr = prices.filter(pl.col("base").is_in(assets)).select(
        "base", "date", "venue", "open", "high", "low", "close", "volume_quote"
    )
    rank = {v: i for i, v in enumerate(venue_priority)}
    pr = pr.with_columns(
        pl.col("venue").replace_strict(rank, default=len(rank)).alias("_vr")
    )
    pr = (
        pr.sort(["base", "date", "_vr"])
        .unique(subset=["base", "date"], keep="first")
        .drop("_vr", "venue")
        .rename({"volume_quote": "volume"})
        .sort(["base", "date"])
    )
    return pr


# --------------------------------------------------------------------------- causal levels

def _swing_levels(high: np.ndarray, low: np.ndarray, w: int, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """Most recent CONFIRMED swing high / swing low as of t-1 (causal). A swing high at bar j is a
    strict local max over [j-w, j+w] and is only knowable at j+w, so for day t we use the latest j
    with j+w <= t-1. Returns arrays r_swing_hi[t], r_swing_lo[t] (nan until one exists)."""
    n = len(high)
    is_hi = np.zeros(n, dtype=bool)
    is_lo = np.zeros(n, dtype=bool)
    for j in range(w, n - w):
        seg_h = high[j - w : j + w + 1]
        seg_l = low[j - w : j + w + 1]
        if high[j] == seg_h.max() and (seg_h == high[j]).sum() == 1:
            is_hi[j] = True
        if low[j] == seg_l.min() and (seg_l == low[j]).sum() == 1:
            is_lo[j] = True
    r_hi = np.full(n, np.nan)
    r_lo = np.full(n, np.nan)
    last_hi = np.nan
    last_lo = np.nan
    last_hi_age = 10**9
    last_lo_age = 10**9
    for t in range(n):
        # a swing at bar j is confirmed at j+w; as of t-1 it is usable when j+w <= t-1
        conf = t - 1 - w  # the newest bar that could be a confirmed swing centre by t-1
        if conf >= 0 and is_hi[conf]:
            last_hi = high[conf]
            last_hi_age = 0
        if conf >= 0 and is_lo[conf]:
            last_lo = low[conf]
            last_lo_age = 0
        if last_hi_age <= lookback:
            r_hi[t] = last_hi
        if last_lo_age <= lookback:
            r_lo[t] = last_lo
        last_hi_age += 1
        last_lo_age += 1
    return r_hi, r_lo


def _vp_levels(
    high: np.ndarray, low: np.ndarray, close: np.ndarray, vol: np.ndarray,
    window: int, bins: int, min_share: float,
) -> tuple[np.ndarray, np.ndarray]:
    """Nearest high-volume node above / below price from a causal 90-day volume-by-price profile
    (the 'shelf' a technician calls resistance). Uses bars [t-window, t-1] only. Returns
    r_vp_above[t], r_vp_below[t] (nan when no qualifying node on that side)."""
    n = len(close)
    above = np.full(n, np.nan)
    below = np.full(n, np.nan)
    for t in range(window, n):
        lo = low[t - window : t]
        hi = high[t - window : t]
        vv = vol[t - window : t]
        pmin, pmax = float(np.nanmin(lo)), float(np.nanmax(hi))
        if not np.isfinite(pmin) or not np.isfinite(pmax) or pmax <= pmin:
            continue
        edges = np.linspace(pmin, pmax, bins + 1)
        centres = 0.5 * (edges[:-1] + edges[1:])
        typ = (hi + lo + close[t - window : t]) / 3.0  # each bar's volume dropped at its typical price
        idx = np.clip(np.digitize(typ, edges) - 1, 0, bins - 1)
        prof = np.zeros(bins)
        vsum = np.nansum(vv)
        if vsum <= 0:
            continue
        for b, vq in zip(idx, vv):
            if np.isfinite(vq):
                prof[b] += vq
        share = prof / vsum
        node = share >= min_share
        p = close[t - 1]
        up = [centres[b] for b in range(bins) if node[b] and centres[b] > p]
        dn = [centres[b] for b in range(bins) if node[b] and centres[b] < p]
        if up:
            above[t] = min(up)
        if dn:
            below[t] = max(dn)
    return above, below


def _rolling_max_shift(a: np.ndarray, window: int) -> np.ndarray:
    """max over [t-window, t-1] (causal, excludes t)."""
    n = len(a)
    out = np.full(n, np.nan)
    for t in range(1, n):
        s = max(0, t - window)
        if t - s >= 1:
            out[t] = np.nanmax(a[s:t])
    return out


def _rolling_min_shift(a: np.ndarray, window: int) -> np.ndarray:
    n = len(a)
    out = np.full(n, np.nan)
    for t in range(1, n):
        s = max(0, t - window)
        if t - s >= 1:
            out[t] = np.nanmin(a[s:t])
    return out


# --------------------------------------------------------------------------- events + labels

def _label_side(
    side: str, t: int, R: float, c: np.ndarray, low: np.ndarray, high: np.ndarray,
    pre_ref: float, m: float, hold_h: int, react_h: int,
) -> tuple[str | None, bool]:
    """Return (label, resolved) for one test at index t over a react window of react_h days.
    resistance: break = pop above R*(1+m) that holds hold_h closes above R*(1-m); reject = close
    back below R*(1-m) AND a lower low than the pre-test swing. support is the mirror."""
    n = len(c)
    end = t + react_h
    if end + hold_h >= n:
        return None, False  # not enough forward bars to resolve honestly
    up = side == SIDE_RESISTANCE
    for d in range(t, end + 1):
        if up:
            broke = c[d] > R * (1 + m)
            if broke:
                hold = np.min(c[d : d + hold_h]) > R * (1 - m)
                if hold:
                    return "break", True
            rejected = c[d] < R * (1 - m) and np.min(low[t : d + 1]) < pre_ref
            if rejected:
                return "reject", True
        else:
            broke = c[d] < R * (1 - m)
            if broke:
                hold = np.max(c[d : d + hold_h]) < R * (1 + m)
                if hold:
                    return "break", True
            rejected = c[d] > R * (1 + m) and np.max(high[t : d + 1]) > pre_ref
            if rejected:
                return "reject", True
    return "chop", True


def _magnitude(side: str, direction: str, t: int, H: int, c: np.ndarray, low: np.ndarray, high: np.ndarray) -> float | None:
    """§3b realised magnitude for the resolved direction. reject -> max adverse excursion from P_t
    over H days (drawdown for resistance, run-up for support); break -> continuation return."""
    n = len(c)
    if t + H >= n:
        return None
    p0 = c[t]
    seg_lo = low[t + 1 : t + H + 1]
    seg_hi = high[t + 1 : t + H + 1]
    if side == SIDE_RESISTANCE:
        if direction == "reject":
            return float(np.min(seg_lo) / p0 - 1.0)  # negative drawdown
        if direction == "break":
            return float(c[t + H] / p0 - 1.0)
    else:
        if direction == "reject":
            return float(np.max(seg_hi) / p0 - 1.0)  # positive bounce
        if direction == "break":
            return float(c[t + H] / p0 - 1.0)  # negative continuation (breakdown)
    return None


def build_events(prices: pl.DataFrame, c: dict) -> pl.DataFrame:
    """Detect every resistance and support test for every asset and every R definition over full
    history, label it at each horizon, and record the causal state. Returns one row per
    (base, side, r_def, date) test. Horizon-specific labels/magnitudes are label_5/label_20 and
    mag_5/mag_20."""
    ev = c["event"]
    horizons = ev["horizons_days"]
    m = ev["labels"]["break_margin_m"]
    hold_h = ev["labels"]["hold_h"]
    band = ev["proximity_band"]
    k = ev["approach_k"]
    ret5_gt = ev["trend"]["ret5_gt"]
    ret3_gt = ev["trend"]["ret3_gt"]
    rl = c["reference_levels"]
    ohlc = canonical_ohlc(prices, c["assets"], c["venue_priority"])
    if not ohlc.height:
        return pl.DataFrame()

    # BTC market-context series (as of t-1), joined to alts by date
    btc = ohlc.filter(pl.col("base") == "BTC").sort("date")
    btc_ctx = {}
    if btc.height:
        bc = btc["close"].to_numpy()
        bdates = btc["date"].to_list()
        bhi90 = _rolling_max_shift(bc, 90)
        for i, d in enumerate(bdates):
            r20 = bc[i - 1] / bc[i - 21] - 1.0 if i >= 21 else None
            dd = (bc[i - 1] / bhi90[i] - 1.0) if (i >= 1 and np.isfinite(bhi90[i])) else None
            btc_ctx[d] = (r20, dd)

    rows: list[dict] = []
    for base, g in ohlc.group_by("base", maintain_order=True):
        base = base[0] if isinstance(base, tuple) else base
        g = g.sort("date")
        n = g.height
        if n < 200:
            continue
        dates = g["date"].to_list()
        _o, h, low, cl, v = (g[x].to_numpy() for x in ("open", "high", "low", "close", "volume"))
        logret = np.diff(np.log(np.clip(cl, 1e-12, None)), prepend=np.log(cl[0]))
        rv = np.full(n, np.nan)
        for t in range(20, n):
            rv[t] = float(np.std(logret[t - 20 : t]) * np.sqrt(365))
        r_hi = _rolling_max_shift(h, rl["hi"]["window_days"])
        r_lo90 = _rolling_min_shift(low, rl["hi"]["window_days"])
        r_sw_hi, r_sw_lo = _swing_levels(h, low, rl["swing"]["fractal_w"], rl["swing"]["lookback_days"])
        r_vp_up, r_vp_dn = _vp_levels(
            h, low, cl, v, rl["vp"]["window_days"], rl["vp"]["bins"], rl["vp"]["min_node_share"]
        )
        levels = {
            SIDE_RESISTANCE: {"hi": r_hi, "swing": r_sw_hi, "vp": r_vp_up},
            SIDE_SUPPORT: {"hi": r_lo90, "swing": r_sw_lo, "vp": r_vp_dn},
        }
        for side in SIDES:
            up = side == SIDE_RESISTANCE
            for r_def in R_DEFS:
                R = levels[side][r_def]
                last_trigger = -(10**9)
                for t in range(60, n):
                    Rt = R[t]
                    if not np.isfinite(Rt) or Rt <= 0:
                        continue
                    if t < 6:
                        continue
                    ret5 = cl[t] / cl[t - 5] - 1.0
                    ret3 = cl[t] / cl[t - 3] - 1.0
                    if up:
                        if not (ret5 > ret5_gt and ret3 > ret3_gt):
                            continue
                        approach = all(cl[t - j] < Rt for j in range(1, k + 1))
                    else:
                        if not (ret5 < -ret5_gt and ret3 < -ret3_gt):
                            continue
                        approach = all(cl[t - j] > Rt for j in range(1, k + 1))
                    if not approach:
                        continue
                    if abs(cl[t] / Rt - 1.0) > band:
                        continue
                    if t - last_trigger < max(horizons):  # one event per resolution window
                        continue
                    last_trigger = t
                    # causal structure state (as of t-1)
                    level_age = 0
                    for a in range(t - 1, max(0, t - 1 - 400), -1):
                        if up and h[a] >= Rt:
                            break
                        if not up and low[a] <= Rt:
                            break
                        level_age += 1
                    rng = (r_hi[t] - r_lo90[t]) / cl[t - 1] if (np.isfinite(r_hi[t]) and np.isfinite(r_lo90[t])) else None
                    r20, dd = btc_ctx.get(dates[t], (None, None))
                    pre_ref = float(np.min(low[t - 5 : t])) if up else float(np.max(h[t - 5 : t]))
                    close_cleared = (cl[t] > Rt) if up else (cl[t] < Rt)
                    row = {
                        "base": base, "date": dates[t], "side": side, "r_def": r_def,
                        "R": float(Rt), "dist": float(cl[t - 1] / Rt - 1.0),
                        "level_age": int(level_age), "level_age_log": float(np.log1p(level_age)),
                        "range_width": float(rng) if rng is not None else None,
                        "rv": float(rv[t]) if np.isfinite(rv[t]) else None,
                        "close_cleared": int(close_cleared),
                        "btc_ret20": float(r20) if r20 is not None else None,
                        "btc_dd90": float(dd) if dd is not None else None,
                    }
                    for H in horizons:
                        lab, resolved = _label_side(side, t, Rt, cl, low, h, pre_ref, m, hold_h, H)
                        row[f"label_{H}"] = lab
                        row[f"resolved_{H}"] = resolved
                        row[f"mag_{H}"] = _magnitude(side, lab, t, H, cl, low, h) if (resolved and lab in ("break", "reject")) else None
                    rows.append(row)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).sort(["side", "r_def", "date", "base"])


# --------------------------------------------------------------------------- overlay state (7b)

def attach_state(
    events: pl.DataFrame,
    positioning_history: pl.DataFrame | None,
    fragility_series: pl.DataFrame | None,
    positioning_live: pl.DataFrame | None,
) -> pl.DataFrame:
    """Left-join the leverage/Phi state (as of t-1) onto the events for the crowding overlay (7b).
    Each source carries its own history depth, so every overlay regressor keeps its own sample:
    funding_z / oi_change_5d from positioning_history (2020-11 on), market phi from fragility_series
    (2017-12 on), and oi_pctile / lambda_over_depth from the live positioning snapshot (collector
    era only — the genuinely short Lambda/D sample §7 warns about). Missing values stay null; the
    overlay reports n per regressor and never imputes."""
    if not events.height:
        return events
    ev = events.with_columns((pl.col("date") - pl.duration(days=1)).alias("_d1"))
    for col in OVERLAY_REGRESSORS:
        if col not in ev.columns:
            ev = ev.with_columns(pl.lit(None, dtype=pl.Float64).alias(col))
    if positioning_history is not None and positioning_history.height:
        ph = positioning_history.select(
            pl.col("base"), pl.col("date").alias("_d1"),
            pl.col("z_fr").alias("_funding_z"), pl.col("oi_change_5d").alias("_oi_change_5d"),
        ).unique(subset=["base", "_d1"], keep="last")
        ev = ev.join(ph, on=["base", "_d1"], how="left").with_columns(
            pl.coalesce("_funding_z", "funding_z").alias("funding_z"),
            pl.coalesce("_oi_change_5d", "oi_change_5d").alias("oi_change_5d"),
        ).drop("_funding_z", "_oi_change_5d")
    if fragility_series is not None and fragility_series.height:
        fs = fragility_series.select(
            pl.col("date").alias("_d1"), pl.col("phi").alias("_phi")
        ).unique(subset=["_d1"], keep="last")
        ev = ev.join(fs, on="_d1", how="left").with_columns(
            pl.coalesce("_phi", "phi").alias("phi")
        ).drop("_phi")
    if positioning_live is not None and positioning_live.height and "ts" in positioning_live.columns:
        pl_live = positioning_live.with_columns(pl.col("ts").dt.date().alias("_d1")).select(
            pl.col("base"), pl.col("_d1"),
            pl.col("oi_rel_pctile").alias("_oi_pctile"),
            pl.col("lambda_over_depth").alias("_lod"),
        ).unique(subset=["base", "_d1"], keep="last")
        ev = ev.join(pl_live, on=["base", "_d1"], how="left").with_columns(
            pl.coalesce("_oi_pctile", "oi_pctile").alias("oi_pctile"),
            pl.coalesce("_lod", "lambda_over_depth").alias("lambda_over_depth"),
        ).drop("_oi_pctile", "_lod")
    return ev.drop("_d1")


# --------------------------------------------------------------------------- fitting (7a / 7b)

def _iso_week(d: date) -> int:
    iso = d.isocalendar()
    return iso[0] * 100 + iso[1]


def _design(events: pl.DataFrame, regressors: tuple[str, ...]) -> tuple[np.ndarray, list[str], np.ndarray]:
    """Standardised design matrix (mean 0, sd 1 per column) for the given regressors, dropping rows
    with any null. Returns X (with intercept col), kept-row mask indices, and the standardisation is
    internal (coefficients are reported as marginal effects, not raw betas)."""
    cols = [r for r in regressors if r in events.columns]
    sub = events.select(cols)
    mask = np.ones(events.height, dtype=bool)
    mats = []
    for cname in cols:
        x = sub[cname].to_numpy().astype(float)
        mask &= np.isfinite(x)
        mats.append(x)
    return np.column_stack(mats) if mats else np.empty((events.height, 0)), cols, mask


def fit_direction(events: pl.DataFrame, side: str, r_def: str, H: int, regressors: tuple[str, ...], c: dict) -> dict:
    """Multinomial break/reject/chop for one (side, r_def, horizon). Walk-forward out-of-sample
    calibration on an expanding window (refit every refit_cadence_days), plus a full-sample fit for
    week-clustered coefficient standard errors. Returns a descriptive dict; publishes nothing on its
    own. `regressors` = BASE_REGRESSORS for 7a, BASE+OVERLAY for 7b."""
    import warnings

    import statsmodels.api as sm

    s = c["samples"]
    lab = f"label_{H}"
    df = events.filter(
        (pl.col("side") == side) & (pl.col("r_def") == r_def)
        & pl.col(f"resolved_{H}") & pl.col(lab).is_not_null()
    ).sort("date")
    n_all = df.height
    out: dict = {
        "side": side, "r_def": r_def, "horizon": H, "n_events": n_all,
        "base_rate": {}, "published": False, "reason": "", "regressors": [r for r in regressors],
    }
    if n_all == 0:
        out["reason"] = "no resolved events"
        return out
    y_all = df[lab].to_list()
    for L in LABELS:
        out["base_rate"][L] = round(sum(1 for z in y_all if z == L) / n_all, 4)
    weeks = np.array([_iso_week(d) for d in df["date"].to_list()])
    out["n_weeks"] = len(set(weeks.tolist()))

    X, cols, mask = _design(df, regressors)
    if X.shape[1] == 0 or mask.sum() < s["min_train_events"]:
        out["reason"] = f"insufficient sample (n={int(mask.sum())})"
        return out
    # standardise (events are already date-sorted, so index order == time order)
    Xm = X[mask]
    mu, sd = Xm.mean(0), Xm.std(0)
    sd[sd == 0] = 1.0
    Xz = np.clip((Xm - mu) / sd, -8.0, 8.0)  # clip guards exp overflow in MNLogit
    y = np.array([y_all[i] for i in range(n_all) if mask[i]])
    wk = weeks[mask]
    dts = [df["date"].to_list()[i] for i in range(n_all) if mask[i]]

    # --- walk-forward OOS calibration: expanding window, refit every refit_cadence_days ---
    reliab_pred, reliab_hit = [], []
    oos_prob_true = []  # predicted prob of the realised class, out of sample
    model = None
    cls_order: list[str] = []
    last_fit_date = None
    for pos in range(len(y)):
        if pos >= s["min_train_events"]:
            need_fit = model is None or last_fit_date is None or (
                (dts[pos] - last_fit_date).days >= s["refit_cadence_days"]
            )
            if need_fit:
                ytr = y[:pos]
                if len(set(ytr.tolist())) >= 2:
                    try:
                        cats = pl.Series(ytr).cast(pl.Categorical)
                        with warnings.catch_warnings():
                            warnings.simplefilter("ignore")
                            model = sm.MNLogit(cats.to_physical().to_numpy(),
                                               sm.add_constant(Xz[:pos], has_constant="add")).fit(
                                disp=0, method="lbfgs", maxiter=2000)
                        cls_order = list(cats.cat.get_categories())
                        last_fit_date = dts[pos]
                    except Exception:
                        model = None
            if model is not None and cls_order:
                try:
                    with warnings.catch_warnings():
                        warnings.simplefilter("ignore")
                        p = np.asarray(model.predict(sm.add_constant(Xz[pos:pos + 1], has_constant="add")))[0]
                    if not np.all(np.isfinite(p)):
                        continue
                    pmap = {cls_order[j]: float(p[j]) for j in range(len(cls_order))}
                    oos_prob_true.append(pmap.get(y[pos], np.nan))
                    reliab_pred.append(pmap.get("break", 0.0))
                    reliab_hit.append(1.0 if y[pos] == "break" else 0.0)
                except Exception:
                    pass
    out["calibration"] = _reliability(reliab_pred, reliab_hit, s["calibration_bins"])
    out["n_oos"] = int(np.isfinite(np.array(oos_prob_true)).sum()) if oos_prob_true else 0

    # --- full-sample fit with week-clustered SE for reported marginal effects ---
    try:
        import warnings

        Xfull = sm.add_constant(Xz, has_constant="add")
        ycat = pl.Series(y).cast(pl.Categorical)
        mod = sm.MNLogit(ycat.to_physical().to_numpy(), Xfull)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            res = mod.fit(disp=0, method="lbfgs", maxiter=2000,
                          cov_type="cluster", cov_kwds={"groups": wk})
        conv = bool(getattr(res, "mle_retvals", {}).get("converged", True))
        cats = list(ycat.cat.get_categories())
        # coefficient of each regressor on P(reject) relative to baseline, with clustered SE
        eff = {}
        if "reject" in cats:
            jr = cats.index("reject") - 1  # MNLogit drops baseline (first category)
            if jr >= 0:
                params = np.asarray(res.params)[:, jr]
                bse = np.asarray(res.bse)[:, jr]
                for ci, cname in enumerate(["const", *cols]):
                    eff[cname] = {"beta": float(params[ci]), "se": float(bse[ci]),
                                  "z": float(params[ci] / bse[ci]) if bse[ci] else None}
        out["reject_effects"] = eff
        out["converged"] = conv
    except Exception as e:
        out["converged"] = False
        out["fit_error"] = str(e)[:120]

    published = (
        out.get("converged", False)
        and n_all >= s["min_events_publish"]
        and out["n_weeks"] >= s["min_events_publish"]
    )
    out["published"] = bool(published)
    if not published and not out["reason"]:
        out["reason"] = f"n={n_all}, weeks={out['n_weeks']} — below publish floor {s['min_events_publish']}"
    return out


def _reliability(pred: list[float], hit: list[float], bins: int) -> list[dict]:
    if not pred:
        return []
    p = np.array(pred)
    y = np.array(hit)
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for b in range(bins):
        sel = (p >= edges[b]) & (p < edges[b + 1] if b < bins - 1 else p <= edges[b + 1])
        if sel.sum() >= 5:
            out.append({"bin": round((edges[b] + edges[b + 1]) / 2, 3),
                        "predicted": round(float(p[sel].mean()), 4),
                        "observed": round(float(y[sel].mean()), 4), "n": int(sel.sum())})
    return out


def magnitude_summary(events: pl.DataFrame, side: str, r_def: str, H: int) -> dict:
    """§3b object (2): E(move | direction) with a Newey-West standard error on the mean, per
    realised direction. Descriptive; the leverage-driver regression is added by the overlay."""
    import statsmodels.api as sm

    df = events.filter(
        (pl.col("side") == side) & (pl.col("r_def") == r_def)
        & pl.col(f"resolved_{H}") & pl.col(f"mag_{H}").is_not_null()
    )
    out: dict = {"side": side, "r_def": r_def, "horizon": H, "by_direction": {}}
    for direction in ("break", "reject"):
        d = df.filter(pl.col(f"label_{H}") == direction)
        x = d[f"mag_{H}"].to_numpy()
        x = x[np.isfinite(x)]
        if len(x) < 5:
            out["by_direction"][direction] = {"n": len(x), "mean": None, "se": None}
            continue
        try:
            res = sm.OLS(x, np.ones(len(x))).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
            mean, se = float(res.params[0]), float(res.bse[0])
        except Exception:
            mean, se = float(x.mean()), float(x.std() / np.sqrt(len(x)))
        out["by_direction"][direction] = {"n": len(x), "mean": round(mean, 4), "se": round(se, 4)}
    return out


def model_cells(events: pl.DataFrame, c: dict) -> list[dict]:
    """One descriptive summary per (side, r_def, horizon): the 7a base-rate multinomial (published
    when it clears the floor), its out-of-sample calibration, the 3b magnitude means, the 7b Phi
    overlay headline (Phi's coefficient on P(reject), preliminary), and the Lambda/D rejection-
    magnitude claim (§4). Everything carries its n; nothing crowding-conditioned is presented as
    established below the §6 floor."""
    cells = []
    for side in SIDES:
        for r_def in R_DEFS:
            for H in c["event"]["horizons_days"]:
                base = fit_direction(events, side, r_def, H, BASE_REGRESSORS, c)
                base["magnitude"] = magnitude_summary(events, side, r_def, H)["by_direction"]
                # 7b overlay for the Phi-on-reject headline, on the sample Phi+funding support
                ov = fit_direction(events, side, r_def, H, (*BASE_REGRESSORS, "phi", "funding_z"), c)
                base["overlay"] = {
                    "regressors": ov.get("regressors"),
                    "n_events": ov.get("n_events"),
                    "converged": ov.get("converged"),
                    "preliminary": True,
                    "phi_on_reject": ov.get("reject_effects", {}).get("phi"),
                    "funding_z_on_reject": ov.get("reject_effects", {}).get("funding_z"),
                }
                base["lambda_over_depth_magnitude"] = overlay_magnitude_reject(events, side, r_def, H, c)
                cells.append(base)
    return cells


def _base_model(events: pl.DataFrame, side: str, r_def: str, H: int, c: dict):
    """Fit the base-rate multinomial on resolved events and return (model, cls_order, cols, mu, sd)
    for scoring live tests. None when the cell cannot be fit."""
    import warnings

    import statsmodels.api as sm

    df = events.filter(
        (pl.col("side") == side) & (pl.col("r_def") == r_def)
        & pl.col(f"resolved_{H}") & pl.col(f"label_{H}").is_not_null()
    )
    X, cols, mask = _design(df, BASE_REGRESSORS)
    if X.shape[1] == 0 or mask.sum() < c["samples"]["min_train_events"]:
        return None
    Xm = X[mask]
    mu, sd = Xm.mean(0), Xm.std(0)
    sd[sd == 0] = 1.0
    y = np.array([df[f"label_{H}"].to_list()[i] for i in range(df.height) if mask[i]])
    if len(set(y.tolist())) < 2:
        return None
    cats = pl.Series(y).cast(pl.Categorical)
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            model = sm.MNLogit(cats.to_physical().to_numpy(),
                               sm.add_constant(np.clip((Xm - mu) / sd, -8.0, 8.0), has_constant="add")).fit(
                disp=0, method="lbfgs", maxiter=2000)
    except Exception:
        return None
    return model, list(cats.cat.get_categories()), cols, mu, sd


def _predict_row(model_pack, row: dict, force_close_cleared: int | None = None) -> dict:
    import warnings

    import statsmodels.api as sm

    model, cls_order, cols, mu, sd = model_pack
    x = []
    for j, cname in enumerate(cols):
        v = row.get(cname)
        if cname == "close_cleared" and force_close_cleared is not None:
            v = force_close_cleared
        if v is None or (isinstance(v, float) and not np.isfinite(v)):
            return {}
        x.append(float(np.clip((float(v) - mu[j]) / sd[j], -8.0, 8.0)))  # clip guards exp overflow
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        p = np.asarray(model.predict(sm.add_constant(np.array([x]), has_constant="add")))[0]
    if not np.all(np.isfinite(p)):
        return {}
    return {cls_order[k]: float(p[k]) for k in range(len(cls_order))}


def active_tests(events: pl.DataFrame, as_of: date, c: dict) -> pl.DataFrame:
    """Tests currently open: the trigger fired on the most recent bars and the outcome has not yet
    resolved (§5, the panel shows only when an asset is in a test). Scored live with the base-rate
    model for both the shorter and longer horizon, 'now' and 'if the daily close clears the level'."""
    if not events.height:
        return pl.DataFrame()
    horizons = c["event"]["horizons_days"]
    recent = events.filter(
        (pl.col("date") >= pl.lit(as_of) - pl.duration(days=int(max(horizons))))
        & ~pl.col(f"resolved_{min(horizons)}")
    )
    if not recent.height:
        return pl.DataFrame()
    packs = {}
    rows = []
    for r in recent.to_dicts():
        side, rd = r["side"], r["r_def"]
        rec = {k: r[k] for k in ("base", "date", "side", "r_def", "R", "dist", "level_age",
                                 "range_width", "rv", "close_cleared", "btc_ret20", "btc_dd90")}
        rec["probs"] = {}
        for H in horizons:
            key = (side, rd, H)
            if key not in packs:
                packs[key] = _base_model(events, side, rd, H, c)
            pack = packs[key]
            if pack is None:
                continue
            now = _predict_row(pack, r)
            clr = _predict_row(pack, r, force_close_cleared=1)
            mag = magnitude_summary(events, side, rd, H)["by_direction"]
            def _exp(pp, mag=mag):
                if not pp:
                    return None
                em = 0.0
                for d in ("break", "reject"):
                    md = mag.get(d, {}).get("mean")
                    if md is not None:
                        em += pp.get(d, 0.0) * md
                return round(em, 4)
            rec["probs"][str(H)] = {
                "now": {k: round(v, 4) for k, v in now.items()},
                "if_clear": {k: round(v, 4) for k, v in clr.items()},
                "exp_move_now": _exp(now), "exp_move_if_clear": _exp(clr),
                "mag": {d: mag.get(d, {}).get("mean") for d in ("break", "reject")},
            }
        rows.append(rec)
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def scorecard(events: pl.DataFrame, as_of: date, calibrated_on: date, c: dict) -> pl.DataFrame:
    """Running calibration scorecard (§5, §8): every test that RESOLVED on or after go-live, with the
    base-rate model's predicted class probabilities and the realised outcome, so live calibration
    accumulates in public. Uses the shorter horizon."""
    if not events.height:
        return pl.DataFrame()
    H = min(c["event"]["horizons_days"])
    df = events.filter(pl.col(f"resolved_{H}") & pl.col(f"label_{H}").is_not_null())
    # resolution date ~ test date + H; keep those resolved since go-live and already in the past
    df = df.with_columns((pl.col("date") + pl.duration(days=H)).alias("resolve_date"))
    df = df.filter((pl.col("resolve_date") >= pl.lit(calibrated_on)) & (pl.col("resolve_date") <= pl.lit(as_of)))
    if not df.height:
        return pl.DataFrame()
    packs = {}
    rows = []
    for r in df.to_dicts():
        key = (r["side"], r["r_def"], H)
        if key not in packs:
            packs[key] = _base_model(events, r["side"], r["r_def"], H, c)
        pred = _predict_row(packs[key], r) if packs[key] else {}
        rows.append({
            "base": r["base"], "date": r["date"], "resolve_date": r["resolve_date"],
            "side": r["side"], "r_def": r["r_def"], "horizon": H,
            "predicted": {k: round(v, 4) for k, v in pred.items()},
            "p_break": round(pred.get("break", float("nan")), 4) if pred else None,
            "realized": r[f"label_{H}"],
        })
    return pl.DataFrame(rows).sort(["resolve_date", "base"])


def overlay_magnitude_reject(events: pl.DataFrame, side: str, r_def: str, H: int, c: dict) -> dict:
    """§3b / §4 headline (2): does Lambda-/D (liquidation mass over depth) and OI percentile predict
    a LARGER rejection drawdown — the leverage-bites-the-loser hypothesis. Newey-West errors. Runs
    on the short leverage sample and is labelled preliminary with its n."""
    import statsmodels.api as sm

    d = events.filter(
        (pl.col("side") == side) & (pl.col("r_def") == r_def)
        & (pl.col(f"label_{H}") == "reject") & pl.col(f"mag_{H}").is_not_null()
        & pl.col("lambda_over_depth").is_not_null() & pl.col("oi_pctile").is_not_null()
    )
    n = d.height
    out = {"side": side, "r_def": r_def, "horizon": H, "n": n, "preliminary": True, "drivers": {}}
    if n < 8:
        out["reason"] = f"insufficient sample (n={n})"
        return out
    y = d[f"mag_{H}"].to_numpy()
    for drv in ("lambda_over_depth", "oi_pctile"):
        x = d[drv].to_numpy().astype(float)
        xz = (x - x.mean()) / (x.std() or 1.0)
        try:
            res = sm.OLS(y, sm.add_constant(xz)).fit(cov_type="HAC", cov_kwds={"maxlags": 5})
            out["drivers"][drv] = {"beta": round(float(res.params[1]), 5), "se": round(float(res.bse[1]), 5),
                                   "z": round(float(res.params[1] / res.bse[1]), 2) if res.bse[1] else None}
        except Exception as e:
            out["drivers"][drv] = {"error": str(e)[:80]}
    out["published"] = n >= c["samples"]["min_events_publish"]
    return out
