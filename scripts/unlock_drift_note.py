"""Regenerate docs/notes/pre_unlock_drift.md and its SVG figures from the cliff-study tables
(work order D). Run after `monitor compute weekly`:

    PYTHONPATH=src uv run --no-sync python scripts/unlock_drift_note.py

Pure reporting: no threshold is read for fitting; Rule 5.1's frozen values are quoted."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from monitor import archive
from monitor.jobs_hourly import _daily_prices_by_base
from monitor.paths import CONFIG

OUT = Path("docs/notes")
FIG = OUT / "figures"


def pc(x):
    return "n/a" if x is None else f"{100 * x:.1f} %"


def pr(x):
    return "n/a" if x is None else f"{100 * x:+.1f} %"


def tbl(rows):
    out = [
        "| group | n | weeks | hit rate | s.e. (i.i.d.) | s.e. (clustered by week) | mean pre 14 d | s.e. pre (clustered) | median pre 14 d | mean pre vs BTC | mean pre β-adjusted (n) | mean post 14 d | median post 14 d |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        nb = r.get("n_beta") or 0
        out.append(
            f"| {r['group']} | {r['n']} | {r.get('n_clusters') or ''} | {pc(r['hit_rate'])} | {'' if r['se'] is None else pc(r['se'])} | {'' if r.get('se_cluster') is None else pc(r['se_cluster'])} | {pr(r['mean_pre'])} | {'' if r.get('se_pre_cluster') is None else pc(r['se_pre_cluster'])} | {pr(r['median_pre'])} | {pr(r['mean_pre_vs_btc'])} | {pr(r.get('mean_pre_beta_adj'))}{f' ({nb})' if nb else ''} | {pr(r['mean_post'])} | {pr(r['median_post'])} |"
        )
    return "\n".join(out)


def bar_svg(
    path, labels, values, errs, title, ref=None, ylab="hit rate", fmt=lambda v: f"{100 * v:.0f}%"
):
    w, h, left, bottom, top = 720, 320, 60, 70, 40
    n = len(labels)
    bw = (w - left - 20) / max(n, 1)
    ymax = max(1.0, max((v or 0) + (e or 0) for v, e in zip(values, errs, strict=True)) + 0.05)

    def y(v):
        return top + (h - top - bottom) * (1 - v / ymax)

    s = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<text x="{left}" y="22" font-size="14" font-weight="bold">{title}</text>',
    ]
    for g in (0, 0.25, 0.5, 0.75, 1.0):
        s.append(
            f'<line x1="{left}" x2="{w - 20}" y1="{y(g):.1f}" y2="{y(g):.1f}" stroke="#ddd"/><text x="{left - 6}" y="{y(g) + 4:.1f}" text-anchor="end" fill="#555">{fmt(g)}</text>'
        )
    if ref is not None:
        s.append(
            f'<line x1="{left}" x2="{w - 20}" y1="{y(ref):.1f}" y2="{y(ref):.1f}" stroke="#c0392b" stroke-dasharray="6,4"/><text x="{w - 22}" y="{y(ref) - 4:.1f}" text-anchor="end" fill="#c0392b">base rate {fmt(ref)}</text>'
        )
    for i, (lab, v, e) in enumerate(zip(labels, values, errs, strict=True)):
        if v is None:
            continue
        x0 = left + i * bw + bw * 0.15
        s.append(
            f'<rect x="{x0:.1f}" y="{y(v):.1f}" width="{bw * 0.7:.1f}" height="{y(0) - y(v):.1f}" fill="#2c6fb0"/>'
        )
        if e:
            s.append(
                f'<line x1="{x0 + bw * 0.35:.1f}" x2="{x0 + bw * 0.35:.1f}" y1="{y(min(v + e, ymax)):.1f}" y2="{y(max(v - e, 0)):.1f}" stroke="#222"/>'
            )
        s.append(
            f'<text x="{x0 + bw * 0.35:.1f}" y="{y(v) - 4:.1f}" text-anchor="middle">{fmt(v)}</text>'
        )
        s.append(
            f'<text x="{x0 + bw * 0.35:.1f}" y="{h - bottom + 16}" text-anchor="middle">{lab}</text>'
        )
    s.append(
        f'<text x="{left}" y="{h - 8}" fill="#555">{ylab}; whiskers ± one standard error; n per group in the table</text></svg>'
    )
    Path(path).write_text("\n".join(s))


def path_svg(path, series, title):
    w, h, left, bottom, top = 720, 340, 60, 50, 40
    allv = [v for _, ys in series for v in ys if v is not None]
    lo, hi = min([*allv, 0]) - 0.005, max([*allv, 0]) + 0.005

    def x(i):
        return left + (w - left - 20) * (i / 28)

    def y(v):
        return top + (h - top - bottom) * (1 - (v - lo) / (hi - lo))

    cols = ["#2c6fb0", "#c0392b", "#27ae60", "#8e44ad", "#e67e22", "#7f8c8d"]
    s = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" font-family="Helvetica, Arial, sans-serif" font-size="12">',
        f'<rect width="{w}" height="{h}" fill="white"/>',
        f'<text x="{left}" y="22" font-size="14" font-weight="bold">{title}</text>',
    ]
    for g in np.linspace(lo, hi, 6):
        s.append(
            f'<line x1="{left}" x2="{w - 20}" y1="{y(g):.1f}" y2="{y(g):.1f}" stroke="#eee"/><text x="{left - 6}" y="{y(g) + 4:.1f}" text-anchor="end" fill="#555">{100 * g:+.1f}%</text>'
        )
    s.append(f'<line x1="{left}" x2="{w - 20}" y1="{y(0):.1f}" y2="{y(0):.1f}" stroke="#999"/>')
    s.append(
        f'<line x1="{x(14):.1f}" x2="{x(14):.1f}" y1="{top}" y2="{h - bottom}" stroke="#c0392b" stroke-dasharray="4,3"/><text x="{x(14) + 4:.1f}" y="{top + 12}" fill="#c0392b">cliff</text>'
    )
    for k, (lab, ys) in enumerate(series):
        pts = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(ys) if v is not None)
        s.append(f'<polyline points="{pts}" fill="none" stroke="{cols[k % 6]}" stroke-width="2"/>')
        s.append(f'<text x="{left + 8 + k * 170}" y="{h - 14}" fill="{cols[k % 6]}">— {lab}</text>')
    for d in (-14, -7, 0, 7, 14):
        s.append(
            f'<text x="{x(d + 14):.1f}" y="{h - bottom + 16}" text-anchor="middle">{d:+d} d</text>'
        )
    s.append("</svg>")
    Path(path).write_text("\n".join(s))


def main() -> None:
    FIG.mkdir(parents=True, exist_ok=True)
    ev = archive.read("cliff_study_events")
    cs = archive.read("cliff_study")
    if ev is None or cs is None or not ev.height:
        raise SystemExit("run `monitor compute weekly` first: cliff_study tables are empty")
    ev = ev.filter(pl.col("as_of") == ev["as_of"].max())
    cs = cs.filter(pl.col("as_of") == cs["as_of"].max())
    as_of = str(cs["as_of"][0])
    th = yaml.safe_load((CONFIG / "thresholds.yaml").read_text())["rules"]["cliff"]
    px = _daily_prices_by_base()
    cl = {}
    for b, g in px.sort("date").group_by("base"):
        cl[b[0] if isinstance(b, tuple) else b] = (
            g["date"].cast(pl.Int32).to_numpy(),
            g["close"].to_numpy(),
        )

    def path_for(rows):
        acc, cnt = np.zeros(29), np.zeros(29)
        for r in rows:
            s_ = cl.get(r["base"])
            if s_ is None:
                continue
            d0 = (r["date"] - date(1970, 1, 1)).days
            idx = np.searchsorted(s_[0], d0, side="right") - 1
            if idx < 14 or idx + 14 >= len(s_[0]) or s_[0][idx] != d0:
                continue
            seg = np.log(s_[1][idx - 14 : idx + 15] / s_[1][idx - 14])
            if np.all(np.isfinite(seg)):
                acc += seg
                cnt += 1
        return [float(a / c) if c else None for a, c in zip(acc, cnt, strict=True)], int(cnt[0])

    meets = (pl.col("share_of_float") > th["single_unlock_float_share_min"]) | (
        pl.col("days_of_volume") > th["single_unlock_days_of_volume_min"]
    )
    rule = ev.filter(meets)
    below = ev.filter(
        ~meets & pl.col("share_of_float").is_not_null() & pl.col("days_of_volume").is_not_null()
    )
    series = []
    for lab, sub in [("Rule 5.1 cliffs", rule), ("below threshold", below), ("all cliffs", ev)]:
        p_, n_ = path_for(sub.to_dicts())
        series.append((f"{lab} (n={n_})", p_))
    path_svg(
        FIG / "unlock_event_paths.svg",
        series,
        "Average cumulative log return around scheduled cliffs, 2021–2026",
    )

    def g(k):
        return cs.filter(pl.col("group_kind") == k).to_dicts()

    br = cs.filter(pl.col("group_kind") == "base rate")["hit_rate"][0]
    cls = cs.filter(pl.col("group_kind") == "recipient class").sort("n", descending=True)
    bar_svg(
        FIG / "unlock_hit_by_class.svg",
        cls["group"].to_list(),
        cls["hit_rate"].to_list(),
        cls["se"].to_list(),
        "Share of cliffs with a negative 14-day pre-cliff return, by dominant recipient class",
        ref=br,
    )
    for kind, order, fname, title in [
        (
            "share of float",
            ["< 0.5 %", "0.5–1 %", "1–2 %", "2–5 %", "> 5 %"],
            "unlock_hit_by_share.svg",
            "Hit rate by unlock size as a share of float",
        ),
        (
            "days of volume",
            ["< 1 d", "1–2 d", "2–5 d", "> 5 d"],
            "unlock_hit_by_dov.svg",
            "Hit rate by unlock size in days of average volume",
        ),
    ]:
        sub = (
            cs.filter(pl.col("group_kind") == kind)
            .with_columns(
                pl.col("group")
                .replace_strict({k: i for i, k in enumerate(order)}, default=99)
                .alias("o")
            )
            .sort("o")
        )
        bar_svg(
            FIG / fname,
            sub["group"].to_list(),
            sub["hit_rate"].to_list(),
            sub["se"].to_list(),
            title,
            ref=br,
        )
    yr = cs.filter(pl.col("group_kind") == "year").sort("group")
    bar_svg(
        FIG / "unlock_hit_by_year.svg",
        yr["group"].to_list(),
        yr["hit_rate"].to_list(),
        yr["se"].to_list(),
        "Hit rate by year of the cliff",
        ref=br,
    )
    bar_svg(
        FIG / "unlock_pre_vs_btc_by_class.svg",
        cls["group"].to_list(),
        [abs(v) if v is not None else None for v in cls["mean_pre_vs_btc"].to_list()],
        [None] * cls.height,
        "Mean 14-day pre-cliff return relative to BTC, by class (magnitude; sign in the table)",
        ylab="|mean pre-cliff return vs BTC|",
        fmt=lambda v: f"{100 * v:.1f}%",
    )

    allr = {r["group"]: r for r in g("all")}
    ruler = {r["group"]: r for r in g("rule")}
    rule_row = next(r for r in ruler if r.startswith("Rule 5.1"))
    rb = next((ruler[r] for r in ruler if r.startswith("both legs")), {})
    n_float = int(ev["float_basis"].is_not_null().sum())
    n_wash = int((ev["adv_basis"] == "wash-filtered venues, current pass set").sum())
    n_unf = int((ev["adv_basis"] == "exchange klines, unfiltered").sum())
    by_year = {r["group"]: r for r in g("year")}
    a_all = allr["all cliffs with price coverage"]
    rr = ruler[rule_row]
    by_year = {r["group"]: r for r in g("year")}
    y25, y26 = by_year.get("2025", {}), by_year.get("2026", {})
    plc = {r["group"]: r for r in g("placebo")}
    plc_year = {r["group"]: r for r in g("placebo year")}
    p25, p26 = plc_year.get("2025", {}), plc_year.get("2026", {})

    def _diff(cy: dict, py: dict) -> tuple[float, float]:
        """β-adjusted cliff minus placebo mean, in points, and its size in clustered s.e.
        (pre-return s.e. of each row combined in quadrature)."""
        a_, b_ = cy.get("mean_pre_beta_adj"), py.get("mean_pre_beta_adj")
        if a_ is None or b_ is None:
            return float("nan"), float("nan")
        se = (
            (cy.get("se_pre_cluster") or 0.0) ** 2 + (py.get("se_pre_cluster") or 0.0) ** 2
        ) ** 0.5
        return 100 * (a_ - b_), abs(a_ - b_) / se if se else float("nan")

    d25, r25 = _diff(y25, p25)
    d26, r26 = _diff(y26, p26)
    plc_all = plc.get("pseudo-cliffs: 20 non-cliff days per token and year", {})
    plc_rule = plc.get("pseudo-cliffs on the Rule 5.1 tokens only", {})
    brs = {r["group"]: r for r in g("base rate")}
    br_sub = brs.get("all days, Rule 5.1 tokens only", {})
    md = f"""# Pre-unlock drift: what the schedule says, 2021–2026

*Research note for work-order item D, revised under work order 2 (item 3). Generated {as_of}
from `cliff_study_events` and `cliff_study` (`monitor.compute.cliff_study`, weekly job) by
`scripts/unlock_drift_note.py`. Nothing here changes a threshold; Rule 5.1 keeps
`single_unlock_float_share_min = {th["single_unlock_float_share_min"]:.0%}` and
`single_unlock_days_of_volume_min = {th["single_unlock_days_of_volume_min"]:g}`.*

## The result, year by year first

{tbl(g("year"))}

![hit rate by year](figures/unlock_hit_by_year.svg)

Placebo dates of the same tokens, by year (the row a cliff year should be read against):

{tbl(g("placebo year"))}

The pooled numbers below are carried by 2025, and the placebo says most of that year's drift
was not cliff-specific. In 2025 (n = {y25.get("n", 0)}) a cliff was preceded by a negative
fourteen-day return {pc(y25.get("hit_rate"))} of the time (clustered s.e.
{pc(y25.get("se_cluster"))}), with a mean pre-cliff return of {pr(y25.get("mean_pre"))} and
{pr(y25.get("mean_pre_vs_btc"))} relative to BTC; the placebo dates of the same tokens in 2025
read {pc(p25.get("hit_rate"))} and {pr(p25.get("mean_pre"))}: the tokens that had cliffs were
falling on ordinary days too. In 2026 (n = {y26.get("n", 0)}) the cliff hit rate is
{pc(y26.get("hit_rate"))} (clustered s.e. {pc(y26.get("se_cluster"))}), at the base rate, and
the BTC-relative drift is {pr(y26.get("mean_pre_vs_btc"))}; by those two measures the drift has
faded in the most recent year, which is what one expects of a public schedule being
arbitraged. The β-adjusted column tells a more cautious story: cliffs read
{pr(y25.get("mean_pre_beta_adj"))} against {pr(p25.get("mean_pre_beta_adj"))} on the placebo in
2025 ({d25:+.1f} points, about {r25:.1f} clustered standard errors) and
{pr(y26.get("mean_pre_beta_adj"))} against {pr(p26.get("mean_pre_beta_adj"))} in 2026
({d26:+.1f} points, about {r26:.1f} standard errors). A residual of that size on rolling
26-week betas of small tokens is suggestive, not established. Either way Rule 5.1 stays
informational: the thresholds are kept, the trade table carries the caveat, and the cliff
calendar is one rolling list rather than a trigger per token.

## Question

Rule 5.1 (notes §5) shorts a token two to four weeks ahead of a scheduled cliff that is large
relative to the float and to real volume, on the argument that recipients who can hedge do so
before the date and that the market front-runs the supply. This note asks the schedule
itself: over every cliff DefiLlama records since 2021 with price coverage, did the token
drift down over the fourteen days before the date, does the size of the event or the class
of the recipient change the answer, and does the effect survive a placebo?

## Data and definitions

* **Events.** {ev.height} scheduled cliffs (rows of `unlock_events` with `kind = cliff`,
  positive amount, grouped by token and date) between 2021-01-01 and {as_of} minus 14 days,
  for tokens with daily exchange klines on Binance, OKX, Bybit, Coinbase or Kraken. The
  schedule is heavily weighted to 2025–26 ({y25.get("n", 0)} and {y26.get("n", 0)} events)
  because DefiLlama's emissions coverage grew with the launches of that period; 2021–23 hold
  {sum(by_year.get(y, {}).get("n", 0) for y in ("2021", "2022", "2023"))} events.
* **Hit.** Negative log return over the fourteen days ending on the cliff date (the same
  definition as the live hit-rate table). Post-return is the fourteen days after.
* **Standard errors.** Two are reported: the i.i.d. binomial one, and one clustered by cliff
  week (cliffs bunch on the 1st and 15th and share the market factor). The clustered one is
  the one to read.
* **Market adjustment.** Two columns: the return relative to BTC, and a β-adjusted return
  using the factor model's rolling β_MKT for the token as of the cliff date (weekly
  estimate, latest on or before the date) times BTC's fourteen-day return. Small tokens
  have β above one, so the BTC-relative column overstates the effect in a falling market.
  Tokens without a β estimate are excluded from that column (n shown).
* **Placebo.** For each token and year with a cliff, twenty pseudo-cliff dates drawn
  uniformly from that token's non-cliff days, with the same windows and definitions. The
  conditional effect is the cliff row minus the placebo row.
* **Size.** Share of float = tokens unlocked ÷ float at the cliff date, where float is backed
  out of today's circulating supply by removing every later scheduled cliff and the current
  linear rate times the elapsed days ({n_float} of {ev.height} events; burns and re-issuance
  are ignored, so shares for old events are approximate). Days of volume = USD value at the
  cliff ÷ average daily quote volume over the thirty days ending fifteen days before the
  date, on venues passing the *current* wash filters where a wash row exists ({n_wash}
  events) and on every venue otherwise ({n_unf} events, flagged `unfiltered`).
* **Base rates.** The unconditional share of negative fourteen-day returns over the same
  period, once on all tokens with cliffs ({pc(br)}) and once on the tokens that form the
  Rule 5.1 subset ({pc(br_sub.get("hit_rate"))}, n = {br_sub.get("n", 0)} token-days).
  Low-float tokens have a higher unconditional share of down fortnights, so the subset base
  rate is the one the Rule 5.1 row should be read against.

## Results

### Headline, placebo and base rates

{tbl(g("all") + g("rule") + g("placebo") + g("base rate"))}

Read: the rule as implemented fires on either leg. Those cliffs (n = {rr["n"]}) were preceded
by a negative return {pc(rr["hit_rate"])} of the time (clustered s.e. {pc(rr.get("se_cluster"))}),
against {pc(plc_rule.get("hit_rate"))} on the placebo dates of the same tokens (clustered s.e.
{pc(plc_rule.get("se_cluster"))}) and a subset base rate of {pc(br_sub.get("hit_rate"))}; their
mean pre-cliff return is {pr(rr["mean_pre"])} ({pr(rr["mean_pre_vs_btc"])} vs BTC,
{pr(rr.get("mean_pre_beta_adj"))} β-adjusted on {rr.get("n_beta", 0)} events) against
{pr(plc_rule.get("mean_pre"))} on the placebo. The intersection of both legs (n = {rb.get("n", 0)})
reads {pc(rb.get("hit_rate"))} (clustered s.e. {pc(rb.get("se_cluster"))}), mean
{pr(rb.get("mean_pre"))}, {pr(rb.get("mean_pre_vs_btc"))} vs BTC. The conditional effect is the
difference between a cliff row and its placebo row, and its uncertainty is the clustered
standard error, not the i.i.d. one.

### By recipient class (dominant class by amount)

{tbl(g("recipient class"))}

![hit rate by class](figures/unlock_hit_by_class.svg)

![pre-cliff return vs BTC by class](figures/unlock_pre_vs_btc_by_class.svg)

### By size

{tbl(g("share of float"))}

![hit rate by share of float](figures/unlock_hit_by_share.svg)

{tbl(g("days of volume"))}

![hit rate by days of volume](figures/unlock_hit_by_dov.svg)

### Event-time paths

Average cumulative log return from fourteen days before to fourteen days after the cliff
(events with a full 29-day price window):

![event paths](figures/unlock_event_paths.svg)

## What the numbers support, and what they do not

1. **The drift is a 2025 phenomenon in this sample.** Pooled: hit rate
   {pc(a_all["hit_rate"])} against a base rate of {pc(br)}, mean pre-cliff return
   {pr(a_all["mean_pre"])}, {pr(a_all["mean_pre_vs_btc"])} vs BTC and
   {pr(a_all.get("mean_pre_beta_adj"))} β-adjusted. By year the effect sits in 2025 and is
   absent in 2026; with clustered standard errors of a few points per year, the 2026 reading
   is not distinguishable from the base rate.
2. **The placebo removes part of the pooled effect.** Placebo dates on the same tokens and
   years show a hit rate of {pc(plc_all.get("hit_rate"))} and a mean pre-window return of
   {pr(plc_all.get("mean_pre"))}: the tokens with cliffs were falling on ordinary days too.
   What survives is the difference, concentrated in the larger events.
3. **The rule's own set shows no drift; only the intersection does.** Cliffs meeting either
   leg (the rule as implemented, n = {rr["n"]}) have a hit rate of {pc(rr["hit_rate"])} and a mean
   pre-cliff return of {pr(rr["mean_pre"])}: at the subset base rate. Cliffs meeting both legs
   (n = {rb.get("n", 0)}) read {pc(rb.get("hit_rate"))} and {pr(rb.get("mean_pre"))}. Whatever
   size effect exists sits in the intersection, and with clustered standard errors of three to
   five points on those rows it is suggestive, not established. Within the size buckets the
   pattern is not monotone.
4. **Recipient class matters in the direction the notes assume**, with investor-dominated
   cliffs the most negative before and after the date; the `unknown` class is at the base
   rate, as a label without information should be. `public` has nine events: ignore.
5. **The post-cliff return is not a mirror image.** Medians are negative in most groups, so
   the supply is not fully priced by the date, but the means are pulled up by rebounds; a
   short held through the date is a different trade from the one Rule 5.1 describes.

## Implications for the rule

* Thresholds unchanged. The study supports the sign of the rule in 2025 and not in 2026; a
  threshold fitted to this sample would be fitted to one year.
* Rule 5.1 is informational on the dashboard: the unlock-short rows carry the caveat "2026
  pre-cliff drift ≈ base rate", and the alerts carry one rolling cliff calendar rather than an
  issue per token.
* The float approximation is the weakest input; the per-protocol DefiLlama detail could
  replace it for the tokens it covers. Days of volume should be read with `adv_basis`.

## Reproduction

```bash
uv run monitor compute weekly                                          # rebuilds the cliff_study tables
PYTHONPATH=src uv run --no-sync python scripts/unlock_drift_note.py    # this note and its figures
```
"""
    (OUT / "pre_unlock_drift.md").write_text(md)
    print(
        f"wrote {OUT / 'pre_unlock_drift.md'}: {ev.height} events, rule n = {rr['n']}, base rate {br:.3f}"
    )


if __name__ == "__main__":
    main()
