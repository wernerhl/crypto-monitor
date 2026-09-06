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
