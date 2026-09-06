# crypto-monitor

A risk-and-context monitor for a tiered crypto asset universe, built to the design in
*Monitoring a Crypto Asset Universe: A Risk-and-Context System for Portfolio Managers*
(lecture notes, September 2026). It tells a portfolio manager what a book is exposed to, surfaces
a small number of pre-committed trigger rules with their historical hit rates, and provides
context (venue risk, market state, scheduled supply, cross-sectional screens). It is **not an
alpha engine**: there is no composite score, no ranking by expected return and no buy/sell
language, for the reasons given in Section 1 of the notes.

**Live page:** https://wernerhl.github.io/crypto-monitor/ (placeholder during phase 1)

**NOTICE.** This system is a monitoring tool and not investment advice.

## Status
Phase 1 (skeleton, CI, Pages placeholder). See `docs/changelog.md` for the phase log.

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
  USD 100M; aggregated 2 % order-book depth above USD 3M. Full hourly monitoring.
* **Tier 2** — spot on at least two major venues with wash-filtered 30-day ADV above USD 10M.
  Daily positioning and supply, weekly screens.
* **Tier 3** — the remainder. Weekly screens only, treated as venture exposure.

Stablecoins, wrapped tokens and liquid-staking derivatives are excluded from every tier.
Membership is recomputed weekly and its history is stored so backtests use membership as of date.

## How to read the front page
Top to bottom, in the order fixed by the notes (Section 12): venue and counterparty panel;
book risk; market state (fragility index and components); active triggers with hit rates and
sample sizes; trade-structure table; four-week event strip; Tier 2/3 screens with IC and standard
error. Every panel shows `last updated` and the source used; stale or missing data is greyed with
the reason. Charts link to their CSV. `methods.html` restates the notes for each panel and links
to the code.

## Repository map
`docs/data_sources.md` (verified endpoints), `docs/indicators.md` (formula, source, test per
indicator), `docs/runbook.md` (what to do when a job fails), `config/` (universe, thresholds,
venues, example book, events), `src/monitor/` (fetch, schema, compute, rules, stress, site),
`data/` (raw immutable responses, processed parquet, monthly archive), `site/` (published).

## License
MIT.
