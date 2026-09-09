# Changelog

## 2026-09-06 — Phase 1: skeleton and CI
* Verified 81 endpoints live before writing any adapter (`docs/data_sources.md`,
  `scripts/verify_sources.py`, samples in `tests/fixtures/live_verification/`).
* Findings that change the plan: Binance `allForceOrders` removed; DefiLlama emissions on
  `api.llama.fi` is paid but the same data is free on `defillama-datasets.llama.fi`; CoinGecko
  free history is capped at 365 days; CoinMetrics community exposes MVRV and exchange supply but
  not realised cap or SOPR; beaconcha.in needs a key; no free official spot-ETF-flow source.
* Repository layout, `pyproject.toml` (uv, pinned), Makefile, CI, daily workflow deploying a
  placeholder page to GitHub Pages with a live-sha verification step.
* Deviation: `ccxt` is not used; adapters call the verified REST endpoints directly so that raw
  responses are stored verbatim (see data_sources.md §1).
* Deviation: tier recomputation is weekly (build prompt) rather than monthly (notes).

## 2026-09-06 — Phase 2: universe and prices (first run)
* Adapters with raw-response envelopes, per-source spacing and backoff, idempotent daily
  buckets: CoinGecko (+ CoinPaprika fallback), Binance spot/USDT-M/COIN-M listings, Bybit,
  OKX, Coinbase, Kraken listings; perp snapshots (OI in USD) on Binance/Bybit/OKX; daily spot
  candles on all five venues (OKX with `bar=1Dutc` so days align).
* Processed tables: `markets`, `coin_meta`, `venue_listings`, `perp_snapshot`, `prices_daily`,
  `universe` (with `as_of`, membership history kept) plus monthly archive partitions.
* Tiering per `config/universe.yaml`; depth criterion `pending_phase3`; ADV flagged `reported`.
  First live run (one day of OI): USD 100M aggregate OI admitted 30 names, USD 150M admitted 22,
  so the Tier 1 OI threshold is set at 150M (notes target 15–25 before the depth screen).
  Result on 2026-09-06: Tier 1 = 22, Tier 2 = 29 (ADV ≥ 10M reported, ≥ 2 spot venues), Tier 3 = 149;
  8 stablecoins and 3 wrapped/LST tokens excluded via CoinGecko category lists.
* Exclusions come from CoinGecko category membership lists (one call per category, verified)
  rather than waiting for per-coin metadata; candidates without per-coin tags are flagged
  `meta_status = pending` instead of being excluded.
* `daily.yml` now fetches, computes, commits as `crypto-monitor-bot` with `[skip ci]`, renders,
  deploys and verifies the live sha. `manual.yml` added.
* Site: `universe.html` (tiers with the metrics that placed each asset, CSV download) and
  `status.html` (per-table freshness with reasons).
* Observed during the first run: CoinGecko returned 429 at 6.5 s spacing; spacing set to
  12.5 s and coin-metadata refresh capped at 30 ids per run (documented in data_sources.md).
* Deviation: tier membership is recomputed on every daily run (a superset of the weekly
  recomputation the build prompt asks for); the weekly job will freeze the Sunday row as the
  official membership from phase 7.
* Local quirk: macOS marks `.venv` files hidden and Python 3.12 skips hidden `.pth` files;
  the Makefile sets `PYTHONPATH=src` and clears the flag on `make sync`.

## 2026-09-06 — Phase 3: derivatives and liquidity
* Hourly job live (456 requests in 114 s on the first run, all 200). Positioning, options
  (Black-76 deltas checked against Deribit greeks), fragility index, Rules 4.1–4.3 computing.
* Order-book depth at 2 % is a lower bound on Binance/OKX/Bybit for BTC and ETH (truncated
  books); the table records coverage and the page shows "≥".
* Wash filters: the absolute Benford χ² rejected every venue (trade notionals cluster on the
  price's leading digit), so the test is on trade sizes and relative to Coinbase/Kraken; a venue
  fails when two of three tests fail. Binance BTC/ETH still fail the size-digit test alone.
* Deviation: raw books trimmed to ±3 % of mid and trade lists slimmed to 500 × (time, price,
  size) before storage — the first untrimmed hourly bucket was 5.4 MB gzipped (≈ 130 MB/day).
  Hourly raw retention in the repo is 14 days rather than 90 (daily raw keeps 90).

## 2026-09-06 — Phase 4: supply, macro, stablecoins, events
* DefiLlama stablecoins (supply, peg, 30-day growth), FRED keyless CSV (net liquidity with
  units reconciled), DefiLlama datasets unlock schedule with recipient classes, fees/TVL,
  CoinMetrics community (MVRV, exchange supply/flows), ultrasound.money staking (unofficial),
  Snapshot governance; ESP at 13/30/90 days, dilution, cliff flags (Rule 5.1), event strip.
* Spot ETF flows and LTH-SOPR shown as unavailable (no free official source).
* Linear vesting tranches from the index are spread evenly over 400 days (approximation,
  flagged) until the per-protocol detail endpoint is fetched for Tier 1/2 names.

## 2026-09-06 — Phase 5: trades, venue, stress, screens
* Venue panel (scores from `config/venues.yaml`, exposure vs limits, stablecoin and systemic
  exposures), book risk (state-weighted Ledoit–Wolf, stress tilt, N_eff, Student-t ES,
  cascade / halt / systemic P&L), trade-structure table (basis, funding carry, vol selling
  with the Rule 4.3 veto, unlock shorts), sector-relative screens with IC, factor model.
* Example book: gross 70 %, net 50 %, N_eff ≈ 1.1 — the notes' point that a crypto book is a
  market view plus a few relative-value positions.
* Sector map seeded from CoinGecko categories (`monitor universe seed-sectors`); the hand
  review is pending and recorded here when done.
* Cost assumptions (6 % stablecoin borrow, public taker fees) are documented defaults.

## 2026-09-06 — Runner reachability and failure isolation
* Probe workflow: Binance api/fapi/dapi return 451 and all Bybit hosts 403 from GitHub-hosted
  runners; `data-api.binance.vision` serves Binance spot (200). Binance spot switched to the
  mirror; Binance futures and Bybit are "unavailable this run" from Actions and refresh only
  through `scripts/collector.sh` (mode b). Deviation from the source map: Actions-side
  derivatives come from OKX + Deribit; the Tier 1 rule keeps its three-venue definition and
  uses the last reachable listing snapshot with the fetch outcome visible on the status page.
* Every fetch is isolated per dataset (`fetch_status` table); only the aggregator markets
  dataset is critical.
* Local quirk: a sync agent on the build machine creates "name 2.ext" duplicate copies during
  rapid writes; they are git-ignored, deleted by `make clean-dups`, and the size check fails
  if any is tracked. 1 363 such copies were removed from the tree on this date.

## 2026-09-06 — Phase 6: backfill
* `monitor backfill --start` pulls daily candles (Binance mirror `endTime` paging, OKX
  `history-candles`), funding history (Binance `startTime` paging multi-year, Bybit `endTime`
  paging, OKX ~3 months), 30-day OI history (Binance, OKX), 365-day market caps (CoinGecko
  free cap) and recomputes walk-forward: daily OI-weighted funding, z^FR (90 d), OI change,
  Rule 4.2 evaluations, fragility history from the three components available in history
  (z_fr, z_dd, z_sc_neg). Depth cannot be backfilled: liquidity-gate history before go-live
  is the volume proxy, flagged. Hit rates and screen ICs are computed by the weekly job on the
  archive with sample sizes; thresholds were not touched (`calibrated_on` unchanged).
* The full Rules 4.1–4.3 cannot be evaluated over history (no liquidation density, depth,
  liquidation volume or options chains before go-live), so the methods page reports the
  partial-input variants 4.1p (z^FR and OI percentile) and 4.2p (z^FR and 5-day OI drop),
  labelled as such, whose OI leg only exists for the 30 days the venues' OI history covers.
  Result on 2026-09-06: 4.2p 4 hits of 7, 4.1p n = 5; 4.3 and 5.1 have no elapsed horizons.
* Weekly factor model on 428 weeks (2018–2026 from Kraken/Coinbase/Binance/OKX candles):
  screen ICs over 52 weeks are small with standard errors of the same order (momentum
  0.02 ± 0.03, size 0.08 ± 0.02, Amihud −0.01 ± 0.01), which is the notes' point.

## 2026-09-06 — Phase 7: hardening
* Weekly job: tier freeze, factor model, IC, hit rates, raw rotation (`scripts/rotate_raw.sh`
  → `data-archive` orphan branch, hourly raw kept 10 days, daily 90), repo-size check with
  duplicate-file guard, review issue (`scripts/weekly_issue.sh`).
* Fresh-clone reproduction (recorded here as the build prompt asks): cloned
  `wernerhl/crypto-monitor` at `2bda8ab` into an empty directory on the build machine,
  `make sync` (uv, pinned lock), `make compute` (replayed every raw file: all processed tables
  rebuilt, 260 universe rows, 20 positioning rows, 51 liquidity rows), `make site` (five pages),
  `make test` (67 passed), size check 89 MB. Deviation: no container runtime is installed on
  the build machine (no docker/podman), so the test ran in a clean directory with the same uv
  toolchain rather than in a container; CI performs the same steps on a fresh Ubuntu runner.
* Deviation: the README has no screenshot file — the page is live at the URL and the build
  environment cannot save a browser capture to disk; add one by hand from the live site.

## 2026-09-07 — Front-page redesign
* Dark editorial layout in the style of the portfolio-tournament page: serif headings,
  monospace labels, alert banners (rules firing, venue breaches, unavailable datasets), a
  headline strip, and per-panel "intuition" lines restating the notes.
* Panel 3 gets a fragility gauge (calm < 0 < building < 1 < fragile), sign-aware component
  tiles, and two-year charts from the backfill (fragility, OI-weighted funding, IV term
  structure, net liquidity, stablecoin supply and growth). Panels 1, 2, 5, 6, 7 get exposure
  vs limit bars, factor and scenario bars, net-rate bars, a sized event timeline, and the
  depth-vs-ADV and momentum-vs-float scatters. Rule inputs are shown as chips against their
  frozen thresholds.
* Data fixes found while wiring the charts: live z-scores now use the backfilled funding
  history (90-day windows) instead of the one-day live table; OI statistics (percentile,
  5-day change, quadrant, liquidation density, fragility z_OI) use the OKX series, the one
  venue whose OI is both historical and reachable from every runner, because summing venues
  with different history lengths produced a level jump at the seam; NaN/Infinity are
  written as null in every site JSON (browsers reject them).
* `data/history.json` (≈ 0.5 MB, two years of daily points) is regenerated by the daily job.

## 2026-09-07 — Completing the gaps
* **Geo-blocked venues:** `scripts/install_collector_macos.sh` installs two launchd agents on the
  build Mac (`com.wernerhl.crypto-monitor.hourly` at :09, `.daily` at 01:45 local) that run
  `scripts/collector.sh`, which pulls, fetches, computes, commits as the bot and pushes. Binance
  futures and Bybit now refresh from a machine that can reach them; the Actions jobs remain the
  fallback and reuse the collector's raw buckets. Remove with `--remove`.
* **Fifth fragility component and Rule 4.3 history:** Deribit DVOL daily index backfilled to
  2021-03 (verified paging), VRP history = (DVOL/100)² − RV²₃₀ from daily closes; the live
  fragility index now has all five components with 250-day windows, and Rule 4.3 is evaluated
  walk-forward so its hit rate carries a sample.
* **Vol-state model:** fitted on non-overlapping weekly realised vol (the overlapping 20-day
  series gave an AR coefficient of one); durations are now in weeks.
* **Tiering:** the Tier 1 depth criterion uses the measured 2 % depth once the liquidity table
  exists (`depth_status = measured`), and the Tier 2 ADV uses wash-filtered volume
  (`adv_basis = wash_filtered`) where available.
* **Unlocks:** per-protocol DefiLlama detail (exact daily amounts by label and class) replaces
  the even-spread linear approximation for Tier 1/2 names, refreshed weekly.
* Still pending by nature: Rule 4.2's liquidation-volume percentile needs 30 days of OKX
  liquidation history (accrues from go-live); a FRED key (user); the hand review of the
  seeded sector map (user).
* Effect of the measured depth screen on 2026-09-07: Tier 1 falls from 20 to 11 names
  (BTC, ETH, XRP, SOL, ZEC, DOGE, BNB, HYPE, XAUT, NEAR, UNI). The books behind the screen
  come from Binance (mirror), OKX, Coinbase and Kraken only — Bybit is missing until the
  collector supplies it — so several names sit just under USD 3M and will re-enter when the
  fifth venue is counted. The threshold itself is the notes' "several million" and is unchanged.
* Rule 4.3 walk-forward: 170 flags since 2021 (VRP < 0 and Φ > 1), 168 with an elapsed
  30-day horizon, 19 % followed by realised vol above the implied vol at the flag. Reported
  as is; the rule's action is a veto on short vol, not a forecast.

## 2026-09-07 — Lecture notes: implementation appendix
* Section 13 added to the notes (`docs/notes/`): free-source availability table and its
  limits, runner geo-blocking and the venue-consistency requirement for aggregated statistics,
  measurement details (depth lower bounds and trimming, relative Benford on trade sizes,
  walk-forward z-scores with sample sizes, missing fragility components, weekly-RV switching
  model, tiering with measured depth, sector seeding), validation on the history that exists
  (partial-input rule variants, Rule 4.3 full walk-forward, screen ICs), governance in practice.

## 2026-09-08 — Audit work order (A1–A10, B1–B2, C1–C2, D)
No threshold in `config/thresholds.yaml` changed; none of these items is a calibration.
* **A1 — commodity tokens out of the tiers.** XAUT, PAXG and every CoinGecko "Tokenized
  Gold/Silver/Commodities/Stocks/Treasuries/…" or "Commodity-backed Stablecoin" member is
  excluded with reason `commodity-backed / tokenised traditional asset`
  (`config/universe.yaml: tokenised_asset_category_regex`). They no longer appear in
  positioning, rules, trades or screens. Tier 1 on 2026-09-08 loses XAUT.
* **A2 — drawdown component.** Kept the notes' definition (current close against the 90-day
  high, not the minimum over the window) and fixed the sign: being at the high is the fragile
  reading (z_dd positive), a deep drawdown is the calm one. The audit's alternative (largest
  drawdown inside the window) is not used; it measures a different thing (what has already
  flushed), and the notes' definition measures where the liquidation mass sits now.
  Tile label reads "drawdown z (90-day high)" with sign-aware readings.
* **A3 — one fragility builder.** `compute/fragility.py` builds the daily component grid,
  robust z-scores and Φ for both the live row and the history (`fragility_series`); the
  live value is the last row of the same series, so history and live agree to 1e-6 on every
  common date. Each component carries `<c>_age_days` (0 = today, 1 = carried one period,
  null = missing); nothing is carried further than one period.
* **A4 — open-interest history and the −30 % five-day change.** The only OI series that is
  both historical and reachable from every runner is OKX `rubik/stat/contracts/open-interest-volume`
  (all contracts of a currency; 1D for 180 days, 1H for 30 days). It now feeds every OI
  statistic (percentile, five-day change, quadrant, liquidation density) through a daily grid
  (`oi_daily`), with `suspect = true` on any |Δlog OI| > 0.4 step. **The −30 % five-day change
  reported for BTC on 2026-09-07 was an artefact:** the live figure was the USDT-swap-only OI
  of one instrument (≈ 2.1 bn) compared against a history built from the all-contracts rubik
  series (≈ 2.85 bn). On the consistent series the five-day change was within a few percent.
* **A5 — funding carry needs 30 days.** The merged funding series (backfilled history plus live
  rows) is written back to `funding_daily`, so every Tier 1 name with history has n ≥ 30 and
  the funding-carry structure is populated instead of "insufficient history".
* **A6 — backwardation and the basis term structure.** When the annualised basis is negative
  the trade table shows the reverse carry (long the dated future, short spot or perp; cost =
  fees plus funding paid on a short perp; dominant risk a short squeeze and venue exposure on
  both legs). Panel 3 gains the current basis term structure across venues and expiries and a
  two-year basis history from Binance COIN-M continuous quarterly klines against the index
  (`basis_history`, 2020-06 onward, verified endpoints).
* **A7 — event strip.** Cliffs are listed only when they meet either Rule 5.1 leg (share of
  float > 1 % or > 2 days of volume); options expiries only on the monthly/quarterly (last
  Friday) dates or when the expiry holds ≥ `events.expiry_oi_share_min` (10 %) of open
  interest. Both numbers are configuration, not rule thresholds.
* **A8 — Rule 5.1 backfill.** `compute/cliff_study.py`: 2 668 scheduled cliffs since 2021 with
  price coverage (`cliff_study_events`), hit rate = negative 14-day pre-cliff return, mean and
  median pre/post returns by recipient class, float-share bucket, days-of-volume bucket and
  year, plus the unconditional base rate of negative 14-day returns on the same assets and
  period. Float at the cliff date is backed out of today's circulating supply through the
  schedule (flagged); ADV uses exchange quote volume restricted to venues passing the current
  wash filters where a wash row exists, flagged `unfiltered` otherwise. Tables on the methods
  page; the rule's thresholds are unchanged.
* **A9 — ages.** Table ages are computed from the row's `fetched_at` for date-keyed tables and
  clamped at zero; the status page can no longer print a negative age.
* **A10 — IC sentence.** The screens panel text is generated from the numbers (IC, standard
  error, ratio) and says when the ratio exceeds three; the caveat that screens condition
  attention and are not forecasts is kept regardless of the trailing IC.
* **B1 — liquidations from more than one venue.** `monitor.fetch.liq_ws` is a resident
  websocket collector (Binance `!forceOrder@arr`, Bybit `allLiquidation.*`) writing hourly raw
  envelopes; `scripts/liq_collector.sh` and a `KeepAlive` launchd agent
  (`com.wernerhl.crypto-monitor.liq`, installed by `install_collector_macos.sh`) keep it up on
  the collector Mac. The hourly compute parses both into `liquidations` next to the OKX REST
  sample and `liq_source` names the venues actually present ("okx (single-venue sample)"
  until the collector has run). Rule 4.2's liquidation percentile still needs 30 days of
  sample; it accrues from the collector's start.
* **B2 — example-book banners.** Panels 1 and 2 carry an EXAMPLE BOOK banner: the positions
  are the illustrative `config/book.yaml`, the scores, limits, pegs, covariance and vol
  state are live.
* **C1 — state reading.** A deterministic paragraph at the top of the front page
  (`compute/reading.py`, regenerated with hourly.json): Φ and its two largest components,
  VRP signs, the front-basis sign, the OI percentile, the 90-day and cycle drawdowns,
  30-day stablecoin growth, rules firing, venue breaches; every number links to its panel.
* **C2 — alerts.** `monitor alerts` (run by the hourly and daily workflows with the
  repository token) opens one GitHub issue per condition — a rule firing, a venue-limit
  breach, a dataset unavailable for two consecutive runs — labelled `alert`, and closes it
  with a comment when the condition clears; the same items are published as RSS at
  `site/alerts.xml`. State is in the `alerts` table.
* **D — pre-unlock drift note.** `docs/notes/pre_unlock_drift.md` reports the A8 study with
  tables and figures; conclusions are stated with their sample sizes and the base rate.
* Also fixed while verifying: the front page and the alerts took the rule evaluations from a
  single timestamp, so whichever job ran last (hourly for 4.x, daily context for 5.1) hid
  the other's rows; both now take the latest evaluation per (rule, asset) within 48 hours.
  The trade table is replaced per `as_of` instead of merged, so a basis that flips sign no
  longer leaves the old structure beside the new one. The basis (trade table and term
  structure) uses the venue's index price at the mark's timestamp; the previous daily close
  was up to a day stale and showed a false ETH backwardation of several hundred percent
  annualised on the near expiries.
* Operational: two overlapping local recomputes on the sync-agent-managed clone corrupted
  `rule_fires.parquet` twice (torn writes); the table was rebuilt from its archive partitions.
  Local recomputes now run one at a time from the clone outside the synced folder
  (`docs/runbook.md`).

## 2026-09-08 — Work order 2 (after commit 340e04e)
No threshold in `config/thresholds.yaml` changed. Item 2 is a definition change and is
recorded as such here, in `docs/indicators.md`, on the methods page and in the notes
(Section 13, "Revisions after the first audits").
* **1 — VRP component regression (fixed).** The A3 refactor read `vrp_history`, a table
  the backfill wrote once; the one-day carry hid it for a day, then `z_vrp_neg` went null
  and Φ was published on four components (0.65 instead of ≈ 1.09 on 2026-09-08 11:10Z).
  The builder now derives VRP every run from the daily DVOL history plus today's live DVOL
  against RV²₃₀ (`live_vrp_history`, written back to `vrp_history`). Per-component sample
  sizes (`z_<c>_n`, finite input days in the window) are restored. A component that is
  null while its source table is fresh is a build failure (`check_components` raises and
  the hourly job fails); only a stale source degrades to "n of 5", and the state reading
  prints "component X unavailable (reason)" in the alert style. Regression test: five fresh
  synthetic sources must give `n_components = 5` with sizes. Live after the fix:
  5 of 5 components, `z_vrp_neg = +2.33` (n = 250), Rule 4.3 evaluated on the restored Φ.
* **2 — Fourth component: 90-day range position (definition change).** Old: robust z of
  DD₉₀ = P/max₉₀ − 1 (sign fixed under work order 1). It is bounded at zero and, over a
  downtrending 250-day window, made any day near the high a tail event: +2.53 for a price
  2.7 % below the high on 2026-09-08. New: pos₉₀ = (P − min₉₀)/(max₉₀ − min₉₀),
  z_dd := 4·(pos₉₀ − ½) ∈ [−2, 2], no standardisation. Same mechanical reading (liquidation
  mass sits below when the price is at the top of the recent range), bounded contribution,
  no dependence on the standardisation window. History rebuilt.
  **Φ before → after on the 3,033 dates with both values: mean |Δ| 0.354, max |Δ| 2.94, the
  state word (calm/building/fragile) differs on 688 dates (first 2018-07-21, last
  2026-09-04).** Live on 2026-09-08: z_dd = +1.62 for pos₉₀ = 0.905 (the price is 2.7 %
  below the high but the 90-day range is about 28 % wide, so the close sits near its top;
  the order's "near 0 to +1" assumed a 5 % range).
* **3 — Cliff study statistics and framing.** Correction first: the work-order-1 study defined the "Rule 5.1 subset" with both legs (share > 1 % AND > 2 days of volume, n = 311); the rule itself fires on either leg (`rules.cliff`, `a or b`), so the subset is now the OR set and the tables and note are regenerated on it. `compute.cliff_study` now reports, per group,
  standard errors clustered by cliff week (pooled 2.6 % against 1.0 % i.i.d.; 2025 4.0 %),
  a β-adjusted pre-cliff return using the factor model's rolling β_MKT as of the cliff
  date, a placebo of 20 pseudo-cliff dates per token and year with the same treatment,
  base rates on all tokens and on the Rule 5.1 tokens, and by-year rows first. The note
  (`docs/notes/pre_unlock_drift.md`) leads with the year table: 2025 carries the pooled
  result and its placebo shows the tokens with cliffs were falling on ordinary days too;
  2026 is at the base rate by hit rate and BTC-relative drift; a β-adjusted residual of
  about four points remains in both years at under two clustered standard errors. The
  unlock-short rows in the trade table carry "2026 pre-cliff drift ≈ base rate" as
  dominant risk; Rule 5.1 stays informational; thresholds unchanged.
* **4 — Alert noise.** Rule 5.1 no longer opens one issue per token: one rolling issue
  "Cliff calendar, next 30 days (N qualifying)" carries the list as a table and is edited
  in place when the list changes; the 15 per-token issues are closed as superseded.
  Per-condition issues remain for Rules 4.1–4.3, venue-limit breaches and datasets
  unavailable for two runs. Same grouping in `site/alerts.xml` and in the state reading.
* **5 — Liquidation collector coverage.** The collector records connected seconds per
  venue and hour in the envelope meta; `liq_coverage` (hour, venue, connected_share,
  n_messages) is parsed from it. Rule 4.2's 30-day percentile uses only venue-hours with
  coverage ≥ 0.9; the triggers panel shows the sample's venues and "collector coverage:
  X % of the last 30 days' hours". `liq_source` lists only venues with rows in the window.
  Bybit subscribes to the Tier 1 list from the universe (was the fixed default of 10).
  **Binance finding:** `fstream.binance.com` accepts the websocket from this machine
  (HTTP 101) and then sends nothing on any futures stream (`!forceOrder@arr`,
  `btcusdt@forceOrder`, `btcusdt@aggTrade`, `btcusdt@markPrice`), while the spot stream
  and REST fapi work. The parser is verified on fixtures; the label therefore reads
  "bybit+okx", and Binance hours appear in `liq_coverage` as connected with zero messages.
  Runbook: a sleeping machine produces holes, holes are shown and not interpolated.
* **6 — Housekeeping.** The clone under the synced Documents folder is marked read-only
  (`chmod -R a-w`), and `archive.upsert` refuses to run from any clone under a cloud-synced
  path (`paths.assert_not_synced`; `MONITOR_ALLOW_SYNCED=1` overrides).
  The "3,176 common dates" in the A3 check counted the full `fragility_series` and
  `fragility_history` tables (2017-12-29 to 2026-09-08, 3,176 calendar days, 3,062 with a
  Φ value); `history.json` carries only the last 730 days, hence 731 rows there.
  Rule 4.3 historical n: 168 (build of 2026-09-07, original drawdown sign) → 91 (11:10Z
  build, after the A2 sign fix lowered Φ on many dates) → 76 (this build, range position);
  fired asset-days 107 → 85 between the last two. The count follows the Φ definition and
  is reported, not tuned.

## 2026-09-08 — Work order 3 (after commit 06c1fba)
* **Review decision 1 (2026-09-08): Rule 5.1 reclassified as calendar; unlock-short
  structure retired.** Exhibit: [docs/notes/pre_unlock_drift.md](notes/pre_unlock_drift.md).
  Reason: mechanism claim not distinguishable from placebo on 2,645 events (as implemented,
  either leg, n = 759: hit rate 54.5 % vs 56.2 % base rate on the same tokens; pooled placebo
  59.3 % vs 59.5 % real; β-adjusted pre-cliff residual ≈ 4 points at under two week-clustered
  s.e., not larger for flagged cliffs than for cliffs meeting neither leg). `rules.cliff`
  keeps computing with status `calendar` and writes `cliff_calendar` (its own table); it
  feeds the event strip, ESP, dilution, the liquidity gate and the rolling calendar issue;
  it has no hit-rate row and does not appear under "Rules firing" or on the triggers panel.
  `trades.unlock_short` is kept in code, marked retired, and no longer built into panel 5.
  The `cliff` block in `config/thresholds.yaml` is unchanged in value and re-labelled as
  calendar thresholds. Recorded in the notes' implementation appendix against Section 5 and
  Rule 4.
* **Rule 4.3 by driver (review item opened 2026-09-08, decide at the December 2026
  quarterly review).** Hit, as implemented and now stated on the methods page: realised vol
  over the next 30 days exceeds IV₁ₘ at the flag. Every historical flag is classified at the
  flag as complacency (IV₁ₘ below its trailing 250-day median), post-shock (RV₃₀ above its
  trailing 250-day 90th percentile), both, or neither; hit rate, mean and median
  (RV₃₀,next − IV_flag), flags and independent episodes (≥ 30 days apart) per class are on
  the methods page (`rule43_drivers`, weekly). BTC, 2021–2026: 84 flags in 10 episodes;
  complacency 20.8 % (n 24, 7 episodes), post-shock 17.9 % (39, 3), both 85.7 % (7, 3),
  neither 50 % (6, 3). The live 4.3 row carries the driver text ("post-shock, RV at the 78th
  percentile" / "implied vol at the 34th percentile") on the triggers panel and in the
  reading. No threshold changed; the review item is on the weekly issue checklist.
* **Disjoint job write sets; no automatic merge resolution.** `config/job_writes.yaml` lists
  the tables and site files each job may write; `scripts/check_write_set.py` asserts the
  staged files before every bot commit (workflows and `scripts/collector.sh`).
  Consequences: the hourly job owns perp snapshots, OI (rubik), liquidations, the derived
  hourly tables, `hourly.json`, alerts and `alerts.xml`; the daily job owns the context
  tables, `cliff_calendar`, hit rates, `daily.json`, `history.json`, status/universe JSON
  and `build.json`; the weekly job owns the universe and tier freeze, the factor model,
  the risk panels (`risk.json`, so venue/book/trade panels refresh weekly), the cliff study,
  the 4.3 driver table and the Φ validation. Fetch status is one table per job
  (`fetch_status_<job>`; the status page and alerts read them all). `-X theirs` is removed
  everywhere; the commit step rebases plainly and fails the run naming the conflicting
  files; CI fails on any `-X theirs|ours` in a workflow or script; `manual.yml` is in the
  `data-write` concurrency group. The 48-hour no-conflict acceptance runs from this
  commit.
* **Collector placement test (item 4): not run.** No permitted-region VPS exists in this
  environment (no SSH hosts; the AWS CLI is configured with root credentials and no
  instance). Provisioning a paid instance is a decision for the owner, so
  `scripts/collector_placement_test.sh` is provided instead: it runs the websocket
  collector for one hour and reports rows per venue. Until it is run, the finding stands
  that Binance's futures websocket is silent from this machine and `liq_source` lists only
  venues with rows.
* **Φ validation after shocks (item 5).** `compute.fragility_validation` and
  `scripts/fragility_validation_note.py` → [docs/notes/fragility_validation.md](notes/fragility_validation.md):
  shock days (BTC daily log return below its rolling 250-day 5th percentile), Φ₍t−1₎ and
  components, 5/20-day maximum drawdown, next-20-day realised vol, days to recover
  (censored at 60), Newey–West slopes, tercile tables, a Φ-matched placebo. Summary
  paragraph on the methods page. **Result on 147 BTC shock days (2018-06 to 2026-06; 96
  with three or more components): no support — three outcomes null, the 5-day drawdown
  opposite in sign.** Mean 20-day drawdown −15.9 % in the low-Φ
  tercile against −14.3 % in the high-Φ tercile (placebo −7.7 % / −6.0 %); next-20-day RV
  72.9 % against 68.7 %; median recovery 24 against 19 days, recovered within 60 days
  63 % against 78 %. No outcome moves with pre-shock Φ in the claimed direction at |t| ≥ 2;
  the 5-day drawdown moves against it (t = +2.3, smaller drawdown with higher Φ). Recorded
  as evidence for the next review; no change to Φ or any threshold. A first run had 266
  "shocks" because NaN warm-up thresholds compared as true; fixed (`shock_days`) and
  covered by a test.
* **Numerical guard in the robust z (found by item 5).** In November 2018 the stablecoin
  component read −684,201 (Φ −342,101) because the 250-day window of stablecoin growth was
  degenerate (MAD ≈ 0 against a 394 % growth reading). `robust_z` now returns undefined when
  the robust scale is below 5 % of the window's ordinary standard deviation; the component
  counts as missing on those 30 days. The formula is otherwise unchanged; the +11.7 VRP
  reading of February 2026 is genuine (RV variance 0.66 against a MAD of 0.02) and stays.

## 2026-09-08 — Work order 4 (after commit 0d9ff56)
* **Review decision 3 (2026-09-08): Φ retained as a positioning summary; predictive claim
  withdrawn pending leverage-component history.** Exhibit:
  [docs/notes/fragility_validation.md](notes/fragility_validation.md) (147 shock days, the last on
  2026-06-05; on the walk-forward series the funding z exists only from 2026-07 and the
  OI/cap z from 2026-04, so they were available on 0 and 3 of the shock days: the leverage
  components are untested rather than refuted; the range-position
  component reads as a bull-regime marker in this sample). State words are descriptive:
  DELEVERAGED / NEUTRAL / LEVERED on the same Φ thresholds (words only; no reweighting, no
  component removed). The gauge caption reads "positioning summary; not validated as a
  predictor of post-shock damage (see methods)" and the validation paragraph (n, component
  availability on shock days, the confound) sits under the gauge. A gated Coinglass adapter
  (`fetch/coinglass.py`, key optional, `COINGLASS_API_KEY`) is present for aggregated OI,
  OI-weighted funding and liquidation history back to 2021, with fixtures on the documented
  response shapes and a live test that is skipped without a key; with a key,
  `backfill.rebuild_leverage_history` writes the `coinglass_*` tables and the fragility
  builder extends the leverage inputs backwards so the validation can be re-run. Until then
  the adapter is inert. The endpoints are documented, not live-verified (no key).
* **Review decision 2 (2026-09-08): Rule 4.3 action removed; premise (negative VRP precedes
  vol expansion) not supported: RV fell below IV in ~80 % of flags across drivers.**
  Exhibit: methods page table (complacency 27 flags / 7 episodes, 20.8 %; post-shock 39 / 3,
  17.9 %; both 7 / 3, 85.7 %; neither 11 / 3, 50 %). `rules.vol_underpricing` has status
  `reading`: it keeps evaluating with its driver, appears on the market-state panel as
  "VRP negative (driver: …)", and is off the triggers panel, off "Rules firing", off the
  alerts and out of the hit-rate table. `trades.vol_premium` has no FORBIDDEN gate; the
  vol-selling row is present whatever the sign of the VRP and its dominant-risk text
  states the driver and, for post-shock, "RV above IV is usually transient (hit 18 %,
  3 episodes)". No replacement rule. Review item on the weekly checklist: re-open when the
  IV-low-and-RV-spiking cell has 3 independent episodes. Threshold values unchanged; the
  block is relabelled as reading thresholds. Recorded in the notes appendix.
* **Write sets corrected (item 3).** Hourly: positioning, options, fragility, rules,
  liquidity, vol state, the basis and funding-carry rows (`trades_carry`) and `hourly.json`
  (which now carries the carry rows, so trades refresh hourly). Daily: context tables,
  hit rates, book risk, venue exposure, the vol-selling rows (`trades_vol`), `daily.json`,
  `risk.json` (venue, book, trades) and the site render. Weekly: universe, factors,
  screens, the studies and `screens.json` (screens and the book's factor exposure, split
  out of `risk.json`). `config/job_writes.yaml` and the assertion updated; the CI check on
  `-X theirs|ours` stays. The 48-hour no-conflict window restarts from this commit.
* **Lecture-notes corrections (item 5).** A dated "Corrections to the notes" subsection in
  the implementation appendix: §2.5 (negative VRP is a transient state, four flags of five
  followed by RV below IV, expansion only in the both cell on too few episodes), §6.2 /
  Definition 6.1 (Φ not validated as a predictor; leverage components have months of
  history; range position a bull-regime marker), §5 / Rule 4 (placebo, calendar), §11
  (removal of an unsupported claim on full-history evidence is within the review procedure
  and does not wait for the calendar; parameter fitting does). PDF recompiled.
* **Regression fixed (found 2026-09-09 while verifying work order 4).** The front-page
  script had a syntax error since commit `6d8a434` (work order 2's collector-coverage
  line), which left panels 3–7 empty on the live page while every JSON was correct; the
  JSON-only acceptance checks of work orders 2–3 did not catch it. Fixed;
  `scripts/check_site_js.py` (node --check on the rendered scripts) now runs in CI.
