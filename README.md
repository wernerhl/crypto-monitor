# crypto-monitor

A risk-and-context monitor for a tiered crypto asset universe, built to the design in
*Monitoring a Crypto Asset Universe: A Risk-and-Context System for Portfolio Managers*
(lecture notes, September 2026; source and PDF in `docs/notes/`, with an implementation appendix
added on 2026-09-07 recording what free data supports, where the jobs can run, and how depth,
wash filters and the rules' validation had to be measured). It tells a portfolio manager what a book is exposed to, surfaces
a small number of pre-committed trigger rules with their historical hit rates, and provides
context (venue risk, market state, scheduled supply, cross-sectional screens). It is **not an
alpha engine**: there is no composite score, no ranking by expected return and no buy/sell
language, for the reasons given in Section 1 of the notes.

**Live page:** https://wernerhl.github.io/crypto-monitor/ (all seven panels populated; secondary pages: Universe, On-chain, Methods, Data status)

**NOTICE.** This system is a monitoring tool and not investment advice.

## Status
Phases 1–6 built and deployed on 2026-09-06; phase 7 hardening in progress. The run-based
acceptance tests (three unattended daily runs, 48 hourly runs) accrue over the coming days.
See `docs/changelog.md` for the phase log and deviations.

**Runner limitation.** GitHub-hosted runners are US addresses: Binance futures (451) and Bybit
(403) are geo-blocked there, Binance spot works through `data-api.binance.vision`. The Actions
jobs therefore run derivatives on OKX + Deribit; `scripts/collector.sh` refreshes the Binance
futures and Bybit feeds from any machine that can reach them (docs/runbook.md).

## Running it
Requirements: Python 3.12 and [`uv`](https://docs.astral.sh/uv/). No paid subscription is needed;
every indicator has a free-source fallback and the page shows which source produced each number.

```bash
git clone https://github.com/wernerhl/crypto-monitor && cd crypto-monitor
cp .env.example .env          # optional keys; the system runs with none
make all                      # sync deps, rebuild every processed table from raw files, render ./site
make test                     # unit + integration tests (live tests need LIVE=1)
```

Secrets (all optional, read from the environment / GitHub Actions secrets):
`FRED_API_KEY`, `COINGECKO_DEMO_KEY`, `COINGLASS_API_KEY`, `GLASSNODE_API_KEY`,
`BEACONCHAIN_API_KEY`, `TALLY_API_KEY`.

## Tiers (notes Section 2, thresholds in `config/universe.yaml`)
* **Tier 1** — perps on at least two of Binance, Bybit, OKX; 30-day median aggregate OI above
  USD 150M; measured 2 % order-book depth (intraday median across venues) above USD 3M. Full
  hourly monitoring.
* **Tier 2** — spot on at least two major venues with wash-filtered 30-day ADV above USD 10M.
  Daily positioning and supply, weekly screens.
* **Tier 3** — the remainder. Weekly screens only, treated as venture exposure.

Stablecoins, wrapped tokens and liquid-staking derivatives are excluded from every tier.
Membership is recomputed weekly and its history is stored so backtests use membership as of date.

## How to read the front page
Banners first: rules firing (red), venue limits breached (amber), datasets unavailable this run
(grey). Then a strip with the market state word (CALM / BUILDING / FRAGILE from the fragility
index), rules firing, BTC 1-month implied vol, the vol-state probability and the data age.
Panels follow the notes' order (Section 12): venue and counterparty; book risk; market state
(gauge, five component tiles with a one-line reading each, two-year charts); active triggers
(each input shown as a chip against its frozen threshold; a rule fires only when every chip is
red); trade structures (net annualised rates by structure, execution venue and grade); the
four-week event strip (cliffs sized by days of real volume); Tier 2/3 screens and the liquidity
gate. Every panel shows its source tables and timestamp; stale or missing data is greyed with the
reason; every table has a CSV link. `methods.html` restates the notes for each panel and links
to the code.

## Repository map
`docs/data_sources.md` (verified endpoints), `docs/indicators.md` (formula, source, test per
indicator), `docs/runbook.md` (what to do when a job fails), `config/` (universe, thresholds,
venues, example book, events), `src/monitor/` (fetch, schema, compute, rules, stress, site),
`data/` (raw immutable responses, processed parquet, monthly archive), `site/` (published).

## License
MIT.
