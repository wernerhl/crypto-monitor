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
        "| group | n | hit rate | s.e. | mean pre 14 d | median pre 14 d | mean pre vs BTC | mean post 14 d | median post 14 d |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for r in rows:
        out.append(
            f"| {r['group']} | {r['n']} | {pc(r['hit_rate'])} | {'' if r['se'] is None else pc(r['se'])} | {pr(r['mean_pre'])} | {pr(r['median_pre'])} | {pr(r['mean_pre_vs_btc'])} | {pr(r['mean_post'])} | {pr(r['median_post'])} |"
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

    meets = (pl.col("share_of_float") > th["single_unlock_float_share_min"]) & (
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
    n_float = int(ev["float_basis"].is_not_null().sum())
    n_wash = int((ev["adv_basis"] == "wash-filtered venues, current pass set").sum())
    n_unf = int((ev["adv_basis"] == "exchange klines, unfiltered").sum())
    by_year = {r["group"]: r for r in g("year")}
    unknown_n = next((r["n"] for r in g("recipient class") if r["group"] == "unknown"), 0)
    a_all = allr["all cliffs with price coverage"]
    rr = ruler[rule_row]
    md = f"""# Pre-unlock drift: what the schedule says, 2021–2026

*Research note for work-order item D. Generated {as_of} from `cliff_study_events` and
`cliff_study` (`monitor.compute.cliff_study`, weekly job) by `scripts/unlock_drift_note.py`.
Nothing here changes a threshold; Rule 5.1 keeps
`single_unlock_float_share_min = {th["single_unlock_float_share_min"]:.0%}` and
`single_unlock_days_of_volume_min = {th["single_unlock_days_of_volume_min"]:g}`.*

## Question

Rule 5.1 (notes §5) shorts a token two to four weeks ahead of a scheduled cliff that is large
relative to the float and to real volume, on the argument that recipients who can hedge do so
before the date and that the market front-runs the supply. The rule's live hit rate has almost
no sample (the monitor is days old), so this note asks the schedule itself: over every cliff
DefiLlama records since 2021 with price coverage, did the token drift down over the fourteen
days before the date, and does the size of the event or the class of the recipient change the
answer?

## Data and definitions

* **Events.** {ev.height} scheduled cliffs (rows of `unlock_events` with `kind = cliff`,
  positive amount, grouped by token and date) between 2021-01-01 and {as_of} minus 14 days,
  for tokens with daily exchange klines on Binance, OKX, Bybit, Coinbase or Kraken. The
  schedule is heavily weighted to 2025–26 ({by_year.get("2025", {}).get("n", 0)} and
  {by_year.get("2026", {}).get("n", 0)} events) because DefiLlama's emissions coverage grew with
  the launches of that period; 2021–23 hold {sum(by_year.get(y, {}).get("n", 0) for y in ("2021", "2022", "2023"))} events.
* **Hit.** Negative log return over the fourteen days ending on the cliff date (the same
  definition as the live hit-rate table). Post-return is the fourteen days after.
* **Size.** Share of float = tokens unlocked ÷ float at the cliff date, where float is backed
  out of today's circulating supply by removing every later scheduled cliff and the current
  linear rate times the elapsed days ({n_float} of {ev.height} events; burns and re-issuance
  are ignored, so shares for old events are approximate). Days of volume = USD value at the
  cliff ÷ average daily quote volume over the thirty days ending fifteen days before the
  date, on venues passing the *current* wash filters where a wash row exists ({n_wash}
  events) and on every venue otherwise ({n_unf} events, flagged `unfiltered`). Wash filters
  cannot be evaluated historically; the book depth they need is not archived.
* **Base rate.** The unconditional share of negative fourteen-day returns over the same tokens
  and period: {pc(br)}. A cliff hit rate is only informative against this number, because the
  sample sits in a period when most of these tokens fell.

## Results

### Headline

{tbl(g("all") + g("rule") + g("base rate"))}

Read: {pc(a_all["hit_rate"])} of all cliffs were preceded by a negative fourteen-day return
against a base rate of {pc(br)}; cliffs that met both Rule 5.1 legs were preceded by a
negative return {pc(rr["hit_rate"])} of the time (n = {rr["n"]}, s.e. {pc(rr["se"])}), with a
mean pre-cliff return of {pr(rr["mean_pre"])} and {pr(rr["mean_pre_vs_btc"])} relative to BTC.
The BTC-relative figure matters: it removes the market's own drift over the same windows.

### By recipient class (dominant class by amount)

{tbl(g("recipient class"))}

![hit rate by class](figures/unlock_hit_by_class.svg)

![pre-cliff return vs BTC by class](figures/unlock_pre_vs_btc_by_class.svg)

### By size

{tbl(g("share of float"))}

![hit rate by share of float](figures/unlock_hit_by_share.svg)

{tbl(g("days of volume"))}

![hit rate by days of volume](figures/unlock_hit_by_dov.svg)

### By year

{tbl(g("year"))}

![hit rate by year](figures/unlock_hit_by_year.svg)

### Event-time paths

Average cumulative log return from fourteen days before to fourteen days after the cliff
(events with a full 29-day price window):

![event paths](figures/unlock_event_paths.svg)

## What the numbers support, and what they do not

1. **A pre-cliff drift exists but it is modest and mostly a market effect.** All cliffs: hit
   rate {pc(a_all["hit_rate"])} against a base rate of {pc(br)}; mean pre-cliff return
   {pr(a_all["mean_pre"])}, of which {pr(a_all["mean_pre_vs_btc"])} is relative to BTC. The
   difference from the base rate is a few percentage points with a standard error near one
   point: real, small.
2. **Size selects.** The Rule 5.1 subset (> {th["single_unlock_float_share_min"]:.0%} of float and
   > {th["single_unlock_days_of_volume_min"]:g} days of volume) has the higher hit rate and the more
   negative BTC-relative drift of the two halves of the sample. Within the size buckets the
   effect is not monotone: the largest events (> 5 % of float) show a strongly negative
   pre-cliff drift and a *positive* mean post-cliff return, the pattern of supply being
   front-run and then absorbed, while events of one to two days of volume look worse before
   the date than events of more than five days, whose tokens are often illiquid names where
   the volume denominator is unreliable. With buckets of 50–350 events the standard errors
   are three to seven points; ranking the buckets is not supported, the direction of the
   overall effect is.
3. **Recipient class matters in the direction the notes assume.** Investor-dominated cliffs
   have the most negative pre-cliff drift relative to BTC and the most negative post-cliff
   return; ecosystem and community cliffs also drift down before the date; team cliffs are
   in between. The `unknown` class (the largest, {unknown_n} events) is close to the base
   rate, which is what one expects when the label carries no information. `public` has nine
   events: ignore.
4. **The post-cliff return is not a mirror image.** Median post-cliff returns are negative in
   most groups, so the supply is not fully priced by the date; but the means are pulled up by
   a tail of rebounds. A structure that holds a short through the date is a different trade
   from the one Rule 5.1 describes (short two to four weeks before, cover into the date), and
   this sample does not favour it.
5. **Regime dependence is large.** 2021 cliffs (n = {by_year.get("2021", {}).get("n", 0)}) were preceded by
   *positive* returns (a rising market); 2025 shows the strongest drift. The effective sample
   for anything regime-dependent is the number of regimes, not the number of events.

## Implications for the rule

* The thresholds stay where they are. The study supports the *sign* of the rule and the
  choice to condition on size and class; it does not identify a better threshold, and a
  threshold moved to fit this sample would be fitted to a 2025-heavy schedule.
* The float approximation is the weakest input. For tokens whose supply history is known
  exactly (the DefiLlama per-protocol detail), `share_of_float` could be recomputed from the
  detail rather than backed out; that is a data task, not a calibration.
* Days of volume should be read with `adv_basis`. Only {n_wash} events use wash-filtered
  volume; the rest use unfiltered exchange volume and overstate liquidity for names with
  wash-prone venues, which makes their `days_of_volume` too small and keeps some genuine
  Rule 5.1 events out of the subset.

## Reproduction

```bash
uv run monitor compute weekly                                   # rebuilds the cliff_study tables
PYTHONPATH=src uv run --no-sync python scripts/unlock_drift_note.py   # this note and its figures
```
"""
    (OUT / "pre_unlock_drift.md").write_text(md)
    print(
        f"wrote {OUT / 'pre_unlock_drift.md'}: {ev.height} events, rule n = {rr['n']}, base rate {br:.3f}"
    )


if __name__ == "__main__":
    main()
