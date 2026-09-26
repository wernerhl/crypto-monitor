"""The daily state reading (work order C1): one deterministic paragraph built from the
numbers on the page, regenerated with the hourly job. No adjectives that are not implied by a
threshold or a sign; every number is a segment that links to the panel it came from.

Segments are dicts {"t": text} or {"t": text, "href": "#panel-id"}; a segment with
"cls": "alert" is rendered in the alert style (a missing fragility component). The page joins
them."""

from __future__ import annotations

import contextlib
import json
from datetime import date

COMPONENT_NAMES = {
    "z_fr": "funding",
    "z_oi": "open interest to cap",
    "z_vrp_neg": "the negative of the variance risk premium",
    "z_dd": "the 90-day range position",
    "z_sc_neg": "the negative of stablecoin growth",
}


def _ordinal(n: int) -> str:
    suf = "th" if 11 <= n % 100 <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suf}"


def _n(x: float | None, fmt: str) -> str:
    return "n/a" if x is None else format(x, fmt)


def _pct(x: float | None, digits: int = 1) -> str:
    return "n/a" if x is None else f"{100 * x:+.{digits}f}%"


def state_reading(
    as_of: date,
    frag: dict | None,
    options: list[dict],
    basis_term: list[dict],
    positioning_btc: dict | None,
    dd_90: float | None,
    dd_cycle: float | None,
    sc_growth_30d: float | None,
    rules: list[dict],
    venue_breaches: list[str],
    low_score_breach: bool,
    gaps: list[dict] | None = None,
    calendar: list[dict] | None = None,
    trend_btc: dict | None = None,
    trend_continuation: dict | None = None,
    active_tests: list[dict] | None = None,
    conflicts: list[dict] | None = None,
    demand: dict | None = None,
) -> list[dict]:
    seg: list[dict] = []
    add = seg.append
    # --- fragility and its two largest components
    if frag and frag.get("phi") is not None:
        phi = frag["phi"]
        word = (
            "levered" if phi > 1 else "neutral" if phi > 0 else "deleveraged"
        )  # descriptive (review decision 3)
        add({"t": f"{as_of.isoformat()}: the positioning summary Φ is "})
        add({"t": f"{phi:+.2f}", "href": "#p-state"})
        add({"t": f" ({word}, {frag.get('n_components') or 0} of 5 components)"})
        if phi > 1:  # WO10 §4: a reader must not infer bearishness from "levered"
            add({"t": "; ", })
            add({"t": "Φ carries no directional information (see validation)",
                 "cls": "small", "href": "#p-state"})
        for g in gaps or []:
            add({"t": "; "})
            add(
                {
                    "t": f"component {COMPONENT_NAMES.get(g['component'], g['component'])} unavailable ({g['reason']})",
                    "cls": "alert",
                    "href": "#p-state",
                }
            )
        comps = [(k, frag.get(k)) for k in COMPONENT_NAMES if frag.get(k) is not None]
        comps.sort(key=lambda kv: -abs(kv[1]))
        if comps:
            parts = []
            for k, z in comps[:2]:
                parts.append(f"{COMPONENT_NAMES[k]} at z = {z:+.2f}")
            add({"t": "; the largest components are " + " and ".join(parts) + ". "})
        else:
            add({"t": ". "})
    else:
        add({"t": f"{as_of.isoformat()}: the fragility index is not available this run. "})
    # --- variance risk premium signs
    vr = [(o["currency"], o.get("vrp")) for o in options if o.get("currency") in ("BTC", "ETH")]
    drivers = {}
    for r in rules:
        if r.get("rule_id") == "4.3":
            with contextlib.suppress(ValueError, TypeError):
                drivers[r["asset"]] = json.loads(r.get("inputs") or "{}").get("driver_text")
    if vr:
        bits = []
        for c, v in vr:
            if v is None:
                bits.append(f"{c} VRP n/a")
            else:
                dt = f": {drivers[c]}" if v < 0 and drivers.get(c) else ""
                bits.append(f"{c} VRP {'positive' if v > 0 else 'negative'} ({v:+.3f}{dt})")
        add({"t": "Implied minus realised variance: "})
        add({"t": ", ".join(bits), "href": "#p-state"})
        add({"t": ". "})
        # WO10 §3 — convexity is two-sided: cheap implied is cheap in BOTH directions, and the trend
        # state names which side is the cheap one. A reading, not a trade rule.
        btc_vrp = next((v for c, v in vr if c == "BTC" and v is not None), None)
        if btc_vrp is not None and btc_vrp < 0:
            st = (trend_btc or {}).get("state")
            cheap = ("upside (call) convexity" if st == "UPTREND"
                     else "downside (put) convexity" if st == "DOWNTREND"
                     else "convexity on either side")
            add({"t": "With implied below realised, options are cheap in both directions; "})
            add({"t": f"in a BTC {st or 'RANGE'} the cheap side is {cheap}", "href": "#p-state"})
            add({"t": " (a reading, not a trade). "})
    # --- front basis sign
    front = [r for r in basis_term if r.get("base") == "BTC" and r.get("basis_ann") is not None]
    if front:
        f = min(front, key=lambda r: r["days"])
        add({"t": "The front BTC dated future is in "})
        add(
            {
                "t": f"{'contango' if f['basis_ann'] >= 0 else 'backwardation'} ({_pct(f['basis_ann'])} annualised, {f['venue']}, {f['days']:.0f} days)",
                "href": "#p-state",
            }
        )
        add({"t": ". "})
    # --- open interest percentile and drawdowns
    if positioning_btc:
        p = positioning_btc.get("oi_rel_pctile")
        if p is not None:
            add({"t": "BTC open interest to market cap sits at the "})
            add({"t": f"{_ordinal(round(100 * p))} percentile", "href": "#p-triggers"})
            add({"t": f" of its 250-day range (n = {positioning_btc.get('oi_rel_pctile_n') or 0})"})
            # WO10 §1: the leverage reading is conditional on the trend state
            st = (trend_btc or {}).get("state")
            if st:
                cb = ((trend_continuation or {}).get("by_state") or {}).get(st) or {}
                rate = cb.get("p_up")
                tail = (
                    f", continuation base rate {100 * rate:.0f}%" if rate is not None else ""
                )
                add({"t": f" — read in a BTC {st}{tail} (Φ scales the size of a losing move, not its direction). "})
            else:
                add({"t": ". "})
    if dd_90 is not None or dd_cycle is not None:
        add({"t": "On the daily close, BTC is "})  # WO10 §5: every price statement says close/intraday
        add({"t": f"{_pct(dd_90)} from its 90-day high", "href": "#p-state"})
        add({"t": f" and {_pct(dd_cycle)} from its cycle high (daily closes; a level is cleared on a close only). "})
    # --- stablecoin growth
    if sc_growth_30d is not None:
        add({"t": "Stablecoin supply grew "})
        add({"t": _pct(sc_growth_30d), "href": "#p-state"})
        add({"t": " over 30 days. "})
    # --- WO10 §2: demand-side flows (the buyer the stablecoin proxy can miss)
    if demand:
        cp = next((p for p in (demand.get("coinbase_premium") or []) if p.get("base") == "BTC"), None)
        nf = next((f for f in (demand.get("exchange_net_flow") or []) if f.get("base") == "BTC"), None)
        bits = []
        if cp and cp.get("premium_z") is not None:
            z = cp["premium_z"]
            bits.append(f"BTC Coinbase premium at z {z:+.1f} ({'US spot bid leading' if z > 1 else 'US spot lagging' if z < -1 else 'in line'})")
        if nf and nf.get("net_flow_7d_usd") is not None:
            v = nf["net_flow_7d_usd"]
            bits.append(f"exchange net {'outflow' if v > 0 else 'inflow'} {abs(v) / 1e9:.1f}B over 7d ({'accumulation' if v > 0 else 'sell-side inventory building'})")
        if bits:
            add({"t": "Demand: "})
            add({"t": "; ".join(bits), "href": "#p-state"})
            add({"t": ". "})
    # --- rules and venues
    firing = [
        r
        for r in rules
        if r.get("fired") is True
        and r.get("rule_id") not in ("4.3", "5.1")
        and r.get("status", "trigger") == "trigger"
    ]
    cliffs = [r for r in (calendar or []) if r.get("fired") is True]
    unavailable = [r for r in rules if r.get("fired") is None]
    if firing:
        add({"t": "Rules firing: "})
        add(
            {
                "t": ", ".join(f"{r['rule_id']} on {r['asset']}" for r in firing),
                "href": "#p-triggers",
            }
        )
        add({"t": ". "})
    else:
        add({"t": "No pre-committed 4.x rule is firing"})
        if unavailable:  # C4: name the leg that is actually missing, never a generic sentence
            legs: dict[str, int] = {}
            for r in unavailable:
                note = str(r.get("note") or "")
                names = (
                    [x.strip() for x in note.split(":", 1)[1].split(",")]
                    if "unavailable:" in note
                    else ["unknown"]
                )
                for n in names:
                    if n:
                        legs[n] = legs.get(n, 0) + 1
            add(
                {
                    "t": f" ({len(unavailable)} unavailable: "
                    + ", ".join(f"missing {k} ×{v}" for k, v in legs.items())
                    + ")",
                    "href": "#p-triggers",
                }
            )
        add({"t": ". "})
    if cliffs:
        add({"t": "Cliff calendar: "})
        add(
            {
                "t": f"{len(cliffs)} token{'s' if len(cliffs) != 1 else ''} meet Rule 5.1 in the next 30 days",
                "href": "#p-events",
            }
        )
        add({"t": " (informational; the 2026 pre-cliff drift is at the base rate). "})
    if venue_breaches or low_score_breach:
        add({"t": "Venue limits breached on the example book: "})
        add(
            {
                "t": ", ".join(venue_breaches)
                + (" and the low-score aggregate" if low_score_breach else ""),
                "href": "#p-venue",
            }
        )
        add({"t": ". "})
    else:
        add({"t": "No venue limit is breached on the example book"})
        add({"t": "", "href": "#p-venue"})
        add({"t": ". "})
    # WO10 §4 — reconcile the reading with the resistance model: when a Tier-1 name is in a test,
    # incorporate the recalibrated break/reject cumulative incidence, and log a conflict whenever
    # the leverage state (Φ levered) and the test model point opposite ways.
    phi_val = frag.get("phi") if frag else None
    for t in (active_tests or []):
        cb = ((t.get("cif_break") or {}).get("20"))
        cr = ((t.get("cif_reject") or {}).get("20"))
        if cb is None:
            continue
        lvl = t.get("r_def", "level")
        add({"t": f"{t.get('base')} is in a {t.get('side')} test ({lvl}); the resistance model puts "})
        add({"t": f"break {100 * cb:.0f}% / reject {100 * (cr or 0):.0f}% over 20d", "href": "#p-resistance"})
        add({"t": f" (cleared: {'yes' if t.get('close_cleared') else 'not on a daily close'}). "})
        if phi_val is not None and phi_val > 1 and cb is not None and cb > (cr or 0):
            msg = (f"{t.get('base')}: leverage state Φ={phi_val:+.2f} (levered) vs resistance model "
                   f"break {100 * cb:.0f}% — the tested base rate leans continuation, not breakdown")
            if conflicts is not None:
                conflicts.append({"base": t.get("base"), "phi": round(phi_val, 2),
                                  "cif_break_20": round(cb, 4), "cif_reject_20": round(cr or 0, 4),
                                  "note": msg})
            add({"t": "Reading conflict logged: ", "cls": "alert"})
            add({"t": msg, "cls": "alert", "href": "#resmethods"})
            add({"t": ". "})
    return [s for s in seg if s["t"] != "" or "href" not in s]


def reading_text(segments: list[dict]) -> str:
    return "".join(s["t"] for s in segments).strip()
