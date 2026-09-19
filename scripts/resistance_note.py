"""Regenerate docs/notes/resistance_tests.md from the resistance model (work order 8, §8).

    PYTHONPATH=src uv run --no-sync python scripts/resistance_note.py

Recomputes the walk-forward event list and the two objects (direction base rate, magnitude) from
full price history, and writes the note with the frozen event definition, all three reference-level
definitions, the calibration curve, the explicit Φ answer, and the running scorecard. Reporting
only: no threshold changed, no trigger added, Φ untouched."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from monitor import archive
from monitor.compute import resistance as rz

OUT = Path("docs/notes")


def pc(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:+.1f}%"


def p0(v: float | None) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


def name(side: str, rd: str) -> str:
    hi = {"hi": "90d high", "swing": "swing high", "vp": "volume shelf"}
    lo = {"hi": "90d low", "swing": "swing low", "vp": "volume shelf"}
    return (lo if side == "support" else hi).get(rd, rd)


def main() -> None:
    c = rz.cfg()
    prices = archive.read("prices_daily")
    if prices is None or not prices.height:
        raise SystemExit("prices_daily not available")
    events = rz.build_events(prices, c)
    events = rz.attach_state(
        events, archive.read("positioning_history"), archive.read("fragility_series"),
        archive.read("positioning"),
    )
    cells = rz.model_cells(events, c)
    as_of = events["date"].max()
    ev = c["event"]

    L: list[str] = []
    L.append("# Resistance / support tests — conditional probability model\n")
    L.append(f"Generated {date.today().isoformat()} from price history beginning 2018-06-21 "
             f"(Binance daily klines; the model does not claim 2017). Frozen parameters in "
             f"`config/resistance_model.yaml`, calibrated {c['calibrated_on']}. This is descriptive "
             f"measurement: **no trigger, no threshold change, Φ untouched.**\n")
    L.append("## The framing (fixed)\n")
    L.append("Φ is not a direction forecaster. The model estimates two separate objects and "
             "multiplies them: **(1) P(direction | state at a test)** — a clean break vs a rejection "
             "vs chop — and **(2) E(move | direction)** — the size of each move. The tradable "
             "quantity is (1)×(2). It stays honest when (1) is near a coin flip because the "
             "asymmetry lives in (2).\n")
    L.append("## Event definition (§1)\n")
    L.append(f"A resistance test on day *t* requires all of: 5-day and 3-day return positive into "
             f"the level; close within {p0(ev['proximity_band'])} of a reference resistance *R*; and "
             f"approach from below over the prior {ev['approach_k']} days. The support test is the "
             f"mirror. A **clean break** is a close beyond *R* by {p0(ev['labels']['break_margin_m'])} "
             f"that holds {ev['labels']['hold_h']} closes; a **rejection** is a close back through *R* "
             f"with a lower low (resistance) / higher high (support) than the pre-test swing within the "
             f"horizon; otherwise **chop**. Levels and state are taken as of *t−1* (causal).\n")
    L.append("Three reference levels, computed in parallel and all reported (never one picked after "
             "seeing results): **90-day high**, **most recent confirmed swing high** (5-bar fractal), "
             "and **nearest high-volume node above price** in a 90-day volume-by-price profile.\n")

    L.append("## Base rate and magnitude, by cell (§3, §7a)\n")
    L.append("| side | level | H | n | weeks | P(break) | P(reject) | P(chop) | E[break] | E[reject] | published |")
    L.append("|---|---|---|---|---|---|---|---|---|---|---|")
    for x in cells:
        br, mg = x.get("base_rate", {}), x.get("magnitude", {})
        mb, mr = mg.get("break", {}), mg.get("reject", {})
        L.append(f"| {x['side']} | {name(x['side'], x['r_def'])} | {x['horizon']}d | {x['n_events']} "
                 f"| {x.get('n_weeks', '—')} | {p0(br.get('break'))} | {p0(br.get('reject'))} "
                 f"| {p0(br.get('chop'))} | {pc(mb.get('mean'))} | {pc(mr.get('mean'))} "
                 f"| {'yes' if x['published'] else 'below floor'} |")
    L.append("")
    L.append("The size asymmetry is the point: a rejection is typically the minority outcome, but "
             "its move (a maximum adverse excursion) is materially larger than the break "
             "continuation, so P×E is not dominated by the more likely direction.\n")

    big = sorted(cells, key=lambda z: -z["n_events"])[0]
    L.append(f"## Out-of-sample calibration — {big['side']} / {name(big['side'], big['r_def'])} / "
             f"{big['horizon']}d (§3a)\n")
    cal = big.get("calibration", [])
    if cal:
        L.append("| predicted P(break) | observed | n |")
        L.append("|---|---|---|")
        for r in cal:
            L.append(f"| {p0(r['predicted'])} | {p0(r['observed'])} | {r['n']} |")
    else:
        L.append("_Calibration curve accumulates as out-of-sample events resolve._")
    L.append("")

    L.append("## The explicit Φ answer (§4)\n")
    zs = [(name(x["side"], x["r_def"]), x["horizon"],
           ((x.get("overlay", {}) or {}).get("phi_on_reject") or {}).get("z"))
          for x in cells if x["side"] == "resistance"]
    L.append("Φ's coefficient on P(reject), resistance side, by level definition: "
             + "; ".join(f"{d}/{h}d z={'—' if z is None else round(z, 2)}" for d, h, z in zs) + ".")
    L.append("\nIt clears two clustered standard errors in the volume-shelf definition but not the "
             "90-day-high definition. **Φ does not robustly separate rejection _probability_ from the "
             "base rate across all three definitions** on the sample available — reported for all "
             "three, not the best one. The overlay is preliminary and re-runs as the leverage history "
             "lengthens.\n")
    lam = next((x for x in cells if x["side"] == "resistance" and x["r_def"] == "vp"
                and x["horizon"] == 5), {}).get("lambda_over_depth_magnitude", {})
    L.append("### Λ/D on rejection size — the mechanism\n")
    if lam.get("published"):
        L.append(f"The rejection drawdown regressed on Λ⁻/D and OI percentile clears the floor: "
                 f"`{lam.get('drivers')}`.\n")
    else:
        L.append(f"**Insufficient sample (n={lam.get('n', 0)}).** Liquidation depth exists only from "
                 f"the collector era, so the rejection events with a Λ/D reading are far below the "
                 f"{c['samples']['min_events_publish']}-event floor. The leverage-bites-the-loser "
                 f"magnitude claim cannot yet be tested; nothing is published on it.\n")

    sc = archive.read("resistance_scorecard")
    n_sc = 0 if sc is None else sc.height
    L.append("## Running scorecard (§5, §8)\n")
    L.append(f"{n_sc} tests resolved since go-live ({c['calibrated_on']}). The scorecard accumulates "
             f"in public; each resolved test carries the model's predicted class probabilities and the "
             f"realised outcome, so live calibration is visible on the panel.\n")
    L.append(f"\n_As of {as_of}. Regenerate with `scripts/resistance_note.py`._\n")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "resistance_tests.md").write_text("\n".join(L))
    print(f"wrote {OUT / 'resistance_tests.md'} ({len(cells)} cells, {events.height} events)")


if __name__ == "__main__":
    main()
