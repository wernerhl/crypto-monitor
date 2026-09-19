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


def _time_to_events(
    side: str, t: int, R: float, c: np.ndarray, low: np.ndarray, high: np.ndarray,
    pre_ref: float, m: float, hold_h: int, W: int,
) -> tuple[str, int, bool]:
    """Competing-risks outcome for one test (work order 9, §2.1): the first day a break confirms and
    the first day a rejection confirms, whichever comes first, with end-of-window and end-of-data as
    censoring. Returns (cause, time_to_resolution_days, censored). 'chop' is no longer an outcome —
    an unresolved test is censored, not counted as a resolution."""
    n = len(c)
    up = side == SIDE_RESISTANCE
    forward = n - 1 - t
    bd = rd = None
    for d in range(t, min(t + W, n - 1) + 1):
        if up:
            if bd is None and c[d] > R * (1 + m) and d + hold_h <= n and np.min(c[d : d + hold_h]) > R * (1 - m):
                bd = d
            if rd is None and c[d] < R * (1 - m) and np.min(low[t : d + 1]) < pre_ref:
                rd = d
        else:
            if bd is None and c[d] < R * (1 - m) and d + hold_h <= n and np.max(c[d : d + hold_h]) < R * (1 + m):
                bd = d
            if rd is None and c[d] > R * (1 + m) and np.max(high[t : d + 1]) > pre_ref:
                rd = d
        if bd is not None and rd is not None:
            break
    if bd is not None and (rd is None or bd <= rd):
        return "break", bd - t, False
    if rd is not None:
        return "reject", rd - t, False
    return "censored", min(W, max(forward, 0)), True


def build_events(prices: pl.DataFrame, c: dict) -> pl.DataFrame:
    """Detect every resistance and support test for every asset and every R definition over full
    history, label it at each horizon, and record the causal state. Returns one row per
    (base, side, r_def, date) test. Horizon-specific labels/magnitudes are label_5/label_20 and
    mag_5/mag_20."""
    ev = c["event"]
    horizons = ev["horizons_days"]
    W = c["competing_risks"]["max_window_days"]
    cif_horizons = c["competing_risks"]["cif_horizons"]
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
                    # work order 9 §2.1: competing-risks time-to-event over the max window
                    cause, ttr, cens = _time_to_events(side, t, Rt, cl, low, h, pre_ref, m, hold_h, W)
                    row["cause"], row["ttr"], row["cens"] = cause, int(ttr), bool(cens)
                    # magnitude at each CIF horizon, for the resolved cause (drawdown/continuation)
                    for H in cif_horizons:
                        row[f"mag_cif_{H}"] = (
                            _magnitude(side, cause, t, H, cl, low, h) if cause in ("break", "reject") else None
                        )
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


# =========================================================================================
# Work order 9 — competing risks, partial pooling, walk-forward recalibration, bootstrap bands.
# The published quantity becomes cause-specific cumulative incidence: P(an upward resolution
# before a downward one, by horizon h). "chop" is censoring, not an outcome. Every published
# number is recalibrated walk-forward and carries a block-bootstrap band. No new indicator.
# =========================================================================================

CIF_FEATURES = ("dist", "level_age_log", "range_width", "rv", "close_cleared", "btc_ret20", "btc_dd90")
CIF_CELLS = (*R_DEFS, "consensus")


def with_week(events: pl.DataFrame) -> pl.DataFrame:
    if not events.height or "week" in events.columns:
        return events
    return events.with_columns(
        pl.col("date").map_elements(_iso_week, return_dtype=pl.Int64).alias("week")
    )


def consensus_events(events: pl.DataFrame, c: dict) -> pl.DataFrame:
    """§2.2 — a test is 'confirmed' when >= min_defs of {hi, swing, vp} fire on the same day. The
    consensus row carries the majority cause (earliest resolution breaks ties), the time-to-event of
    that cause, and the mean of the (near-identical) constituent states. r_def = 'consensus'."""
    min_defs = c["consensus"]["min_defs"]
    if not events.height:
        return events
    rows = []
    num_cols = [x for x in (*CIF_FEATURES, "R", "level_age") if x in events.columns]
    for (base, dt, side), g in events.group_by(["base", "date", "side"], maintain_order=True):
        # group key order follows the by-list (base, date, side)
        if g["r_def"].n_unique() < min_defs:
            continue
        gd = g.to_dicts()
        causes = [r["cause"] for r in gd]
        # majority cause; tie -> the cause with the earliest resolution
        from collections import Counter

        cnt = Counter(causes)
        top = max(cnt.values())
        winners = [ca for ca, n in cnt.items() if n == top]
        if len(winners) > 1:
            resolved = [r for r in gd if r["cause"] in winners and not r["cens"]]
            cause = min(resolved, key=lambda r: r["ttr"])["cause"] if resolved else "censored"
        else:
            cause = winners[0]
        same = [r for r in gd if r["cause"] == cause]
        cens = cause == "censored"
        ttr = (min(r["ttr"] for r in same) if not cens else max(r["ttr"] for r in gd))
        row = {"base": base, "date": dt, "side": side, "r_def": "consensus",
               "cause": cause, "ttr": int(ttr), "cens": bool(cens)}
        for col in num_cols:
            vals = [r[col] for r in gd if r[col] is not None]
            row[col] = float(np.mean(vals)) if vals else None
        for H in c["competing_risks"]["cif_horizons"]:
            v = [r.get(f"mag_cif_{H}") for r in gd if r.get(f"mag_cif_{H}") is not None]
            row[f"mag_cif_{H}"] = float(np.mean(v)) if v else None
        rows.append(row)
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def cif_empirical(df: pl.DataFrame, c: dict) -> dict:
    """Aalen–Johansen cause-specific cumulative incidence (discrete time) for a set of tests:
    P(break before reject by h) and P(reject before break by h) at each CIF horizon, plus the
    median time-to-resolution per cause. Model-free; this is the base-rate anchor."""
    horizons = c["competing_risks"]["cif_horizons"]
    W = c["competing_risks"]["max_window_days"]
    out = {"n": df.height, "break": {}, "reject": {}, "median_ttr": {}}
    if not df.height:
        return out
    ttr = df["ttr"].to_numpy()
    cause = np.array(df["cause"].to_list())
    S = 1.0
    cb = dict.fromkeys(horizons, 0.0)
    cr = dict.fromkeys(horizons, 0.0)
    for d in range(0, W + 1):
        at_risk = int((ttr >= d).sum())
        if at_risk == 0:
            break
        nb = int(((ttr == d) & (cause == "break")).sum())
        nr = int(((ttr == d) & (cause == "reject")).sum())
        hb, hr = nb / at_risk, nr / at_risk
        for h in horizons:
            if d <= h:
                cb[h] += hb * S
                cr[h] += hr * S
        S *= max(1.0 - hb - hr, 0.0)
    out["break"] = {h: round(cb[h], 4) for h in horizons}
    out["reject"] = {h: round(cr[h], 4) for h in horizons}
    for ca in ("break", "reject"):
        tt = ttr[(cause == ca)]
        out["median_ttr"][ca] = int(np.median(tt)) if len(tt) else None
    out["n_resolved"] = int((cause != "censored").sum())
    return out


def _cif_bootstrap(df: pl.DataFrame, c: dict) -> dict:
    """Block bootstrap over calendar weeks (§1.3): resample weeks with replacement, recompute the
    empirical CIF, and return the 16th–84th percentile band per horizon for each cause."""
    B = c["bootstrap"]["n"]
    lo, hi = c["bootstrap"]["lo_pct"], c["bootstrap"]["hi_pct"]
    horizons = c["competing_risks"]["cif_horizons"]
    df = with_week(df)
    if not df.height:
        return {}
    weeks = df["week"].unique().to_list()
    rng = np.random.default_rng(20260919)
    samples = {"break": {h: [] for h in horizons}, "reject": {h: [] for h in horizons}}
    by_week = {w: df.filter(pl.col("week") == w) for w in weeks}
    for _ in range(B):
        pick = rng.choice(weeks, size=len(weeks), replace=True)
        boot = pl.concat([by_week[w] for w in pick])
        cif = cif_empirical(boot, c)
        for ca in ("break", "reject"):
            for h in horizons:
                samples[ca][h].append(cif[ca].get(h))
    band = {}
    for ca in ("break", "reject"):
        band[ca] = {}
        for h in horizons:
            arr = np.array([x for x in samples[ca][h] if x is not None], dtype=float)
            band[ca][h] = [round(float(np.percentile(arr, lo)), 4), round(float(np.percentile(arr, hi)), 4)] if len(arr) else None
    return band


# --------------------------------------------------------------------------- pooled hazard (1.2)

def _person_period(df: pl.DataFrame, c: dict) -> pl.DataFrame:
    """Expand each test into one row per day at risk (day 0..ttr) with a discrete-time competing-
    risks outcome (break / reject / survive) and the day-shape features. This is the design for the
    pooled multinomial hazard."""
    pp = df.with_columns(pl.int_ranges(0, pl.col("ttr") + 1).alias("d")).explode("d")
    pp = pp.with_columns(
        pl.when((pl.col("d") == pl.col("ttr")) & (~pl.col("cens")))
        .then(pl.col("cause")).otherwise(pl.lit("survive")).alias("y"),
        pl.col("d").cast(pl.Float64).alias("day"),
        (pl.col("d").cast(pl.Float64) + 1.0).log().alias("day_log"),
        (pl.col("side") + ":" + pl.col("r_def")).alias("cell"),
    )
    return pp


def _featurize(pp: pl.DataFrame, cont_cols: list[str], med: dict, mu: np.ndarray, sd: np.ndarray, cells: list[str]) -> np.ndarray:
    cont = np.column_stack([
        pl.Series([med[cc] if v is None else v for v in pp[cc].to_list()]).to_numpy().astype(float)
        for cc in cont_cols
    ])
    cont = (cont - mu) / sd
    cellv = pp["cell"].to_list()
    onehot = np.zeros((pp.height, len(cells)))
    idx = {cn: i for i, cn in enumerate(cells)}
    for r, cn in enumerate(cellv):
        if cn in idx:
            onehot[r, idx[cn]] = 1.0
    return np.hstack([cont, onehot])


def fit_pooled_hazard(df_single: pl.DataFrame, c: dict):
    """§1.2 — ONE partially-pooled multinomial hazard over all six single-definition cells, so the
    sparse 90-day-high cell borrows the shared covariate slopes instead of starving. sklearn L2
    (ridge) multinomial; the cell one-hot gives each cell its own baseline while the slopes are
    pooled. Returns a pack used to predict cause-specific cumulative incidence."""
    import warnings

    from sklearn.linear_model import LogisticRegression

    pp = _person_period(df_single, c)
    if pp.height < 200 or pp["y"].n_unique() < 3:
        return None
    cont_cols = [*CIF_FEATURES, "day", "day_log"]
    med = {cc: float(np.nanmedian(pp[cc].to_numpy().astype(float))) for cc in cont_cols}
    raw = np.column_stack([
        pl.Series([med[cc] if v is None else v for v in pp[cc].to_list()]).to_numpy().astype(float)
        for cc in cont_cols
    ])
    mu, sd = raw.mean(0), raw.std(0)
    sd[sd == 0] = 1.0
    cells = sorted(pp["cell"].unique().to_list())
    X = _featurize(pp, cont_cols, med, mu, sd, cells)
    y = np.array(pp["y"].to_list())
    # sklearn >=1.7 fits multinomial by default for multiclass with the lbfgs solver; L2 is the
    # default penalty (ridge), which is the shrinkage the partial pooling relies on
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        model = LogisticRegression(C=c["pooling"]["ridge_C"], max_iter=2000).fit(X, y)
    return {"model": model, "cont_cols": cont_cols, "med": med, "mu": mu, "sd": sd,
            "cells": cells, "classes": list(model.classes_)}


def _cif_from_hazards(hb: np.ndarray, hr: np.ndarray, horizons: list[int]) -> tuple[dict, dict]:
    """Cause-specific cumulative incidence from a per-day hazard path (day index = array index)."""
    S = 1.0
    cb = dict.fromkeys(horizons, 0.0)
    cr = dict.fromkeys(horizons, 0.0)
    for d in range(len(hb)):
        for h in horizons:
            if d <= h:
                cb[h] += hb[d] * S
                cr[h] += hr[d] * S
        S *= max(1.0 - hb[d] - hr[d], 0.0)
    return {h: cb[h] for h in horizons}, {h: cr[h] for h in horizons}


def predict_cif_tests(pack, tests: list[dict], c: dict) -> list[dict]:
    """Predict cause-specific cumulative incidence for a list of tests (each a state dict with
    'side' and 'r_def'). Single-definition cells use their own cell; a 'consensus' test is the mean
    of its side's three definition cells."""
    if pack is None:
        return [{} for _ in tests]
    horizons = c["competing_risks"]["cif_horizons"]
    hmax = max(horizons)
    bi, ri = pack["classes"].index("break"), pack["classes"].index("reject")
    out = []
    for trow in tests:
        side, rd = trow["side"], trow["r_def"]
        defs = R_DEFS if rd == "consensus" else (rd,)
        cbs, crs = [], []
        for dd in defs:
            rows = [{"cell": f"{side}:{dd}", "day": float(d), "day_log": float(np.log(d + 1)),
                     **{cc: (None if trow.get(cc) is None else float(trow.get(cc))) for cc in CIF_FEATURES}}
                    for d in range(hmax + 1)]
            pp = pl.DataFrame(rows, infer_schema_length=None)
            X = _featurize(pp, pack["cont_cols"], pack["med"], pack["mu"], pack["sd"], pack["cells"])
            P = pack["model"].predict_proba(X)
            hb, hr = P[:, bi], P[:, ri]
            cb, cr = _cif_from_hazards(hb, hr, horizons)
            cbs.append(cb)
            crs.append(cr)
        out.append({
            "break": {h: float(np.mean([x[h] for x in cbs])) for h in horizons},
            "reject": {h: float(np.mean([x[h] for x in crs])) for h in horizons},
        })
    return out


def _realized_cause_by(row: dict, cause: str, h: int) -> int | None:
    """Whether the test resolved to `cause` by horizon h, or None if it was censored before h."""
    if row["cens"] and row["ttr"] < h:
        return None
    return 1 if (row["cause"] == cause and row["ttr"] <= h) else 0


def walkforward_recalibrate(df_single: pl.DataFrame, c: dict) -> dict:
    """§1.1 — walk-forward isotonic recalibration of the conditional CIF, and the reliability/Brier
    evidence. Expanding window: refit the pooled hazard on a cadence, predict each later test's
    CIF(break, h) out of sample, and map predicted->realised with isotonic regression. Returns the
    per-horizon calibrators (fit on all OOS pairs, for live use), raw vs recalibrated reliability,
    and the model Brier against the base-rate Brier (skill over the naive forecast)."""
    from sklearn.isotonic import IsotonicRegression

    horizons = c["competing_risks"]["cif_horizons"]
    prim = 20 if 20 in horizons else horizons[len(horizons) // 2]
    s = c["samples"]
    df = df_single.sort("date")
    rows = df.to_dicts()
    n = len(rows)
    oos = {ca: {h: {"pred": [], "real": []} for h in horizons} for ca in ("break", "reject")}
    by_def: dict[str, list] = {}     # primary-horizon (pred, real) tagged by r_def
    by_consensus: dict[bool, list] = {True: [], False: []}
    pack = None
    last_fit = None
    for i in range(n):
        r = rows[i]
        if i >= s["min_train_events"]:
            need = pack is None or last_fit is None or (r["date"] - last_fit).days >= s["refit_cadence_days"]
            if need:
                pack = fit_pooled_hazard(df.slice(0, i), c)  # polars slice keeps the schema
                last_fit = r["date"]
            if pack is not None:
                pr = predict_cif_tests(pack, [r], c)[0]
                if not pr:
                    continue
                for ca in ("break", "reject"):
                    for h in horizons:
                        y = _realized_cause_by(r, ca, h)
                        if y is not None:
                            oos[ca][h]["pred"].append(pr[ca][h])
                            oos[ca][h]["real"].append(y)
                yp = _realized_cause_by(r, "break", prim)
                if yp is not None:
                    by_def.setdefault(r["r_def"], []).append((pr["break"][prim], yp))
                    if "consensus_flag" in r:
                        by_consensus[bool(r["consensus_flag"])].append((pr["break"][prim], yp))
    # one isotonic calibrator per (cause, horizon); reliability/Brier reported for the break cause
    calibrators = {"break": {}, "reject": {}}
    reliability, brier = {}, {}
    for ca in ("break", "reject"):
        for h in horizons:
            pred = np.array(oos[ca][h]["pred"])
            real = np.array(oos[ca][h]["real"], dtype=float)
            if len(pred) >= c["recalibration"]["min_pairs"] and len(set(real.tolist())) > 1:
                iso = IsotonicRegression(out_of_bounds="clip", y_min=0.0, y_max=1.0).fit(pred, real)
                calibrators[ca][h] = iso
                if ca == "break":
                    recal = iso.predict(pred)
                    base = float(real.mean())
                    brier[h] = {"raw": round(float(np.mean((pred - real) ** 2)), 4),
                                "recal": round(float(np.mean((recal - real) ** 2)), 4),
                                "base_rate": round(float(np.mean((base - real) ** 2)), 4), "n": len(pred)}
                    reliability[h] = {"raw": _reliability(list(pred), list(real), s["calibration_bins"]),
                                      "recal": _reliability(list(recal), list(real), s["calibration_bins"])}
            else:
                calibrators[ca][h] = None
                if ca == "break":
                    brier[h] = {"n": len(pred), "raw": None, "recal": None, "base_rate": None}
    def _brier(pairs):
        if len(pairs) < c["recalibration"]["min_pairs"]:
            return {"n": len(pairs), "brier": None}
        p = np.array([a for a, _ in pairs])
        y = np.array([b for _, b in pairs], dtype=float)
        return {"n": len(pairs), "brier": round(float(np.mean((p - y) ** 2)), 4),
                "base_rate": round(float(np.mean((y.mean() - y) ** 2)), 4)}

    brier_by_def = {k: _brier(v) for k, v in by_def.items()}
    brier_by_def["consensus_days"] = _brier(by_consensus[True])
    brier_by_def["non_consensus_days"] = _brier(by_consensus[False])
    return {"calibrators": calibrators, "reliability": reliability, "brier": brier,
            "n_oos": {h: len(oos["break"][h]["pred"]) for h in horizons},
            "primary_horizon": prim, "brier_by_def": brier_by_def}


# --------------------------------------------------------------------------- orchestration (WO9)

def _apply_calibrators(cif: dict, calibrators: dict) -> dict:
    out = {}
    for h, v in cif.items():
        iso = calibrators.get(h)
        out[h] = round(float(iso.predict([v])[0]), 4) if iso is not None else round(float(v), 4)
    return out


def _active_bands(df_single: pl.DataFrame, tests: list[dict], c: dict, calibrators: dict) -> list[dict]:
    """§1.3 — block-bootstrap the pooled hazard over weeks and re-predict each open test, applying the
    same recalibration as the point estimate, so the band is on the published (recalibrated) scale.
    Refits are the cost, so this runs only when tests are open."""
    B = c["bootstrap"]["n_model"]
    lo, hi = c["bootstrap"]["lo_pct"], c["bootstrap"]["hi_pct"]
    horizons = c["competing_risks"]["cif_horizons"]
    df = with_week(df_single)
    weeks = df["week"].unique().to_list()
    by_week = {w: df.filter(pl.col("week") == w) for w in weeks}
    rng = np.random.default_rng(20260919)
    acc = [{"break": {h: [] for h in horizons}, "reject": {h: [] for h in horizons}} for _ in tests]
    for _ in range(B):
        pick = rng.choice(weeks, size=len(weeks), replace=True)
        pack = fit_pooled_hazard(pl.concat([by_week[w] for w in pick]), c)
        if pack is None:
            continue
        preds = predict_cif_tests(pack, tests, c)
        for j, pr in enumerate(preds):
            if not pr:
                continue
            rb = _apply_calibrators(pr["break"], calibrators.get("break", {}))
            rr = _apply_calibrators(pr["reject"], calibrators.get("reject", {}))
            for ca, rc in (("break", rb), ("reject", rr)):
                for h in horizons:
                    acc[j][ca][h].append(rc[h])
    bands = []
    for a in acc:
        b = {"break": {}, "reject": {}}
        for ca in ("break", "reject"):
            for h in horizons:
                arr = np.array(a[ca][h], dtype=float)
                b[ca][h] = [round(float(np.percentile(arr, lo)), 4), round(float(np.percentile(arr, hi)), 4)] if len(arr) else None
        bands.append(b)
    return bands


def _cell_magnitude(df: pl.DataFrame, c: dict) -> dict:
    """Mean adverse/continuation move by cause at each CIF horizon, with a block-bootstrap band."""
    horizons = c["competing_risks"]["cif_horizons"]
    out = {}
    for ca in ("break", "reject"):
        sub = df.filter(pl.col("cause") == ca)
        out[ca] = {}
        for h in horizons:
            x = sub[f"mag_cif_{h}"].to_numpy()
            x = x[np.isfinite(x)]
            out[ca][h] = {"mean": round(float(x.mean()), 4), "n": len(x)} if len(x) >= 5 else {"mean": None, "n": len(x)}
    return out


def run_model(events: pl.DataFrame, c: dict, as_of: date, calibrated_on: date) -> dict:
    """Assemble the whole work-order-9 resistance block: competing-risks cumulative incidence per
    cell with bootstrap bands (the base-rate anchor), the partially-pooled + walk-forward-recalibrated
    conditional model, the open tests led by recalibrated CIF with intervals, the consensus-vs-
    definition Brier comparison, and the live scorecard. The raw WO8 multinomial is retained for one
    release for comparison."""
    single = events.filter(pl.col("r_def").is_in(R_DEFS)).sort("date")
    # consensus flag for the Brier comparison (does a 2-of-3 agreement predict better)
    flags = single.group_by(["base", "side", "date"]).agg(pl.col("r_def").n_unique().alias("_ndef"))
    single = single.join(flags, on=["base", "side", "date"]).with_columns(
        (pl.col("_ndef") >= c["consensus"]["min_defs"]).alias("consensus_flag")
    )
    cons = consensus_events(events, c)
    horizons = c["competing_risks"]["cif_horizons"]

    recal = walkforward_recalibrate(single, c)
    pack = fit_pooled_hazard(single, c)

    # cells: empirical CIF (anchor) + band + median ttr + magnitude, for the 3 defs and consensus
    cells = []
    for side in SIDES:
        for rd in CIF_CELLS:
            sub = cons.filter(pl.col("side") == side) if rd == "consensus" else \
                single.filter((pl.col("side") == side) & (pl.col("r_def") == rd))
            if not sub.height:
                continue
            emp = cif_empirical(sub, c)
            band = _cif_bootstrap(sub, c)
            cell = {"side": side, "r_def": rd, "n": emp["n"], "n_resolved": emp["n_resolved"],
                    "cif_break": emp["break"], "cif_reject": emp["reject"],
                    "band_break": band.get("break"), "band_reject": band.get("reject"),
                    "median_ttr": emp["median_ttr"], "magnitude": _cell_magnitude(sub, c)}
            # raw WO8 multinomial retained one release (single defs only)
            if rd in R_DEFS:
                raw = fit_direction(single, side, rd, 5, BASE_REGRESSORS, c)
                cell["raw_multinomial"] = {"base_rate": raw.get("base_rate"), "n": raw.get("n_events"),
                                           "published": raw.get("published")}
            cells.append(cell)

    # choose the headline definition by out-of-sample Brier: consensus vs single (per §2.2)
    bbd = recal.get("brier_by_def", {})
    scored = {k: v.get("brier") for k, v in bbd.items() if k in R_DEFS and v.get("brier") is not None}
    cons_b = bbd.get("consensus_days", {}).get("brier")
    headline = "consensus" if (cons_b is not None and (not scored or cons_b <= min(scored.values()))) else \
        (min(scored, key=scored.get) if scored else "vp")

    return {"single": single, "cons": cons, "pack": pack, "recal": recal, "cells": cells,
            "headline_def": headline, "horizons": horizons}


def active_block(model: dict, c: dict, as_of: date) -> list[dict]:
    """Open tests (§5), led by the recalibrated cause-specific CIF with bootstrap bands. A consensus
    test is preferred when the day is a 2-of-3 agreement; otherwise the strongest single definition."""
    single, cons, pack, recal = model["single"], model["cons"], model["pack"], model["recal"]
    horizons = c["competing_risks"]["cif_horizons"]
    window = max(horizons)
    open_single = single.filter((pl.col("date") >= pl.lit(as_of) - pl.duration(days=window)) & pl.col("cens"))
    if not open_single.height:
        return []
    # dedupe to the most recent open test per (base, side, r_def)
    open_single = open_single.sort("date").group_by(["base", "side", "r_def"], maintain_order=True).last()
    # promote to consensus rows where the day agrees
    cons_keys = set()
    if cons.height:
        cons_keys = {(r["base"], r["side"], r["date"]) for r in cons.to_dicts()}
    tests, meta = [], []
    seen = set()
    for r in open_single.to_dicts():
        key = (r["base"], r["side"], r["date"])
        rd = "consensus" if key in cons_keys else r["r_def"]
        dedup = (r["base"], r["side"], rd, r["date"])
        if dedup in seen:
            continue
        seen.add(dedup)
        t = {"side": r["side"], "r_def": rd, **{k: r.get(k) for k in CIF_FEATURES}}
        tests.append(t)
        meta.append(r)
    raw_preds = predict_cif_tests(pack, tests, c)
    bands = _active_bands(single, tests, c, recal["calibrators"]) if tests else []
    out = []
    for i, (t, r) in enumerate(zip(tests, meta)):
        raw = raw_preds[i]
        if not raw:
            continue
        recal_break = _apply_calibrators(raw["break"], recal["calibrators"]["break"])
        recal_reject = _apply_calibrators(raw["reject"], recal["calibrators"]["reject"])
        out.append({
            "base": r["base"], "side": r["side"], "r_def": t["r_def"], "date": str(r["date"]),
            "R": r.get("R"), "dist": r.get("dist"), "close_cleared": r.get("close_cleared"),
            "level_age": r.get("level_age"),
            "cif_break": recal_break, "cif_reject": recal_reject,
            "cif_break_raw": {h: round(raw["break"][h], 4) for h in horizons},
            "band_break": bands[i]["break"] if i < len(bands) else None,
            "band_reject": bands[i]["reject"] if i < len(bands) else None,
        })
    return out


def tier3_overlay(single: pl.DataFrame, c: dict) -> dict:
    """§3 (Tier 3) — the crowding question stays PRELIMINARY and never drives a published number.
    Reports Phi's coefficient on P(reject) per resistance definition (still definition-dependent),
    the Lambda/D magnitude cell (still insufficient sample), and the per-cell overlay event counts
    for the 6 October review. It is not allowed to move the headline cumulative incidence."""
    H = 20 if 20 in c["competing_risks"]["cif_horizons"] else 5
    phi = {}
    for rd in R_DEFS:
        try:
            fit = fit_direction(single, "resistance", rd, H, (*BASE_REGRESSORS, "phi", "funding_z"), c)
            phi[rd] = (fit.get("reject_effects", {}) or {}).get("phi")
        except Exception:
            phi[rd] = None
    lam = overlay_magnitude_reject(single, "resistance", "vp", H, c)
    counts = {rd: int(single.filter((pl.col("r_def") == rd) & pl.col("funding_z").is_not_null()).height)
              for rd in R_DEFS}
    return {
        "preliminary": True, "horizon": H, "phi_on_reject": phi,
        "lambda_over_depth_magnitude": {"n": lam.get("n"), "published": lam.get("published"),
                                        "reason": lam.get("reason")},
        "overlay_event_counts": counts,
        "review_note": "6 October review: report the crowding overlay's independent-event count per "
                       "cell and whether any cell has crossed the publish threshold; only then "
                       "revisit whether Phi shifts direction or magnitude at a test. The accelerants "
                       "are collector coverage (WO6-B) and the TradingView leverage backfill (WO5), "
                       "not the model.",
    }


def scorecard_cif(events: pl.DataFrame, model: dict, c: dict, as_of: date, calibrated_on: date) -> pl.DataFrame:
    """§1.4 — every test that RESOLVED since go-live, with the recalibrated predicted P(break by the
    primary horizon) and the realised cause, so the forward Brier (vs the base-rate Brier) accrues in
    public."""
    single, pack, recal = model["single"], model["pack"], model["recal"]
    prim = recal.get("primary_horizon", 20)
    df = single.filter(~pl.col("cens")).with_columns(
        (pl.col("date") + pl.duration(days=pl.col("ttr"))).alias("resolve_date")
    )
    df = df.filter((pl.col("resolve_date") >= pl.lit(calibrated_on)) & (pl.col("resolve_date") <= pl.lit(as_of)))
    if not df.height:
        return pl.DataFrame()
    rows = df.to_dicts()
    tests = [{"side": r["side"], "r_def": r["r_def"], **{k: r.get(k) for k in CIF_FEATURES}} for r in rows]
    preds = predict_cif_tests(pack, tests, c)
    out = []
    for r, pr in zip(rows, preds):
        if not pr:
            continue
        pb = _apply_calibrators(pr["break"], recal["calibrators"]["break"]).get(prim)
        out.append({"base": r["base"], "date": r["date"], "resolve_date": r["resolve_date"],
                    "side": r["side"], "r_def": r["r_def"], "horizon": prim,
                    "p_break": pb, "realized": r["cause"]})
    return pl.DataFrame(out).sort(["resolve_date", "base"]) if out else pl.DataFrame()
