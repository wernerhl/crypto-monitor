# Post-mortem — the 15–21 September 2026 BTC run

*First live post-mortem (work order 10). The format is reused for every resolved test that
contradicts a stated lean.*

## The facts (from the monitor's own history)

- BTC daily closes: 75,644 (15 Sep) → 81,178 (20 Sep), **+7.3% in five sessions**, +5.8% on the
  18th alone; an intraday 84.4k print on the 21st.
- The 82.3k resistance level (90-day high) was **not cleared on a daily close** as of the 20th.
- Φ rose from 0.51 (10 Sep) to 1.28 (19 Sep) as price ran.
- The resistance model (work orders 8/9) said, on the 19th, **break 67% / reject 30% over 20 days**.
- The analyst's stated lean on the 13th and 19th was **downside-skewed, from the crowding
  mechanism** (high OI percentile, funding, liquidation mass = "levered, fragile").

## What the model said vs what the analyst said

| | direction | basis |
|---|---|---|
| Resistance model (tested) | break ≈ 67% over 20d | walk-forward cumulative incidence, recalibrated, out-of-sample calibrated |
| Analyst (mechanism-only) | downside-skewed | crowding mechanism (Φ, OI pct, Λ/D) — not a tested base rate |

Both were on the page, unreconciled: the reading paragraph said "levered, fragile" while the
resistance panel said "break 67%".

## The outcome

The BTC 90-day-high test (dated 2026-09-18) **resolved as a break** on the 21 Sep close and is
logged in the live scorecard (predicted P(break) 0.68 → realised break). The AVAX, ETH, LTC, ADA,
LINK and XRP resistance tests in the same window also broke; SUI and BNB support tests held
(reject). The live scorecard now stands at **15 resolved tests, forward Brier 0.144 against a
base-rate Brier of 0.160 (skill +10%)**. **The model was right; the mechanism-driven read was
wrong.**

## Diagnosis (one line)

The system measured HOW MUCH leverage sits on the market and had no representation of WHICH WAY the
market is going, so a trend being bought read as a fragility warning. Conditioned on trend state,
Φ's relation to the forward return flips: in an UPTREND mean Φ is positive and its correlation to
the next-20-day return is **+0.17**, not negative.

## Design changes taken (work order 10)

1. **Trend state** added as a first-class market layer on panel 3 (UPTREND / DOWNTREND / RANGE from
   a frozen momentum rule) with its continuation base rate; the leverage reading is now conditional
   on it. Φ is annotated "carries no directional information" whenever Φ > 1 is reported.
2. **Demand-side flows** added: Coinbase premium (US-demand proxy) and exchange net flow (the
   accumulation that a flat stablecoin supply hid) — over this run BTC showed a multi-billion-dollar
   net *outflow* from exchanges while stablecoin supply was flat. US spot ETF creations and exchange
   stablecoin inflow are named but marked unavailable rather than scraped.
3. **Convexity framing** on the vol reading is now two-sided and names the cheap side given the
   trend state (cheap implied is cheap in both directions).
4. **Reconciliation**: the reading incorporates the active-test probabilities and logs a
   `reading_conflict` whenever the leverage state and the test model point opposite ways.
5. **Analyst discipline** encoded (runbook + notes): no directional lean may contradict the tested
   base rate unless labelled "mechanism-only judgment; not supported by the system's tests".
6. **Freshness debt** fixed: `positioning_history`/`oi_history` are rolled forward each hourly run
   and the job now fails (fail-not-publish) if the OI history lags the live OI by more than a day.

The two mechanism-only leans of 13 and 19 September are the case that motivated the discipline rule.
