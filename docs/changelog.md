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
