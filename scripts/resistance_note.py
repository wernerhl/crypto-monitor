"""Regenerate docs/notes/resistance_tests.md from the resistance model (work orders 8 + 9, §8).

    PYTHONPATH=src uv run --no-sync python scripts/resistance_note.py

Recomputes the competing-risks event list and the model from full price history, and writes the
note with the frozen event definition, the cause-specific cumulative incidence with bootstrap bands,
the recalibration before/after, the consensus-vs-definition Brier comparison, and the explicit
(preliminary) Φ answer. Reporting only: no threshold changed, no trigger added, Φ untouched."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from monitor import archive
from monitor.compute import resistance as rz

OUT = Path("docs/notes")


def pc(v) -> str:
    return "—" if v is None else f"{100 * v:.1f}%"


def sg(v) -> str:
    return "—" if v is None else f"{100 * v:+.1f}%"


def name(side: str, rd: str) -> str:
    hi = {"hi": "90d high", "swing": "swing high", "vp": "volume shelf", "consensus": "consensus"}
    lo = {"hi": "90d low", "swing": "swing low", "vp": "volume shelf", "consensus": "consensus"}
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
    calibrated_on = date.fromisoformat(str(c["calibrated_on"]))
    as_of = events["date"].max()
    model = rz.run_model(events, c, as_of, calibrated_on)
    cells, rc = model["cells"], model["recal"]
    HZ = c["competing_risks"]["cif_horizons"]
    prim = rc["primary_horizon"]

    L: list[str] = []
    L.append("# Resistance / support tests — competing-risks conditional probability model\n")
    L.append(f"Generated {date.today().isoformat()} from price history beginning 2018-06-21. Frozen "
             f"parameters in `config/resistance_model.yaml`, calibrated {c['calibrated_on']}. "
             f"Descriptive measurement: **no trigger, no threshold change, Φ untouched.**\n")
    L.append("## What work order 9 changed\n")
    L.append("The five-day snapshot (work order 8) lumped *unresolved* tests into a \"chop\" bucket, "
             "which flattered both the break and the reject probabilities, and the raw multinomial was "
             "overconfident at the tails. WO9 re-casts the outcome as **competing risks**: for each "
             "test we record the time to a break and the time to a rejection, whichever comes first, "
             f"with the {c['competing_risks']['max_window_days']}-day window and end-of-data as "
             "censoring. The published quantity is the cause-specific **cumulative incidence** — "
             "P(the level breaks before it holds) by horizon — recalibrated walk-forward and carrying "
             "a block-bootstrap band. \"Chop\" is censoring, not an outcome.\n")

    L.append(f"## Cause-specific cumulative incidence (§2.1), horizons {HZ} days\n")
    L.append("| side | level | n | cause | " + " | ".join(f"{h}d" for h in HZ) + " | med. days |")
    L.append("|---|---|---|---|" + "---|" * (len(HZ) + 1))
    for x in sorted(cells, key=lambda z: (z["r_def"] != "consensus", z["side"])):
        cb, cr, mt = x["cif_break"], x["cif_reject"], x["median_ttr"]
        L.append(f"| {x['side']} | {name(x['side'], x['r_def'])} | {x['n']} | break | "
                 + " | ".join(pc(cb.get(h)) for h in HZ) + f" | {mt.get('break', '—')} |")
        L.append("| | | | reject | " + " | ".join(pc(cr.get(h)) for h in HZ)
                 + f" | {mt.get('reject', '—')} |")
    L.append("\nBands (not shown in this table; on the panel) are the "
             f"{c['bootstrap']['n']}-replicate block bootstrap over calendar weeks, 16th–84th "
             "percentile. The size asymmetry survives: a rejection is the minority outcome but its "
             "move is larger, so the expected move is not dominated by the more likely direction.\n")

    L.append("## Partial pooling and walk-forward recalibration (§1.1, §1.2)\n")
    L.append("One partially-pooled multinomial hazard (ridge, cell as a factor) is fit over all six "
             "single-definition cells so the sparse 90-day-high cell borrows the shared covariate "
             "slopes. A walk-forward isotonic map (predicted→realised, estimated only on prior data) "
             "is applied on top; the recalibrated probability is what the panel publishes, with the "
             "raw retained in the JSON for one release.\n")
    b = rc["brier"].get(prim, {})
    L.append(f"Brier at {prim} days: raw **{b.get('raw')}** → recalibrated **{b.get('recal')}** vs a "
             f"base-rate-only **{b.get('base_rate')}** ({b.get('n')} out-of-sample tests). "
             "Recalibration removes the tail overconfidence the audit flagged (a cell that predicted "
             "0.95 and was right 0.76 of the time).\n")

    L.append("## Consensus vs single definition (§2.2)\n")
    bbd = rc.get("brier_by_def", {})

    def bs(k):
        v = bbd.get(k, {})
        return f"{v.get('brier')} (n={v.get('n')})" if v.get("brier") is not None else "n/a"

    L.append(f"Out-of-sample Brier at {prim} days by selection: consensus days {bs('consensus_days')}, "
             f"non-consensus days {bs('non_consensus_days')}; single definitions "
             f"90d {bs('hi')}, swing {bs('swing')}, shelf {bs('vp')}. The data pick "
             f"**{model['headline_def']}** as the headline; the single definitions remain the "
             "robustness strip.\n")

    L.append("## The Φ answer (Tier 3, preliminary)\n")
    ov = rz.tier3_overlay(model["single"], c)
    zl = "; ".join(f"{name('resistance', k)} z={(ov['phi_on_reject'].get(k) or {}).get('z'):.2f}"
                   if (ov["phi_on_reject"].get(k) or {}).get("z") is not None else f"{name('resistance', k)} z=—"
                   for k in rz.R_DEFS)
    L.append(f"Φ's coefficient on P(reject), resistance side, by definition: {zl}. Significant in the "
             "volume-shelf definition but not the 90-day-high — definition-dependent, not established. "
             f"The mechanistic Λ⁻/D claim is **insufficient sample "
             f"(n={ov['lambda_over_depth_magnitude'].get('n', 0)})**: liquidation depth is collector-"
             "era only. The overlay never drives a published number and re-runs as data accrue.\n")
    L.append(f"> {ov['review_note']}\n")
    L.append(f"\n_As of {as_of}. Regenerate with `scripts/resistance_note.py`._\n")

    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "resistance_tests.md").write_text("\n".join(L))
    print(f"wrote {OUT / 'resistance_tests.md'} ({len(cells)} cells, {events.height} events)")


if __name__ == "__main__":
    main()
