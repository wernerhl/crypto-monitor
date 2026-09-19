# Resistance / support tests — conditional probability model

Generated 2026-09-19 from price history beginning 2018-06-21 (Binance daily klines; the model does not claim 2017). Frozen parameters in `config/resistance_model.yaml`, calibrated 2026-09-19. This is descriptive measurement: **no trigger, no threshold change, Φ untouched.**

## The framing (fixed)

Φ is not a direction forecaster. The model estimates two separate objects and multiplies them: **(1) P(direction | state at a test)** — a clean break vs a rejection vs chop — and **(2) E(move | direction)** — the size of each move. The tradable quantity is (1)×(2). It stays honest when (1) is near a coin flip because the asymmetry lives in (2).

## Event definition (§1)

A resistance test on day *t* requires all of: 5-day and 3-day return positive into the level; close within 2.0% of a reference resistance *R*; and approach from below over the prior 3 days. The support test is the mirror. A **clean break** is a close beyond *R* by 0.5% that holds 2 closes; a **rejection** is a close back through *R* with a lower low (resistance) / higher high (support) than the pre-test swing within the horizon; otherwise **chop**. Levels and state are taken as of *t−1* (causal).

Three reference levels, computed in parallel and all reported (never one picked after seeing results): **90-day high**, **most recent confirmed swing high** (5-bar fractal), and **nearest high-volume node above price** in a 90-day volume-by-price profile.

## Base rate and magnitude, by cell (§3, §7a)

| side | level | H | n | weeks | P(break) | P(reject) | P(chop) | E[break] | E[reject] | published |
|---|---|---|---|---|---|---|---|---|---|---|
| resistance | 90d high | 5d | 246 | 110 | 65.8% | 8.5% | 25.6% | +9.8% | -17.4% | yes |
| resistance | 90d high | 20d | 242 | 108 | 76.9% | 16.9% | 6.2% | +15.8% | -22.6% | yes |
| resistance | swing high | 5d | 493 | 213 | 65.5% | 11.2% | 23.3% | +7.6% | -13.8% | yes |
| resistance | swing high | 20d | 489 | 211 | 77.5% | 20.2% | 2.2% | +13.6% | -21.8% | yes |
| resistance | volume shelf | 5d | 872 | 307 | 62.6% | 18.4% | 19.0% | +5.5% | -14.9% | yes |
| resistance | volume shelf | 20d | 866 | 305 | 70.7% | 27.0% | 2.3% | +10.3% | -22.9% | yes |
| support | 90d low | 5d | 186 | 90 | 51.1% | 9.1% | 39.8% | -6.6% | +21.4% | yes |
| support | 90d low | 20d | 185 | 89 | 64.3% | 24.9% | 10.8% | -5.1% | +26.4% | yes |
| support | swing low | 5d | 523 | 200 | 59.7% | 12.2% | 28.1% | -4.1% | +17.5% | yes |
| support | swing low | 20d | 519 | 198 | 69.6% | 27.0% | 3.5% | -3.3% | +27.4% | yes |
| support | volume shelf | 5d | 825 | 289 | 64.6% | 16.1% | 19.3% | -2.9% | +15.9% | yes |
| support | volume shelf | 20d | 812 | 286 | 73.9% | 24.3% | 1.8% | -1.6% | +29.0% | yes |

The size asymmetry is the point: a rejection is typically the minority outcome, but its move (a maximum adverse excursion) is materially larger than the break continuation, so P×E is not dominated by the more likely direction.

## Out-of-sample calibration — resistance / volume shelf / 5d (§3a)

| predicted P(break) | observed | n |
|---|---|---|
| 24.6% | 71.4% | 7 |
| 36.3% | 77.3% | 22 |
| 47.0% | 52.5% | 137 |
| 53.6% | 51.8% | 222 |
| 65.0% | 63.2% | 68 |
| 75.1% | 72.2% | 266 |
| 83.1% | 72.2% | 79 |
| 91.4% | 88.9% | 9 |

## The explicit Φ answer (§4)

Φ's coefficient on P(reject), resistance side, by level definition: 90d high/5d z=-0.13; 90d high/20d z=-0.62; swing high/5d z=-0.35; swing high/20d z=-0.29; volume shelf/5d z=2.2; volume shelf/20d z=2.31.

It clears two clustered standard errors in the volume-shelf definition but not the 90-day-high definition. **Φ does not robustly separate rejection _probability_ from the base rate across all three definitions** on the sample available — reported for all three, not the best one. The overlay is preliminary and re-runs as the leverage history lengthens.

### Λ/D on rejection size — the mechanism

**Insufficient sample (n=0).** Liquidation depth exists only from the collector era, so the rejection events with a Λ/D reading are far below the 20-event floor. The leverage-bites-the-loser magnitude claim cannot yet be tested; nothing is published on it.

## Running scorecard (§5, §8)

0 tests resolved since go-live (2026-09-19). The scorecard accumulates in public; each resolved test carries the model's predicted class probabilities and the realised outcome, so live calibration is visible on the panel.


_As of 2026-09-18. Regenerate with `scripts/resistance_note.py`._
