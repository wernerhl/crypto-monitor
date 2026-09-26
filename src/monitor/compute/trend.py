"""Work order 10 §1 — trend state, a first-class market layer beside Φ.

The system measured HOW MUCH leverage sits on the market and had no representation of WHICH WAY the
market is going, so a trend being bought read as a fragility warning. This adds a per-asset trend
state from time-series momentum (price vs 20/50/200-day averages and their slopes, trailing 1- and
3-month returns, Donchian 20/55 breakout state) and a single frozen categorical state
UPTREND / DOWNTREND / RANGE, with its historical continuation base rate P(next-20d return > 0 |
state). It is a state and a base rate, not a trigger; it changes no threshold and does not touch Φ.
"""

from __future__ import annotations

from datetime import date

import numpy as np
import polars as pl

from monitor.compute.resistance import canonical_ohlc
from monitor.paths import CONFIG

STATES = ("UPTREND", "DOWNTREND", "RANGE")


def cfg() -> dict:
    import yaml

    return yaml.safe_load((CONFIG / "trend_state.yaml").read_text())


def _sma(x: np.ndarray, w: int) -> np.ndarray:
    out = np.full(len(x), np.nan)
    if len(x) >= w:
        c = np.cumsum(np.insert(x, 0, 0.0))
        out[w - 1 :] = (c[w:] - c[:-w]) / w
    return out


def _series(close: np.ndarray, c: dict) -> dict:
    """Per-day arrays of the trend features and the categorical state (all causal — computed from
    closes up to and including t, no look-ahead)."""
    n = len(close)
    mas = {w: _sma(close, w) for w in c["ma_windows"]}
    sl = c["slope_lookback"]
    rising = {w: np.array([mas[w][i] > mas[w][i - sl] if i >= sl and np.isfinite(mas[w][i]) and np.isfinite(mas[w][i - sl]) else False for i in range(n)]) for w in c["ma_windows"]}
    r = c["rule"]
    state = np.array(["RANGE"] * n, dtype=object)
    for i in range(n):
        up = (
            np.isfinite(mas[50][i]) and np.isfinite(mas[200][i]) and np.isfinite(mas[20][i])
            and close[i] > mas[r["uptrend"]["close_above"]][i]
            and all(rising[w][i] for w in r["uptrend"]["rising"])
            and mas[r["uptrend"]["fast_above_slow"][0]][i] > mas[r["uptrend"]["fast_above_slow"][1]][i]
        )
        dn = (
            np.isfinite(mas[50][i]) and np.isfinite(mas[200][i]) and np.isfinite(mas[20][i])
            and close[i] < mas[r["downtrend"]["close_below"]][i]
            and all(not rising[w][i] for w in r["downtrend"]["falling"])
            and mas[r["downtrend"]["fast_below_slow"][0]][i] < mas[r["downtrend"]["fast_below_slow"][1]][i]
        )
        if up:
            state[i] = "UPTREND"
        elif dn:
            state[i] = "DOWNTREND"
    return {"ma": mas, "rising": rising, "state": state}


def _donchian(high: np.ndarray, low: np.ndarray, close: np.ndarray, w: int, t: int) -> str:
    if t < w:
        return "inside"
    hh = np.nanmax(high[t - w : t])
    ll = np.nanmin(low[t - w : t])
    if close[t] >= hh:
        return "new high"
    if close[t] <= ll:
        return "new low"
    return "inside"


def states(prices: pl.DataFrame, c: dict) -> list[dict]:
    """Latest trend state per asset (as of the last daily close), with the momentum features that
    define it."""
    ohlc = canonical_ohlc(prices, c["assets"], c["venue_priority"])
    if not ohlc.height:
        return []
    out = []
    for base, g in ohlc.group_by("base", maintain_order=True):
        base = base[0] if isinstance(base, tuple) else base
        g = g.sort("date")
        if g.height < max(c["ma_windows"]) + c["slope_lookback"]:
            continue
        close = g["close"].to_numpy()
        high, low = g["high"].to_numpy(), g["low"].to_numpy()
        dates = g["date"].to_list()
        s = _series(close, c)
        t = g.height - 1
        r1 = c["ret_windows"]["1m"]
        r3 = c["ret_windows"]["3m"]
        out.append({
            "base": base, "date": str(dates[t]), "state": str(s["state"][t]),
            "close": float(close[t]),
            "ma20": float(s["ma"][20][t]) if np.isfinite(s["ma"][20][t]) else None,
            "ma50": float(s["ma"][50][t]) if np.isfinite(s["ma"][50][t]) else None,
            "ma200": float(s["ma"][200][t]) if np.isfinite(s["ma"][200][t]) else None,
            "ma50_rising": bool(s["rising"][50][t]), "ma200_rising": bool(s["rising"][200][t]),
            "ret_1m": float(close[t] / close[t - r1] - 1.0) if t >= r1 else None,
            "ret_3m": float(close[t] / close[t - r3] - 1.0) if t >= r3 else None,
            "donchian_20": _donchian(high, low, close, c["donchian"][0], t),
            "donchian_55": _donchian(high, low, close, c["donchian"][1], t),
        })
    return out


def _iso_week(d: date) -> int:
    iso = d.isocalendar()
    return iso[0] * 100 + iso[1]


def continuation_base_rate(prices: pl.DataFrame, c: dict) -> dict:
    """P(next-N-session return > 0 | trend state), pooled across assets over full history, with a
    week-clustered standard error (§1). Walk-forward by construction: the state at t uses only
    closes up to t and the outcome is the strictly-forward return. Descriptive base rate, no fit."""
    ohlc = canonical_ohlc(prices, c["assets"], c["venue_priority"])
    H = c["continuation_horizon"]
    rows_state, rows_y, rows_wk = [], [], []
    for _base, g in ohlc.group_by("base", maintain_order=True):
        g = g.sort("date")
        n = g.height
        if n < max(c["ma_windows"]) + H + 5:
            continue
        close = g["close"].to_numpy()
        dates = g["date"].to_list()
        st = _series(close, c)["state"]
        for t in range(max(c["ma_windows"]), n - H):
            rows_state.append(st[t])
            rows_y.append(1.0 if close[t + H] / close[t] - 1.0 > 0 else 0.0)
            rows_wk.append(_iso_week(dates[t]))
    out = {"horizon": H, "by_state": {}}
    if not rows_state:
        return out
    state_arr = np.array(rows_state)
    y = np.array(rows_y)
    wk = np.array(rows_wk)
    for s in STATES:
        m = state_arr == s
        n = int(m.sum())
        if n == 0:
            out["by_state"][s] = {"n": 0, "p_up": None, "se": None}
            continue
        ys = y[m]
        phat = float(ys.mean())
        # week-clustered s.e. of the mean (sandwich): groups are ISO weeks
        wks = wk[m]
        groups = np.unique(wks)
        G = len(groups)
        if G > 1:
            ss = sum((ys[wks == gk] - phat).sum() ** 2 for gk in groups)
            se = float(np.sqrt(G / (G - 1) * ss) / n)
        else:
            se = float(np.sqrt(phat * (1 - phat) / max(n, 1)))
        out["by_state"][s] = {"n": n, "p_up": round(phat, 4), "se": round(se, 4)}
    return out


def phi_by_trend(prices: pl.DataFrame, fragility_series: pl.DataFrame | None, c: dict) -> dict:
    """§1 backtest — does Φ's (null) relation to forward outcomes change once conditioned on trend
    state? Report the interaction descriptively: mean Φ and mean next-20d return by state, and the
    within-state correlation of Φ to the forward return. Do NOT tune Φ to it."""
    if fragility_series is None or not fragility_series.height:
        return {}
    btc = canonical_ohlc(prices, ["BTC"], c["venue_priority"]).sort("date")
    if not btc.height:
        return {}
    close = btc["close"].to_numpy()
    dates = btc["date"].to_list()
    st = _series(close, c)["state"]
    H = c["continuation_horizon"]
    idx = {d: i for i, d in enumerate(dates)}
    phi_map = {r["date"]: r["phi"] for r in fragility_series.to_dicts() if r.get("phi") is not None}
    rows = []
    for d, phi in phi_map.items():
        i = idx.get(d)
        if i is None or i >= len(dates) - H:
            continue
        rows.append((str(st[i]), float(phi), float(close[i + H] / close[i] - 1.0)))
    out = {"horizon": H, "by_state": {}}
    for s in STATES:
        sub = [(p, r) for (ss, p, r) in rows if ss == s]
        if len(sub) < 20:
            out["by_state"][s] = {"n": len(sub)}
            continue
        p = np.array([x[0] for x in sub])
        r = np.array([x[1] for x in sub])
        corr = float(np.corrcoef(p, r)[0, 1]) if p.std() > 0 and r.std() > 0 else None
        out["by_state"][s] = {"n": len(sub), "mean_phi": round(float(p.mean()), 3),
                              "mean_fwd_ret": round(float(r.mean()), 4),
                              "corr_phi_fwd": round(corr, 3) if corr is not None else None}
    return out
