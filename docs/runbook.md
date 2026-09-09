# Runbook

## Jobs
| job | schedule (UTC) | what it does | phase |
|---|---|---|---|
| `ci` | push / PR | ruff, pytest (live tests skipped), secret scan, repo-size check | 1 |
| `daily` | 01:25 | fetch aggregator + listings + perps + daily candles → compute tables → bot commit `[skip ci]` → render site → deploy Pages → verify live sha | 2 |
| `manual` | dispatch | `job` ∈ {daily, hourly, weekly, backfill}, `start_date` for backfills | 2 |
| `probe` | dispatch | reachability of every source host from a runner (status codes) | 5 |
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

## Geo-blocked venues on GitHub-hosted runners
Binance (api/fapi/dapi) answers HTTP 451 and Bybit HTTP 403 from US addresses, which is
where GitHub-hosted runners live (probe workflow, 2026-09-06). Binance spot uses the public
mirror `data-api.binance.vision` (200). Binance futures and Bybit datasets are recorded as
unavailable per run (`fetch_status`, status page) and the derived tables use OKX + Deribit
until a collector refreshes them. To get the full three-venue derivatives set, run
`scripts/collector.sh hourly` from a machine that can reach the venues (a small VPS outside
the US, or a workstation) on a cron; it fetches, computes, commits as the bot and pushes, and
the raw buckets are idempotent so it coexists with the Actions jobs. The mode-(b) websocket
collector for Binance `forceOrder` / Bybit `allLiquidation` liquidation streams is the same
script's natural home and is not implemented in this repository.

## Collector on a Mac (installed 2026-09-07)
`scripts/install_collector_macos.sh` installs launchd agents that run `scripts/collector.sh
hourly` at :09 and `daily` at 01:45 local time; logs in `~/Library/Logs/crypto-monitor-*.log`.
Run it from a clone **outside** `~/Documents`, `~/Desktop` and `~/Downloads` (macOS privacy
protection blocks launchd jobs there with "Operation not permitted"); on the build machine
that clone is `~/crypto-monitor`, with its own `.env`.
The machine must be awake (System Settings → Battery → prevent sleep, or `caffeinate`), have
push rights (`gh auth` is used by git) and the `.env` with keys. `scripts/install_collector_macos.sh
--remove` uninstalls. Both paths write the same idempotent raw buckets, so the Actions jobs
simply reuse whatever the collector already fetched.

Since 2026-09-08 the agents run from a dedicated clone, `~/crypto-monitor-collector`, that
nobody edits: development work in `~/crypto-monitor` (uncommitted changes) blocked the
collector's `git pull --rebase` for six hours, which left only runner (geo-blocked) fetch rows
and opened six dataset alerts. Geo-block responses (HTTP 451/403) from runners are ignored by
the dataset alert, which fires on real outages only.

The same installer also loads `com.wernerhl.crypto-monitor.liq`, a `KeepAlive` agent running
`scripts/liq_collector.sh` (`python -m monitor.fetch.liq_ws`): the resident websocket
collector for Binance `!forceOrder@arr` and Bybit `allLiquidation.*`. It writes one raw
envelope per venue and hour (`binance_liquidations_ws_HH00`, `bybit_liquidations_ws_HH00`),
merging into an hour already on disk after a restart; the hourly collector run commits them
and `monitor compute hourly` parses them into `liquidations`. Log:
`~/Library/Logs/crypto-monitor-liq.log`. `liq_source` on the positioning row names the venues
present; Rule 4.2's liquidation percentile needs 30 days of this sample.

**Coverage and holes.** The collector only sees what it is connected to. Each venue-hour's
connected share and message count go into `liq_coverage`; Rule 4.2's percentile uses hours
with coverage ≥ 0.9 and the triggers panel prints the covered share of the last 30 days. A
machine that sleeps produces holes; holes are shown, never interpolated. Run the collector on
a machine that does not sleep (a Mac with `caffeinate -s`, or any always-on box outside the
US address ranges the venues block). Binance's futures websocket (`fstream.binance.com`)
accepts the connection from this machine and sends nothing (verified 2026-09-08 on four
streams; the spot stream works), so Binance hours show as connected with zero messages and
`liq_source` lists only venues with rows.

## Collector placement test (2026-09-09, work order 4 item 4)
Two `t4g.nano` instances (Ubuntu 24.04 arm64, ≈ USD 3 per month each if kept) ran
`scripts/collector_placement_test.sh` for one hour from 00:38 UTC: eu-central-1 (Frankfurt,
3.71.106.251) and ap-southeast-2 (Sydney, 13.210.13.130). Result per venue and region:

| stream | Frankfurt | Sydney | Mac (collector) |
|---|---|---|---|
| Binance `fstream.binance.com` `!forceOrder@arr` (USDⓈ-M host) | connected 3,595 s of 3,600, 0 messages | connected 3,375 s, 0 messages | connected, 0 messages |
| Binance `fstream` `btcusdt@aggTrade` (control, 15 s) | 0 messages | not probed | 0 messages |
| Binance `dstream.binance.com` `!forceOrder@arr` (60 s) | 23 messages | not probed | 12 messages |
| Bybit `allLiquidation.*` (12 Tier 1 symbols) | 38 messages in the hour | 33 messages in the hour | delivers |
| OKX (REST `liquidation-orders`, hourly job) | not part of the websocket test | — | delivers |

Reading: the USDⓈ-M websocket host accepts the connection and sends nothing on any
stream from any of the three locations, including a control stream that publishes dozens
of messages per second; the COIN-M host `dstream.binance.com` carries the all-market
forced-order feed (USDT and COIN-M symbols) everywhere, the Mac included. Region is not
the variable. **Decision: keep the Mac collector, stop trying regions; the collector reads
Binance forced orders from `dstream` (COIN-M contracts converted from contract counts; USDT
symbols on the same feed are in base units). No systemd service, no VPS cost; both
instances were terminated after the test.** `liq_source` continues to list only venues with
rows in the window; Binance rows appear once the restarted collector has run an hour.

## Alerts (GitHub issues and RSS)
`uv run monitor alerts` (run by the hourly and daily workflows with `GH_TOKEN`) opens one
issue per active condition — a Rule 4.1–4.3 firing (`rule:<id>:<asset>`), a venue limit
breached on the example book (`venue:<venue>`), a dataset unavailable in two consecutive runs
of its job (`dataset:<name>`) — labelled `alert`, and closes it with a comment when the
condition clears. Rule 5.1 is informational and gets ONE rolling issue, "Cliff calendar,
next 30 days", whose body (the qualifying list as a table) is edited in place when the list
changes; open issues = active 4.x/venue/dataset conditions + 1. The same items are published at `site/alerts.xml` (RSS 2.0). `--dry-run` touches
no issues (used locally). State: the `alerts` table (`key`, `opened_at`, `closed_at`,
`issue_number`). To silence a class of alerts, close the issue and fix the condition; there
is no mute list by design.

## Job write sets (no shared files, no automatic merge resolution)
`config/job_writes.yaml` names the tables and site files each job may write; every bot
commit (workflows, `scripts/collector.sh`) runs `scripts/check_write_set.py <job>` on the
staged files and refuses anything outside the set. Hourly (work order 4 assignment): derived
hourly tables, the basis and funding-carry trade rows (`trades_carry`), `hourly.json`, alerts.
Daily: context tables, `cliff_calendar`, hit rates, book risk, venue exposure, the vol-selling
rows (`trades_vol`), `daily.json`, `history.json`, `risk.json` (venue, book, trades),
status/universe/build JSON, site render. Weekly: universe and tier freeze, factor model,
screens, the studies, `screens.json` (screens and the book's factor exposure). Fetch status is
one table per job (`fetch_status_<job>`).
The commit step rebases plainly; a conflict fails the run with the file names and is never
auto-resolved (CI rejects any `-X theirs|ours`). If a run fails with a conflict, the fix is
to move the file into exactly one job's set, not to pick a side.

## Re-running by hand
```bash
make fetch                 # today's raw files (skips ones that exist)
make compute               # replay every raw file into data/processed and data/archive
make site                  # render ./site
uv run monitor universe show --tier 1
uv run monitor alerts --dry-run
PYTHONPATH=src uv run --no-sync python scripts/unlock_drift_note.py   # docs/notes/pre_unlock_drift.md
```
**Run local recomputes one at a time, and from the clone outside the synced folder
(`~/crypto-monitor`).** The code enforces it: `archive.upsert` refuses to write from a clone
whose path contains `Documents`, `Desktop`, `Downloads`, `Library/CloudStorage`, `Dropbox`,
`Google Drive` or `OneDrive` (`MONITOR_ALLOW_SYNCED=1` overrides, knowingly). The old clone
under `~/Documents/whl._Trading/crypto-monitor` is read-only since 2026-09-08. The development clone under `~/Documents` is managed by a file-sync
agent: writes there are slow while it syncs (a 12-second `compute context` took 27 minutes)
and two overlapping recomputes tore `rule_fires.parquet` twice on 2026-09-08. A torn table
shows up as `parquet: File out of specification`; rebuild it from `data/archive/<table>/*.parquet`
(concatenate, `unique` on `archive.KEYS[table]`, write to `data/processed/`), or restore it
with `git checkout origin/main -- data/processed/<table>.parquet` and recompute.

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
