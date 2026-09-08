"""Regenerate docs/notes/fragility_validation.md and its SVG figures from the phi_shock_* tables
(work order 3, item 5). Run after `monitor compute weekly`:

    PYTHONPATH=src uv run --no-sync python scripts/fragility_validation_note.py

Reporting only: no change to Φ or to any threshold."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import polars as pl

from monitor import archive

OUT = Path("docs/notes")
FIG = OUT / "figures"
OUTCOMES = {
    "mdd_5": ("max drawdown, 5 days", "%"),
    "mdd_20": ("max drawdown, 20 days", "%"),
    "rv_20": ("realised vol, days 1–20 (ann.)", "%"),
    "days_to_recover": ("days to recover P₋₁ (censored at 60)", "d"),
}
NAMES = {
    "phi_pre": "Φ₍t−1₎",
    "z_fr": "funding z",
    "z_oi": "OI/cap z",
    "z_vrp_neg": "−VRP z",
    "z_dd": "range position",
    "z_sc_neg": "−stablecoin growth z",
}


def f(v, unit):
    if v is None or (isinstance(v, float) and not np.isfinite(v)):
        return "n/a"
    return f"{100 * v:+.1f} %" if unit == "%" else f"{v:.1f} d"


def terc_table(t: pl.DataFrame) -> str:
    out = [
        "| sample | Φ tercile | Φ range | n | MDD 5 d mean (p25–p75) | MDD 20 d mean (p25–p75) | RV 20 d mean | days to recover median | recovered within 60 d |",
        "|---|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for r in t.to_dicts():
        rng = f"{'' if r['phi_lo'] is None else f'{r["phi_lo"]:+.2f}'} … {'' if r['phi_hi'] is None else f'{r["phi_hi"]:+.2f}'}"
        out.append(
            f"| {r['sample']} | {r['tercile']} | {rng} | {r['n']} | {f(r['mdd_5_mean'], '%')} ({f(r['mdd_5_p25'], '%')} – {f(r['mdd_5_p75'], '%')}) | {f(r['mdd_20_mean'], '%')} ({f(r['mdd_20_p25'], '%')} – {f(r['mdd_20_p75'], '%')}) | {f(r['rv_20_mean'], '%')} | {f(r['days_to_recover_median'], 'd')} | {'n/a' if r['recovered_share'] is None else f'{100 * r["recovered_share"]:.0f} %'} |"
        )
    return "\n".join(out)


def reg_table(reg: pl.DataFrame) -> str:
    out = [
        "| sample | outcome | regressor | n | slope per unit of regressor | Newey–West s.e. | t | R² |",
        "|---|---|---|---:|---:|---:|---:|---:|",
    ]
    for r in reg.to_dicts():
        unit = OUTCOMES[r["outcome"]][1]
        sl = (
            "n/a"
            if r["slope"] is None
            else (f"{100 * r['slope']:+.2f} pts" if unit == "%" else f"{r['slope']:+.2f} d")
        )
        se = (
            "" if r["se"] is None else (f"{100 * r['se']:.2f}" if unit == "%" else f"{r['se']:.2f}")
        )
        out.append(
            f"| {r['sample']} | {OUTCOMES[r['outcome']][0]} | {NAMES.get(r['regressor'], r['regressor'])} | {r['n']} | {sl} | {se} | {'' if r['t'] is None else f'{r["t"]:+.2f}'} | {'' if r['r2'] is None else f'{r["r2"]:.3f}'} |"
        )
    return "\n".join(out)


def bars_svg(path: Path, t: pl.DataFrame, outcome: str, title: str) -> None:
    samples = [
        s
        for s in ("shocks", "shocks, ≥3 components", "placebo")
        if t.filter(pl.col("sample") == s).height
    ]
    w, h, left, bottom, top = 760, 320, 70, 70, 40
    vals = {(r["sample"], r["tercile"]): r for r in t.to_dicts()}
    allv = [abs(r[f"{outcome}_mean"]) for r in t.to_dicts() if r[f"{outcome}_mean"] is not None]
    vmax = max([*allv, 1e-6]) * 1.15
    unit = OUTCOMES[outcome][1]

    def y(v):
        return top + (h - top - bottom) * (1 - v / vmax)

    cols = {"low Φ": "#7dd39a", "mid Φ": "#e5b85a", "high Φ": "#e0776f"}
    s = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<text x="{left}" y="22" font-size="14" font-weight="bold">{title}</text>',
    ]
    gw = (w - left - 20) / max(len(samples), 1)
    for gi, smp in enumerate(samples):
        for ti, terc in enumerate(("low Φ", "mid Φ", "high Φ")):
            r = vals.get((smp, terc))
            if not r or r[f"{outcome}_mean"] is None:
                continue
            v = abs(r[f"{outcome}_mean"])
            x0 = left + gi * gw + 10 + ti * (gw - 20) / 3
            bw = (gw - 20) / 3 * 0.8
            s.append(
                f'<rect x="{x0:.1f}" y="{y(v):.1f}" width="{bw:.1f}" height="{y(0) - y(v):.1f}" fill="{cols[terc]}"/>'
            )
            lab = f"{100 * v:.1f}%" if unit == "%" else f"{v:.1f}"
            s.append(
                f'<text x="{x0 + bw / 2:.1f}" y="{y(v) - 4:.1f}" text-anchor="middle">{lab}</text>'
            )
            s.append(
                f'<text x="{x0 + bw / 2:.1f}" y="{h - bottom + 14}" text-anchor="middle" font-size="10">{terc} (n={r["n"]})</text>'
            )
        s.append(
            f'<text x="{left + gi * gw + gw / 2:.1f}" y="{h - bottom + 32}" text-anchor="middle">{smp}</text>'
        )
    s.append(
        f'<text x="{left}" y="{h - 8}" fill="#555">bars: |mean| of {OUTCOMES[outcome][0]} by Φ tercile (edges from the shock sample)</text></svg>'
    )
    path.write_text("\n".join(s))


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    ev, plc, reg, terc = (
        archive.read(t)
        for t in (
            "phi_shock_events",
            "phi_shock_placebo",
            "phi_shock_regressions",
            "phi_shock_terciles",
        )
    )
    if ev is None or not ev.height:
        raise SystemExit("run `monitor compute weekly` first")
    as_of = ev["as_of"].max()
    ev, plc, reg, terc = (
        d.filter(pl.col("as_of") == as_of) if d is not None else d for d in (ev, plc, reg, terc)
    )
    for o in OUTCOMES:
        bars_svg(
            FIG / f"phi_shock_{o}.svg",
            terc,
            o,
            f"{OUTCOMES[o][0]} after a shock, by pre-shock Φ tercile",
        )
    nc = ev["n_components"].value_counts().sort("n_components").to_dicts()
    sub = ev.filter(pl.col("n_components") >= 3)
    r_phi = reg.filter(pl.col("regressor") == "phi_pre")
    rs = {(r["sample"], r["outcome"]): r for r in r_phi.to_dicts()}

    def slope_txt(sample, outcome):
        r = rs.get((sample, outcome), {})
        if not r or r.get("slope") is None:
            return "n/a"
        unit = OUTCOMES[outcome][1]
        return (
            f"{100 * r['slope']:+.2f} points per unit of Φ (t = {r['t']:+.1f})"
            if unit == "%"
            else f"{r['slope']:+.2f} days per unit of Φ (t = {r['t']:+.1f})"
        )

    from monitor.compute.fragility_validation import verdict

    vd = verdict(reg)
    tt = {(r["sample"], r["tercile"]): r for r in terc.to_dicts()}
    lo, hi = tt.get(("shocks", "low Φ"), {}), tt.get(("shocks", "high Φ"), {})
    plo, phi_ = tt.get(("placebo", "low Φ"), {}), tt.get(("placebo", "high Φ"), {})
    md = f"""# Does Φ predict the damage after a shock? (2017-12 → {as_of})

*Work order 3, item 5. Generated {as_of} from `phi_shock_events`, `phi_shock_placebo`,
`phi_shock_regressions` and `phi_shock_terciles` (`monitor.compute.fragility_validation`, weekly
job) by `scripts/fragility_validation_note.py`. Walk-forward, filtered values only. No change
to Φ's definition or to any threshold.*

## Claim under test

Notes §6.2: Φ measures the size of the dry pile, not the timing of the spark. Conditional on a
spark, the damage should increase in Φ. So, on shock days, the drawdown that follows, the
realised vol that follows and the time to recover should all be worse when Φ was high the
day before. A null here matters as much as the null on Rule 5.1.

## Design

* **Shock day.** A daily BTC log return below its rolling 250-day 5th percentile, the
  percentile computed on the prior 250 days only. {ev.height} shock days between
  {ev["date"].min()} and {ev["date"].max()} with a 60-day window elapsed.
* **Pre-shock state.** Φ₍t−1₎ and its components from the walk-forward `fragility_series`.
  Components available by shock day: {", ".join(f"{d['n_components']} of 5 on {d['count']} days" for d in nc)}.
  Rows with ≥ 3 components ({sub.height} shocks, from {sub["date"].min() if sub.height else "n/a"}) are reported separately.
* **Outcomes.** Maximum drawdown from P₍t−1₎ over the next 5 and 20 days; realised vol over
  days t+1..t+20 (annualised); days until the close recovers P₍t−1₎, censored at 60.
* **Estimation.** OLS of each outcome on Φ₍t−1₎ with Newey–West (Bartlett, HAC) errors,
  because shocks cluster; the same on each component; conditional distributions by Φ
  tercile (edges from the shock sample).
* **Placebo.** {plc.height if plc is not None else 0} non-shock days (no shock in the prior 20
  days) drawn five per shock from the same Φ tercile, with the same outcome definitions.
  The placebo answers "what happens on an ordinary day with this Φ", so the shock effect
  is the difference between the two tables at each tercile.

## Conditional distributions by Φ tercile

{terc_table(terc)}

![MDD 5](figures/phi_shock_mdd_5.svg)

![MDD 20](figures/phi_shock_mdd_20.svg)

![RV 20](figures/phi_shock_rv_20.svg)

![recovery](figures/phi_shock_days_to_recover.svg)

## Slopes (Newey–West)

{reg_table(reg)}

## Verdict

**{vd["text"]}** Per outcome (shock sample, |t| ≥ 2 and the claimed sign): {", ".join(f"{OUTCOMES[k][0]}: {v}" for k, v in vd["status"].items())}.

## Reading

* **20-day drawdown after a shock:** {f(lo.get("mdd_20_mean"), "%")} in the low-Φ tercile
  against {f(hi.get("mdd_20_mean"), "%")} in the high-Φ tercile (placebo days:
  {f(plo.get("mdd_20_mean"), "%")} and {f(phi_.get("mdd_20_mean"), "%")}). Slope on Φ:
  {slope_txt("shocks", "mdd_20")}; on the ≥3-component subset {slope_txt("shocks, ≥3 components", "mdd_20")};
  on the placebo {slope_txt("placebo", "mdd_20")}.
* **5-day drawdown:** {f(lo.get("mdd_5_mean"), "%")} low Φ against {f(hi.get("mdd_5_mean"), "%")} high Φ;
  slope {slope_txt("shocks", "mdd_5")}.
* **Realised vol over the next 20 days:** {f(lo.get("rv_20_mean"), "%")} low Φ against
  {f(hi.get("rv_20_mean"), "%")} high Φ; slope {slope_txt("shocks", "rv_20")}; placebo
  {slope_txt("placebo", "rv_20")}.
* **Recovery:** median {f(lo.get("days_to_recover_median"), "d")} (low Φ) against
  {f(hi.get("days_to_recover_median"), "d")} (high Φ); recovered within 60 days
  {"n/a" if lo.get("recovered_share") is None else f"{100 * lo['recovered_share']:.0f} %"} against
  {"n/a" if hi.get("recovered_share") is None else f"{100 * hi['recovered_share']:.0f} %"}.

The regression slopes are the summary; the tercile tables are the evidence. A slope whose
sign matches the claim (more negative drawdown, higher RV, longer recovery with higher Φ) on
the shock sample and not on the placebo is what §6.2 predicts. Signs that appear on both
samples are a property of Φ's level, not of the spark. Signs that appear on neither, or only
on the placebo, are a null. The component rows say which part of Φ carries whatever is
there; the range-position component is the one whose sign was redefined on 2026-09-08 and
its early-history contribution dominates the one-component years (2018–2020), when only
the price-based component existed.

## Caveats

* Half the shock days have one or two components of Φ; the ≥3-component subset starts in
  {sub["date"].min() if sub.height else "n/a"} and holds {sub.height} shocks. Both are reported.
* The 5th-percentile shock definition selects the same episodes repeatedly (a crash is
  several shock days); Newey–West errors address serial correlation but the number of
  independent episodes is far below {ev.height}.
* The placebo matches the Φ tercile, not the market regime; ordinary days in a bear market
  also drift down.

## Reproduction

```bash
uv run monitor compute weekly                                                   # phi_shock_* tables
PYTHONPATH=src uv run --no-sync python scripts/fragility_validation_note.py     # this note and figures
```
"""
    (OUT / "fragility_validation.md").write_text(md)
    print(
        f"wrote {OUT / 'fragility_validation.md'}: {ev.height} shocks, {plc.height if plc is not None else 0} placebo days"
    )


if __name__ == "__main__":
    main()
