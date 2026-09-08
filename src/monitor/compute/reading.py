"""The daily state reading (work order C1): one deterministic paragraph built from the
numbers on the page, regenerated with the hourly job. No adjectives that are not implied by a
threshold or a sign; every number is a segment that links to the panel it came from.

Segments are dicts {"t": text} or {"t": text, "href": "#panel-id"}. The page joins them."""

from __future__ import annotations

from datetime import date

COMPONENT_NAMES = {
    "z_fr": "funding",
    "z_oi": "open interest to cap",
    "z_vrp_neg": "the negative of the variance risk premium",
    "z_dd": "drawdown from the 90-day high",
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
) -> list[dict]:
    seg: list[dict] = []
    add = seg.append
    # --- fragility and its two largest components
    if frag and frag.get("phi") is not None:
        phi = frag["phi"]
        word = "fragile" if phi > 1 else "building" if phi > 0 else "calm"
        add({"t": f"{as_of.isoformat()}: the fragility index is "})
        add({"t": f"{phi:+.2f}", "href": "#p-state"})
        add({"t": f" ({word}, {frag.get('n_components') or 0} of 5 components)"})
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
    if vr:
        bits = []
        for c, v in vr:
            if v is None:
                bits.append(f"{c} VRP n/a")
            else:
                bits.append(f"{c} VRP {'positive' if v > 0 else 'negative'} ({v:+.3f})")
        add({"t": "Implied minus realised variance: "})
        add({"t": ", ".join(bits), "href": "#p-state"})
        add({"t": ". "})
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
            add(
                {
                    "t": f" of its 250-day range (n = {positioning_btc.get('oi_rel_pctile_n') or 0}). "
                }
            )
    if dd_90 is not None or dd_cycle is not None:
        add({"t": "BTC is "})
        add({"t": f"{_pct(dd_90)} from its 90-day high", "href": "#p-state"})
        add({"t": f" and {_pct(dd_cycle)} from its cycle high. "})
    # --- stablecoin growth
    if sc_growth_30d is not None:
        add({"t": "Stablecoin supply grew "})
        add({"t": _pct(sc_growth_30d), "href": "#p-state"})
        add({"t": " over 30 days. "})
    # --- rules and venues
    firing = [r for r in rules if r.get("fired") is True]
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
        add({"t": "No pre-committed rule is firing"})
        add(
            {
                "t": f" ({len(unavailable)} unavailable)" if unavailable else "",
                "href": "#p-triggers",
            }
        )
        add({"t": ". "})
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
    return [s for s in seg if s["t"] != "" or "href" not in s]


def reading_text(segments: list[dict]) -> str:
    return "".join(s["t"] for s in segments).strip()
