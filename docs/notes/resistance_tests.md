# Resistance / support tests — competing-risks conditional probability model

Generated 2026-09-19 from price history beginning 2018-06-21. Frozen parameters in `config/resistance_model.yaml`, calibrated 2026-09-19. Descriptive measurement: **no trigger, no threshold change, Φ untouched.**

## What work order 9 changed

The five-day snapshot (work order 8) lumped *unresolved* tests into a "chop" bucket, which flattered both the break and the reject probabilities, and the raw multinomial was overconfident at the tails. WO9 re-casts the outcome as **competing risks**: for each test we record the time to a break and the time to a rejection, whichever comes first, with the 40-day window and end-of-data as censoring. The published quantity is the cause-specific **cumulative incidence** — P(the level breaks before it holds) by horizon — recalibrated walk-forward and carrying a block-bootstrap band. "Chop" is censoring, not an outcome.

## Cause-specific cumulative incidence (§2.1), horizons [5, 10, 20, 40] days

| side | level | n | cause | 5d | 10d | 20d | 40d | med. days |
|---|---|---|---|---|---|---|---|---|
| resistance | consensus | 83 | break | 66.3% | 72.5% | 76.3% | 77.5% | 1 |
| | | | reject | 12.5% | 16.2% | 21.2% | 21.2% | 5 |
| support | consensus | 77 | break | 71.4% | 75.3% | 76.6% | 79.2% | 1 |
| | | | reject | 9.1% | 14.3% | 18.2% | 20.8% | 6 |
| resistance | 90d high | 272 | break | 66.5% | 73.5% | 77.6% | 79.5% | 1 |
| | | | reject | 8.6% | 13.8% | 16.8% | 19.0% | 7 |
| resistance | swing high | 530 | break | 65.5% | 72.7% | 77.2% | 77.8% | 1 |
| | | | reject | 11.2% | 16.7% | 20.7% | 21.8% | 5 |
| resistance | volume shelf | 882 | break | 62.9% | 68.1% | 70.8% | 71.8% | 1 |
| | | | reject | 18.3% | 24.3% | 26.9% | 27.6% | 4 |
| support | 90d low | 194 | break | 51.0% | 60.3% | 65.0% | 69.6% | 2 |
| | | | reject | 8.8% | 16.5% | 24.7% | 27.3% | 9 |
| support | swing low | 554 | break | 59.1% | 65.8% | 68.7% | 70.2% | 2 |
| | | | reject | 12.3% | 22.3% | 27.6% | 29.4% | 6 |
| support | volume shelf | 835 | break | 64.4% | 70.8% | 73.2% | 74.0% | 1 |
| | | | reject | 16.2% | 22.3% | 25.0% | 25.8% | 4 |

Bands (not shown in this table; on the panel) are the 120-replicate block bootstrap over calendar weeks, 16th–84th percentile. The size asymmetry survives: a rejection is the minority outcome but its move is larger, so the expected move is not dominated by the more likely direction.

## Partial pooling and walk-forward recalibration (§1.1, §1.2)

One partially-pooled multinomial hazard (ridge, cell as a factor) is fit over all six single-definition cells so the sparse 90-day-high cell borrows the shared covariate slopes. A walk-forward isotonic map (predicted→realised, estimated only on prior data) is applied on top; the recalibrated probability is what the panel publishes, with the raw retained in the JSON for one release.

Brier at 20 days: raw **0.1994** → recalibrated **0.1946** vs a base-rate-only **0.2** (3198 out-of-sample tests). Recalibration removes the tail overconfidence the audit flagged (a cell that predicted 0.95 and was right 0.76 of the time).

## Consensus vs single definition (§2.2)

Out-of-sample Brier at 20 days by selection: consensus days 0.1911 (n=304), non-consensus days 0.2002 (n=2894); single definitions 90d 0.2041 (n=442), swing 0.1936 (n=1041), shelf 0.2016 (n=1715). The data pick **consensus** as the headline; the single definitions remain the robustness strip.

## The Φ answer (Tier 3, preliminary)

Φ's coefficient on P(reject), resistance side, by definition: 90d high z=-0.62; swing high z=-0.29; volume shelf z=2.31. Significant in the volume-shelf definition but not the 90-day-high — definition-dependent, not established. The mechanistic Λ⁻/D claim is **insufficient sample (n=0)**: liquidation depth is collector-era only. The overlay never drives a published number and re-runs as data accrue.

> 6 October review: report the crowding overlay's independent-event count per cell and whether any cell has crossed the publish threshold; only then revisit whether Phi shifts direction or magnitude at a test. The accelerants are collector coverage (WO6-B) and the TradingView leverage backfill (WO5), not the model.


_As of 2026-09-18. Regenerate with `scripts/resistance_note.py`._
