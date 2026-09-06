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
