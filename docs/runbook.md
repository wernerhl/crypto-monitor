# Runbook

## Jobs
| job | schedule (UTC) | what it does | phase |
|---|---|---|---|
| `ci` | push / PR | ruff, pytest (live tests skipped), secret scan, repo-size check | 1 |
| `daily` | 01:25 | fetch aggregator + listings + perps + daily candles → compute tables → bot commit `[skip ci]` → render site → deploy Pages → verify live sha | 2 |
| `manual` | dispatch | `job` ∈ {daily, hourly, weekly, backfill}, `start_date` for backfills | 2 |
| `hourly` | :07 | funding, OI, books, liquidations, options → positioning, liquidity, fragility, rules | 3 |
| `weekly` | Sun 02:40 | tier freeze (`tier_history`), factor model, screen IC, rule hit rates, raw rotation to `data-archive`, size check, review issue | 5/7 |

Every fetch is idempotent per bucket: `data/raw/YYYY/MM/DD/<source>_<dataset>_<HHMM>.json.gz`
is reused if it exists (daily bucket `0000`, hourly bucket the floored hour). Re-running a
job on the same day therefore costs no requests and cannot duplicate rows; pass `--force`
(or the `force` input on `workflow_dispatch`) to refetch.

## What failure looks like and what to do
* **`daily` fails in "fetch daily datasets"** — the log names the adapter. Three causes:
  1. `SanityError` (row count, freshness, value range): the source returned something unexpected.
     Open the raw file it wrote (the path is in the log), compare with the sample in
     `tests/fixtures/live_verification/`, fix the parser, re-run with `force`.
  2. `SourceStaleError`: the endpoint's `verified_on` in `config/sources.yaml` is older than 120
     days. Run `make verify-sources`, check the diff of `summary.json`, update the date.
  3. HTTP 429/5xx after six backoff attempts: re-run later; if CoinGecko keeps rate-limiting, set
     the `COINGECKO_DEMO_KEY` secret (free demo key, 30 req/min).
  The aggregator step falls back to CoinPaprika automatically; the universe row then shows
  `source = coinpaprika` and `supply_source = derived`.
* **`daily` fails in "commit data"** — a push race with the hourly job; `concurrency: data-write`
  should prevent it. Re-run the workflow; the fetch step is free (raw files exist).
* **`verify-pages` fails** — Pages propagation lag (the step retries for 5 minutes). Re-run the
  workflow. If `data-build-sha` still differs, check that the Pages source is "GitHub Actions".
* **CI did not run on a push** — GitHub skips workflows when the commit message contains
  `[skip ci]` anywhere, including the body. Only the bot's data commits should carry it.
  `ci.yml` also has `workflow_dispatch` so it can be run by hand.
* **CI secret scan fails** — the named line looks like a key. Move it to an environment variable;
  for a false positive add `# not-a-secret` to the line.
* **Repo-size check fails** — rotate raw files (below).
* **Local: `ModuleNotFoundError: monitor`** — macOS marks files in `.venv` hidden and Python
  3.12 skips hidden `.pth` files. `make sync` clears the flag; the Makefile also sets
  `PYTHONPATH=src`. In CI (Linux) this does not occur.

## Re-running by hand
```bash
make fetch                 # today's raw files (skips ones that exist)
make compute               # replay every raw file into data/processed and data/archive
make site                  # render ./site
uv run monitor universe show --tier 1
```

## Raw retention and rotation
Daily raw files stay in the repo for 90 days; hourly raw files (books, trades, perps,
liquidations, options) stay for 14 days — at ≈ 0.7 MB per hourly bucket that is ≈ 240 MB,
which keeps the repo under the 800 MB check. Older files are moved by the weekly job into
monthly tarballs on the `data-archive` orphan branch (phase 7), so every processed row stays
traceable to its raw file. Processed parquet keeps a rolling 90–120-day window for hourly
tables and everything for daily tables; the monthly archive partitions keep everything.

## Adding a venue
1. Verify the endpoints live (`scripts/verify_sources.py`, add the entries, run it, commit the
   trimmed samples) and document them in `docs/data_sources.md`.
2. Add an entry to `config/sources.yaml` with `verified_on` and the observed limit.
3. Write `src/monitor/fetch/<venue>.py` with `fetch_listings`/`parse_listings` (and the perp /
   kline functions if applicable), returning the schema rows in `schema/tables.py`.
4. Add the venue to `PERP_VENUES` / `SPOT_VENUES` and `QUOTES` in `compute/universe.py`, to
   `jobs.py`, and to `config/venues.yaml` with its review fields.
5. Add a fixture test in `tests/unit/test_parsers.py`.

## Adding or overriding an asset
Assets enter through the aggregator top-N. To force inclusion or exclusion, edit
`config/universe.yaml → exclusions.manual` (`id: include` or `id: reason`). To fix a venue
symbol that the automatic resolver gets wrong, add `symbol_map: {<coingecko id>: {binance_perp: …}}`.
Record the change in `docs/changelog.md`.

## Replacing the example book
Edit `config/book.yaml`: `nav_usd`, `positions` (coingecko id, signed weight as a share of NAV,
venue, instrument), `collateral` (stablecoin, venue, usd) and `recovery_assumption_on_halt`.
The next daily run recomputes the venue panel, book risk and gate reference sizes; nothing
else reads the book. Keep the file out of public forks if the positions are real.

## Rate-limit budget (daily job, phase 2)
| source | requests per run | observed / documented limit | headroom |
|---|---|---|---|
| CoinGecko (no key) | 2 markets + ≤ 30 coin meta, spaced 12.5 s | 429 seen at 6.5 s spacing on 2026-09-06; ~5/min sustained is safe | ≥ 50 % |
| CoinPaprika (fallback only) | 1 (+ meta) | 20 000 / month | — |
| Binance spot | 1 exchangeInfo (w 20) + ~150 klines (w 2) ≈ 320 weight | 6 000 / min | > 90 % |
| Binance USDT-M | premiumIndex 10 + fundingInfo 1 + ticker 40 + ~150 OI × 1 ≈ 200 weight | 2 400 / min | > 90 % |
| Bybit | 2 instruments + 1 tickers + ~150 klines, 0.15 s spacing | 600 / 5 s documented; 40 burst OK | > 80 % |
| OKX | 3 instruments + 3 perps + ~150 candles, 0.15 s spacing | 20 / 2 s documented per endpoint; 40 burst OK | > 50 % |
| Coinbase | 1 products + ~80 candles | 10 / s documented | > 80 % |
| Kraken | 1 AssetPairs + ~80 OHLC | ~1 / s documented; 10 burst OK | > 80 % |
## Rate-limit budget (hourly job)
Measured on the first live hourly run (2026-09-06, Tier 1 = 22, Tier 2 = 29): ~450 requests in
under 3 minutes, all 200.
| source | requests per run | limit | headroom |
|---|---|---|---|
| Binance spot | 51 books (BTC/ETH 5000 levels = weight 250 each, others 1000 = 50) ≈ 2 950 weight + 22 trades × 25 = 550 → ≈ 3 500 / min worst case | 6 000 / min | ≥ 40 % (books are spread over ~30 s) |
| Binance USDT-M | premiumIndex 10 + fundingInfo 1 + ticker 40 + 22 OI + 22 long/short (separate limit) | 2 400 / min | > 90 % |
| Binance COIN-M | 2 | 2 400 / min | — |
| Bybit | 1 tickers + 51 books + 22 trades, 0.15 s spacing | 600 / 5 s documented | > 80 % |
| OKX | 3 perps + 22 funding + 51 books (2 books-full) + 22 trades + ≤ 110 liquidation pages + 2 futures | 20–40 / 2 s per endpoint | > 50 % |
| Coinbase | 51 books + 22 trades | 10 / s documented | > 80 % |
| Kraken | 51 books + 22 trades, 0.15 s spacing | ~1 / s documented | ≈ 50 % (Kraken is the slowest leg) |
| Deribit | 10 | 20 / s | > 95 % |
If the run exceeds 10 minutes the workflow fails with a scope message: drop Tier 2 books
first (they only feed the liquidity gate), then reduce trade sampling to three venues.
