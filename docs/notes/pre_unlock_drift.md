# Pre-unlock drift: what the schedule says, 2021–2026

*Research note for work-order item D. Generated 2026-09-08 from `cliff_study_events` and
`cliff_study` (`monitor.compute.cliff_study`, weekly job) by `scripts/unlock_drift_note.py`.
Nothing here changes a threshold; Rule 5.1 keeps
`single_unlock_float_share_min = 1%` and
`single_unlock_days_of_volume_min = 2`.*

## Question

Rule 5.1 (notes §5) shorts a token two to four weeks ahead of a scheduled cliff that is large
relative to the float and to real volume, on the argument that recipients who can hedge do so
before the date and that the market front-runs the supply. The rule's live hit rate has almost
no sample (the monitor is days old), so this note asks the schedule itself: over every cliff
DefiLlama records since 2021 with price coverage, did the token drift down over the fourteen
days before the date, and does the size of the event or the class of the recipient change the
answer?

## Data and definitions

* **Events.** 2645 scheduled cliffs (rows of `unlock_events` with `kind = cliff`,
  positive amount, grouped by token and date) between 2021-01-01 and 2026-09-08 minus 14 days,
  for tokens with daily exchange klines on Binance, OKX, Bybit, Coinbase or Kraken. The
  schedule is heavily weighted to 2025–26 (1057 and
  1155 events) because DefiLlama's emissions coverage grew with
  the launches of that period; 2021–23 hold 167 events.
* **Hit.** Negative log return over the fourteen days ending on the cliff date (the same
  definition as the live hit-rate table). Post-return is the fourteen days after.
* **Size.** Share of float = tokens unlocked ÷ float at the cliff date, where float is backed
  out of today's circulating supply by removing every later scheduled cliff and the current
  linear rate times the elapsed days (2641 of 2645 events; burns and re-issuance
  are ignored, so shares for old events are approximate). Days of volume = USD value at the
  cliff ÷ average daily quote volume over the thirty days ending fifteen days before the
  date, on venues passing the *current* wash filters where a wash row exists (154
  events) and on every venue otherwise (2433 events, flagged `unfiltered`). Wash filters
  cannot be evaluated historically; the book depth they need is not archived.
* **Base rate.** The unconditional share of negative fourteen-day returns over the same tokens
  and period: 56.2 %. A cliff hit rate is only informative against this number, because the
  sample sits in a period when most of these tokens fell.

## Results

### Headline

| group | n | hit rate | s.e. | mean pre 14 d | median pre 14 d | mean pre vs BTC | mean post 14 d | median post 14 d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| all cliffs with price coverage | 2645 | 59.5 % | 1.0 % | -2.7 % | -3.7 % | -2.5 % | -1.1 % | -2.6 % |
| cliffs with float and volume measured | 2583 | 58.8 % | 1.0 % | -2.3 % | -3.5 % | -2.2 % | -1.1 % | -2.5 % |
| Rule 5.1 (> 1% of float and > 2 days of volume) | 311 | 63.0 % | 2.7 % | -3.9 % | -4.0 % | -4.1 % | -0.3 % | -2.4 % |
| below either threshold | 2272 | 58.3 % | 1.0 % | -2.1 % | -3.4 % | -1.9 % | -1.2 % | -2.5 % |
| all days, same assets and period (share of negative 14-day returns) | 42932 | 56.2 % |  | n/a | n/a | n/a | n/a | n/a |

Read: 59.5 % of all cliffs were preceded by a negative fourteen-day return
against a base rate of 56.2 %; cliffs that met both Rule 5.1 legs were preceded by a
negative return 63.0 % of the time (n = 311, s.e. 2.7 %), with a
mean pre-cliff return of -3.9 % and -4.1 % relative to BTC.
The BTC-relative figure matters: it removes the market's own drift over the same windows.

### By recipient class (dominant class by amount)

| group | n | hit rate | s.e. | mean pre 14 d | median pre 14 d | mean pre vs BTC | mean post 14 d | median post 14 d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| community | 32 | 71.9 % | 7.9 % | -7.1 % | -5.1 % | -4.7 % | +1.1 % | -2.6 % |
| ecosystem | 495 | 64.0 % | 2.2 % | -5.0 % | -5.7 % | -3.9 % | -2.4 % | -3.5 % |
| investors | 474 | 61.6 % | 2.2 % | -5.8 % | -5.9 % | -7.1 % | -5.7 % | -7.2 % |
| public | 9 | 22.2 % | 13.9 % | +9.1 % | +13.5 % | +3.2 % | +9.9 % | +19.7 % |
| team | 301 | 57.8 % | 2.8 % | -3.1 % | -2.9 % | -4.1 % | -0.4 % | -1.7 % |
| unknown | 1334 | 57.3 % | 1.4 % | -0.7 % | -2.4 % | -0.1 % | +0.7 % | -1.7 % |

![hit rate by class](figures/unlock_hit_by_class.svg)

![pre-cliff return vs BTC by class](figures/unlock_pre_vs_btc_by_class.svg)

### By size

| group | n | hit rate | s.e. | mean pre 14 d | median pre 14 d | mean pre vs BTC | mean post 14 d | median post 14 d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 0.5–1 % | 52 | 57.7 % | 6.9 % | -3.1 % | -5.7 % | -3.0 % | -1.8 % | -3.2 % |
| 1–2 % | 154 | 64.9 % | 3.8 % | -5.9 % | -5.3 % | -6.0 % | -1.9 % | -5.1 % |
| 2–5 % | 199 | 62.8 % | 3.4 % | -3.4 % | -3.4 % | -2.8 % | +0.0 % | -2.6 % |
| < 0.5 % | 2142 | 58.6 % | 1.1 % | -2.3 % | -3.6 % | -2.1 % | -1.3 % | -2.5 % |
| > 5 % | 94 | 63.8 % | 5.0 % | -6.0 % | -4.8 % | -7.1 % | +2.5 % | +0.1 % |

![hit rate by share of float](figures/unlock_hit_by_share.svg)

| group | n | hit rate | s.e. | mean pre 14 d | median pre 14 d | mean pre vs BTC | mean post 14 d | median post 14 d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 1–2 d | 331 | 64.4 % | 2.6 % | -5.1 % | -5.6 % | -4.5 % | +0.1 % | -2.5 % |
| 2–5 d | 345 | 51.0 % | 2.7 % | +0.1 % | -0.4 % | -2.2 % | -3.0 % | -1.3 % |
| < 1 d | 1622 | 60.3 % | 1.2 % | -2.8 % | -4.0 % | -2.0 % | -1.1 % | -2.9 % |
| > 5 d | 289 | 53.6 % | 2.9 % | +0.9 % | -1.3 % | +0.0 % | -0.5 % | -3.2 % |

![hit rate by days of volume](figures/unlock_hit_by_dov.svg)

### By year

| group | n | hit rate | s.e. | mean pre 14 d | median pre 14 d | mean pre vs BTC | mean post 14 d | median post 14 d |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| 2021 | 79 | 41.8 % | 5.5 % | +3.3 % | +4.9 % | -0.3 % | +3.4 % | +7.1 % |
| 2022 | 37 | 64.9 % | 7.8 % | -2.5 % | -2.5 % | -1.5 % | -5.4 % | -7.1 % |
| 2023 | 51 | 52.9 % | 7.0 % | +0.8 % | -0.3 % | -2.4 % | +7.1 % | +1.3 % |
| 2024 | 266 | 54.5 % | 3.1 % | +3.4 % | -2.5 % | -2.0 % | +5.4 % | -0.1 % |
| 2025 | 1057 | 67.2 % | 1.4 % | -6.6 % | -7.3 % | -6.0 % | -3.8 % | -5.6 % |
| 2026 | 1155 | 54.9 % | 1.5 % | -1.2 % | -1.6 % | +0.3 % | -0.6 % | -1.1 % |

![hit rate by year](figures/unlock_hit_by_year.svg)

### Event-time paths

Average cumulative log return from fourteen days before to fourteen days after the cliff
(events with a full 29-day price window):

![event paths](figures/unlock_event_paths.svg)

## What the numbers support, and what they do not

1. **A pre-cliff drift exists but it is modest and mostly a market effect.** All cliffs: hit
   rate 59.5 % against a base rate of 56.2 %; mean pre-cliff return
   -2.7 %, of which -2.5 % is relative to BTC. The
   difference from the base rate is a few percentage points with a standard error near one
   point: real, small.
2. **Size selects.** The Rule 5.1 subset (> 1% of float and
   > 2 days of volume) has the higher hit rate and the more
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
   in between. The `unknown` class (the largest, 1334 events) is close to the base
   rate, which is what one expects when the label carries no information. `public` has nine
   events: ignore.
4. **The post-cliff return is not a mirror image.** Median post-cliff returns are negative in
   most groups, so the supply is not fully priced by the date; but the means are pulled up by
   a tail of rebounds. A structure that holds a short through the date is a different trade
   from the one Rule 5.1 describes (short two to four weeks before, cover into the date), and
   this sample does not favour it.
5. **Regime dependence is large.** 2021 cliffs (n = 79) were preceded by
   *positive* returns (a rising market); 2025 shows the strongest drift. The effective sample
   for anything regime-dependent is the number of regimes, not the number of events.

## Implications for the rule

* The thresholds stay where they are. The study supports the *sign* of the rule and the
  choice to condition on size and class; it does not identify a better threshold, and a
  threshold moved to fit this sample would be fitted to a 2025-heavy schedule.
* The float approximation is the weakest input. For tokens whose supply history is known
  exactly (the DefiLlama per-protocol detail), `share_of_float` could be recomputed from the
  detail rather than backed out; that is a data task, not a calibration.
* Days of volume should be read with `adv_basis`. Only 154 events use wash-filtered
  volume; the rest use unfiltered exchange volume and overstate liquidity for names with
  wash-prone venues, which makes their `days_of_volume` too small and keeps some genuine
  Rule 5.1 events out of the subset.

## Reproduction

```bash
uv run monitor compute weekly                                   # rebuilds the cliff_study tables
PYTHONPATH=src uv run --no-sync python scripts/unlock_drift_note.py   # this note and its figures
```
