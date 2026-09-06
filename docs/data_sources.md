# Data sources

Every endpoint used by an adapter is listed here with the date it was last verified by a
live request, the fields the adapter reads, the observed rate-limit behaviour and the
sample stored under `tests/fixtures/live_verification/` (samples are truncated to 20
elements per list; `summary.json` there holds status, latency, size and headers for every
request). Re-run `python scripts/verify_sources.py` before changing any adapter and update
the `verified_on` date in `config/sources.yaml`.

Status legend: **verified** = live 200 with the fields below on the stated date;
**unavailable** = no free, official source found (dashboard shows the marker);
**optional** = adapter activates only when the named key is in the environment;
**rejected** = exists but not used, with reason.

Ground rule (build prompt 0.1): nothing here is hard-coded from memory. Where a vendor
publishes a limit that the live test could not reach, the table says "documented" and
gives the observed burst that did *not* trigger throttling.

Last full verification: **2026-09-06** (81 endpoints, 70 returned 200; the 11 others are
explained below).

---

## 1. Prices, volume, market cap, supply

### CoinGecko (primary aggregator) — verified 2026-09-06
| item | value |
|---|---|
| `coins/markets` | `GET https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page={1,2}&sparkline=false` → list of 250 per page (page 2 verified). Fields used: `id, symbol, name, current_price, market_cap, market_cap_rank, fully_diluted_valuation, total_volume, circulating_supply, total_supply, max_supply, last_updated`. |
| `coins/{id}` | `GET .../coins/{id}?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false` → `categories` (list of strings), `asset_platform_id`, `platforms`. Used once per asset to seed `sector_map` and to detect wrapped/LST tokens. |
| `coins/categories/list` | 868 categories, fields `category_id, name`. |
| `coins/markets?category=` | `?vs_currency=usd&category={stablecoins|wrapped-tokens|liquid-staking-tokens|liquid-restaking-tokens|tokenized-btc|bridged-tokens}&per_page=250&page=1` → same row shape as `coins/markets`, verified for `stablecoins` (250 rows: tether, usd-coin, usds, dai, …) and `wrapped-tokens` (wrapped-steth, wrapped-bitcoin, weth, …). One call per category gives the exclusion lists for every candidate on the first run; per-coin `coins/{id}` tags then refine sector seeding at 30 ids per day. |
| `coins/{id}/market_chart` | `?vs_currency=usd&days=365&interval=daily` → `prices, market_caps, total_volumes` as `[ms, value]` pairs (366 rows). **`days=max` returns 401, error 10012: public users limited to 365 days.** Backfill beyond 365 days uses CoinPaprika / exchange klines / CoinMetrics. |
| auth | none required. Optional demo key sent as header `x-cg-demo-api-key` (`COINGECKO_DEMO_KEY`); the key itself was not tested. |
| rate limit (observed) | burst of 20 requests: 6 × 200 then 14 × **429 with `Retry-After: 60`**. No `x-ratelimit-*` headers. Adapter spaces calls 12.5 s apart (≈ 5/min; a 429 still appeared at 6.5 s spacing during the first daily run on 2026-09-06) and sleeps `Retry-After` on 429. |
| sample | `coingecko.markets.json.gz`, `coingecko.coin.json.gz`, `coingecko.market_chart.json.gz`, `coingecko.market_chart_max.json.gz` (the 401 body), `coingecko.markets_by_category.json.gz` |

### CoinPaprika (fallback aggregator) — verified 2026-09-06
| item | value |
|---|---|
| `tickers` | `GET https://api.coinpaprika.com/v1/tickers?limit=250` → `id, symbol, rank, total_supply, max_supply, first_data_at, quotes.USD.{price, volume_24h, market_cap}`. **No circulating supply field**; float falls back to `market_cap / price`, flagged `supply_source=derived`. |
| `coins/{id}` | `tags[].id` (e.g. `layer-1-l1`, `proof-of-work`), `type` (`coin`/`token`). |
| rate limit (observed) | headers `ratelimit-limit: 20000`, `ratelimit-remaining`, `ratelimit-reset ≈ 2.2e6 s` → **20 000 calls per ~month** per IP. Burst of 10 fine. |
| sample | `coinpaprika.tickers.json.gz`, `coinpaprika.coin.json.gz` |

### Exchange spot candles — verified 2026-09-06
| venue | endpoint | shape | limit |
|---|---|---|---|
| Binance | `GET https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000` | list of 12-element arrays `[openTime, o,h,l,c, vol, closeTime, quoteVol, trades, takerBuyBase, takerBuyQuote, ignore]` | weight 2 per call; `REQUEST_WEIGHT 6000/min` (from `exchangeInfo.rateLimits`, verified) |
| Bybit | `GET https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=60&limit=1000` | `result.list[]` = `[start, o,h,l,c, vol, turnover]` strings, newest first | no headers; 40-request burst unthrottled |
| OKX | `GET https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=1H&limit=300` and `.../history-candles` for older | `data[]` = `[ts, o,h,l,c, vol, volCcy, volCcyQuote, confirm]`, newest first | no headers; 40-request burst unthrottled |
| Coinbase Exchange | `GET https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600` | `[time_s, low, high, open, close, volume]` (350 rows) | 15-request burst unthrottled |
| Kraken | `GET https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60` | `result.XXBTZUSD[]` = `[time_s, o,h,l,c, vwap, vol, count]` (721 rows) | 10-request burst unthrottled |

Daily bars (verified 2026-09-06): Binance `interval=1d&limit=1000` (1000 rows, UTC days); Bybit `interval=D&limit=1000`; OKX `bar=1Dutc` (plain `1D` is aligned to UTC+8 — verified by timestamp — so `1Dutc` is used; `history-candles` for older); Coinbase `granularity=86400`; Kraken `interval=1440`.

Listings (verified 2026-09-06): Binance `api/v3/exchangeInfo` (3692 symbols; `status`, `baseAsset`, `quoteAsset`, `isSpotTradingAllowed`) and `fapi/v1/exchangeInfo`; Bybit `instruments-info?category=spot` (538) and `linear` (855); OKX `instruments?instType=SPOT|SWAP|FUTURES` (1389 / 473 / 194; one swap had an empty `instFamily` and pre-open instruments carry an empty `ctVal` — both skipped); Coinbase `/products` (837; `status`, `trading_disabled`); Kraken `AssetPairs` (1446; `wsname`, `altname`, `status`).

`ccxt` 4.5.77 released 2026-09-01 (PyPI verified) — maintained. Adapters call the REST
endpoints above directly with `httpx` so that raw responses are stored verbatim; `ccxt`
is **not** a dependency (deviation from the build prompt's suggestion, reason: raw-file
traceability and one fewer 10 MB dependency; symbol normalisation is done in
`config/universe.yaml`).

## 2. Derivatives

### Binance USDT-M futures — verified 2026-09-06
| endpoint | fields used | notes |
|---|---|---|
| `GET https://fapi.binance.com/fapi/v1/premiumIndex` | per symbol `markPrice, indexPrice, lastFundingRate, interestRate, nextFundingTime, time` (898 symbols) | weight 10 for all symbols |
| `GET .../fapi/v1/fundingRate?symbol=X&limit=1000` | `fundingTime, fundingRate, markPrice` | returned 500 rows for limit=1000; supports `startTime/endTime` paging (docs, to confirm in backfill) |
| `GET .../fapi/v1/fundingInfo` | `symbol, adjustedFundingRateCap/Floor, fundingIntervalHours` (778 symbols) | funding interval is **not always 8h**; annualisation uses this field |
| `GET .../fapi/v1/openInterest?symbol=X` | `openInterest` (contracts = base units), `time` | weight 1 |
| `GET .../futures/data/openInterestHist?symbol=X&period=1h&limit=500` | `sumOpenInterest, sumOpenInterestValue, timestamp` | 30-day window only; no weight header (separate limit) |
| `GET .../fapi/v1/exchangeInfo` | `symbols[].{symbol, contractType, deliveryDate, status}`; `rateLimits` | contractTypes seen: PERPETUAL 700, TRADIFI_PERPETUAL 191, CURRENT_QUARTER 2, NEXT_QUARTER 2 (`BTCUSDT_260925`, …) |
| `GET .../fapi/v1/ticker/24hr` | `quoteVolume, lastPrice` for all perps | weight 40 |
| `GET .../fapi/v1/allForceOrders` | **404 — removed.** No REST liquidation feed on Binance. | see liquidations below |
| `GET https://fapi.binance.com/futures/data/topLongShortPositionRatio`, `takerlongshortRatio` | verified 200; context only | |
| rate limit | `REQUEST_WEIGHT 2400/min` (from `exchangeInfo`), header `x-mbx-used-weight-1m` | 20-request burst → weight 19 |

### Binance COIN-M — verified 2026-09-06
`GET https://dapi.binance.com/dapi/v1/premiumIndex` → `BTCUSD_PERP, BTCUSD_260925 (CURRENT_QUARTER), BTCUSD_261225 (NEXT_QUARTER)` with `markPrice, indexPrice`; `dapi/v1/exchangeInfo` gives `deliveryDate`. Limit 2400/min. Used for BTC/ETH basis alongside USDT-M quarterlies.

### Bybit v5 — verified 2026-09-06
| endpoint | fields used |
|---|---|
| `GET https://api.bybit.com/v5/market/tickers?category=linear` | 859 rows: `symbol, lastPrice, indexPrice, markPrice, openInterest, openInterestValue, turnover24h, fundingRate, nextFundingTime, deliveryTime, basisRate` |
| `GET .../v5/market/funding/history?category=linear&symbol=X&limit=200` | `fundingRate, fundingRateTimestamp` (200 rows, `startTime/endTime` paging) |
| `GET .../v5/market/open-interest?category=linear&symbol=X&intervalTime=1h&limit=200` | `openInterest, timestamp` (contracts) |
| `GET .../v5/market/instruments-info?category=linear&limit=1000` | 855 rows: `contractType ∈ {LinearPerpetual 815, LinearFutures 40}`, `deliveryTime, fundingInterval, leverageFilter.maxLeverage`, `nextPageCursor` |
| rate limit | no headers on public endpoints; 40 requests in 1.5 s all 200. Bybit documents 600 req / 5 s per IP for market endpoints (documented, not reached). Bybit has **no REST liquidation endpoint** (`/v5/market/liquidation` → 404; websocket only). |

### OKX v5 — verified 2026-09-06
| endpoint | fields used |
|---|---|
| `GET https://www.okx.com/api/v5/public/funding-rate?instId=X-USDT-SWAP` | `fundingRate, fundingTime, nextFundingRate, maxFundingRate, minFundingRate, premium, interestRate, settFundingRate` |
| `GET .../public/funding-rate-history?instId=X&limit=100` | `fundingRate, realizedRate, fundingTime` (paging `after/before`) |
| `GET .../public/open-interest?instType=SWAP` | 472 rows: `instId, oi (contracts), oiCcy (base), oiUsd, ts` |
| `GET .../rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H` | 720 rows `[ts, oiUsd, volUsd]` (30 days) |
| `GET .../public/instruments?instType=FUTURES` | 194 dated futures incl. `BTC-USD-260925, BTC-USD-261225, BTC-USD-270326 …` with `expTime, ctVal, ctValCcy, ctType` |
| `GET .../public/instruments?instType=SWAP` | 473: `ctVal, ctValCcy, ctMult, settleCcy, lever` — needed to convert `oi` contracts to USD |
| `GET .../public/mark-price?instType=SWAP`, `.../market/tickers?instType=SWAP` | mark, last, 24h vol |
| `GET .../public/liquidation-orders?instType=SWAP&state=filled&uly=BTC-USDT&limit=100` | `data[0].details[] = {bkPx, sz, posSide, side, ts}`; **`uly` or `instFamily` is required** (error 50015 otherwise); paging with `after=<ts>` / `before=<ts>` verified; 100 orders spanned 4.6 h for BTC-USDT at test time |
| rate limit | no headers; 40 requests in 1.3 s all 200. OKX documents 40 req / 2 s for `books`, 20 req / 2 s for most public endpoints (documented, not reached). |

### Deribit — verified 2026-09-06
| endpoint | fields used |
|---|---|
| `GET https://www.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=option&expired=false` | 978 instruments: `instrument_name, strike, option_type, expiration_timestamp, settlement_period` |
| `GET .../public/get_book_summary_by_currency?currency=BTC&kind=option` | 978 rows: `mark_iv, open_interest, underlying_price, underlying_index, mark_price, bid_price, ask_price, volume_usd` — **no delta**; delta is computed in `compute/positioning.py` from `mark_iv` with Black-76 and checked in tests against `ticker.greeks.delta` |
| `GET .../public/ticker?instrument_name=BTC-30OCT26-54000-C` | `mark_iv, bid_iv, ask_iv, greeks.{delta,gamma,vega,theta}, open_interest` (one call per instrument; used only for spot checks) |
| `GET .../public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp&end_timestamp` | DVOL candles `[ts, o,h,l,c]` |
| `GET .../public/get_index_price?index_name=btc_usd` | `index_price` |
| `GET .../public/get_instruments?currency=BTC&kind=future&expired=false` | perpetual + day/week/month futures (`BTC-25SEP26, BTC-25DEC26, BTC-26MAR27, BTC-25JUN27`) → basis cross-check |
| rate limit | no headers; 40 requests in 0.9 s all 200. Deribit documents 20 req/s for public non-matching-engine methods without credentials. |

### Aggregated liquidations — optional
Coinglass `GET https://open-api-v4.coinglass.com/api/futures/liquidation/coin-list` → `{"code":"401"}` without key. Adapter activates on `COINGLASS_API_KEY`; not required. Free mode (a) = OKX `liquidation-orders` polling with `after`-paging back to the previous fetch, labelled **OKX-only sample** on the dashboard. Mode (b) = long-running websocket collector for Binance `forceOrder` and Bybit `allLiquidation` (documented in the runbook; not part of the Actions jobs).

## 3. Order books and trades (depth to 2 %) — verified 2026-09-06
| venue | endpoint | levels | 2 % coverage on BTC at test time |
|---|---|---|---|
| Binance | `GET https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5000` (weight 250) | 5000/side, `[price, qty]` | ±1.1 % → **truncated** for BTC; stored with `coverage_pct` |
| Bybit | `GET https://api.bybit.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=1000` (limit 1000 verified for spot and linear) | `result.b / result.a` | 200 levels gave ±0.2 %; 1000 levels stored, coverage recorded |
| OKX | `GET https://www.okx.com/api/v5/market/books-full?instId=BTC-USDT&sz=5000` | 5000/side `[price, qty, orders]` | ±1.25 % |
| Coinbase | `GET https://api.exchange.coinbase.com/products/BTC-USD/book?level=2` | full book (23 075 bids) `[price, size, num_orders]` | full |
| Kraken | `GET https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=500` (count > 500 returns 100) | 500/side `[price, vol, ts]` | ±1.7 % |

Consequence: $D(0.02)$ on Binance/OKX/Bybit is a lower bound for BTC and ETH; the
liquidity table stores the achieved coverage per venue and the dashboard marks
`D(0.02)` as "≥" when any venue was truncated. For smaller assets 5000 levels cover 2 %.

Recent trades (Benford / size-distribution filters): Binance `api/v3/trades?limit=1000` (weight 25) `{price, qty, quoteQty, time, isBuyerMaker}`; Bybit `recent-trade` (**spot capped at 60 rows**, linear 1000); OKX `market/trades?limit=500` `{px, sz, side, ts}`; Coinbase `products/{id}/trades` (1000, `{size, price, side, time}`); Kraken `Trades` (1000, `[price, vol, time, side, type, misc, id]`).

## 4. Stablecoins and macro

### DefiLlama stablecoins — verified 2026-09-06
| endpoint | fields used |
|---|---|
| `GET https://stablecoins.llama.fi/stablecoins?includePrices=true` | `peggedAssets[]` (423): `id, name, symbol, gecko_id, pegType, pegMechanism, circulating.peggedUSD, circulatingPrevDay/Week/Month, price` |
| `GET https://stablecoins.llama.fi/stablecoincharts/all` | 3204 daily rows: `date (s), totalCirculatingUSD.peggedUSD` |
| `GET https://stablecoins.llama.fi/stablecoin/{id}` | per-coin daily `tokens[] {date, circulating.peggedUSD}` (21 MB for USDT; fetched weekly only) |
| rate limit | no headers; 10-request burst fine |

### FRED — verified 2026-09-06
| item | value |
|---|---|
| keyless CSV (primary) | `GET https://fred.stlouisfed.org/graph/fredgraph.csv?id={SERIES}` → `observation_date,{SERIES}` daily/weekly rows back to 1990–2006. Verified for `WALCL, WTREGEN, RRPONTSYD, DFII10, DTWEXBGS, VIXCLS`. |
| JSON API (optional) | `GET https://api.stlouisfed.org/fred/series/observations?series_id=X&file_type=json&api_key=…` → **400 without a valid 32-char key**. Activates on `FRED_API_KEY`; adds `series` metadata (units). |
| units (reconciled from magnitudes on 2026-09-02/04) | WALCL = 6 737 204 → **millions USD**; WTREGEN = 967 935 → **millions USD**; RRPONTSYD = 0.675 → **billions USD**. Net liquidity in USD bn = `WALCL/1e3 − WTREGEN/1e3 − RRPONTSYD`. |
| rate limit (observed) | **3 concurrent CSV requests all timed out at 60 s**; serial requests take 0.1–2 s. Adapter fetches the six series serially with a 1 s gap. |
| samples | `fred.csv.*.csv.gz` |

### Spot ETF flows — unavailable (free)
No official free API was found. `farside.co.uk/btc/` is an HTML table (robots.txt only addresses Twitterbot; terms of use not verified → not scraped per ground rule 10). The iShares "download holdings" URL returns an HTML page, not CSV. The panel shows **unavailable** with this reason; an optional keyed adapter slot exists (`ETF_FLOWS_API_KEY`, provider to be verified before use).

## 5. Supply schedule and on-chain

### DefiLlama unlocks — verified 2026-09-06 (datasets host; API host is paid)
`https://api.llama.fi/emissions`, `/emission/{p}`, `/emissionsBreakdown` all return **402 "Upgrade to the paid API plan"**. The same data is served free from the datasets CDN:
| endpoint | fields used |
|---|---|
| `GET https://defillama-datasets.llama.fi/emissionsIndex` | `data[]` (370 tokens): `token (coingecko:{id}), gecko_id, protocolSlug, circSupply, circSupply30d, totalLocked, maxSupply, unlockEvents[] {timestamp, cliffAllocations[] {recipient, category, unlockType, amount}, linearAllocations[], summary}, events[] {category, unlockType, noOfTokens, timestamp}, next24h, tokenPrice` |
| `GET https://defillama-datasets.llama.fi/emissions/{slug}` | `documentedData.data[] {label, data[] {timestamp, unlocked}}`, `categories {noncirculating, airdrop, privateSale, insiders, publicSale, …: [labels]}`, `metadata.events`, `supplyMetrics` |
| `GET https://defillama-datasets.llama.fi/emissionsProtocolsList` | slugs (372) |
| `GET https://defillama-datasets.llama.fi/emissionsBreakdown` | `emission24h/7d/30d, emissions1y` per slug |
Recipient classes map from `category`: `insiders → team`, `privateSale → investors`, `publicSale → public`, `airdrop → community`, `noncirculating / farming / liquidity → ecosystem`. Missing class → `pi_c` default in `config/thresholds.yaml`, flagged.

### DefiLlama fees, revenue, TVL — verified 2026-09-06
`GET https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true` → `protocols[]` (2658): `slug, name, category, total24h, total7d, total30d, total1y, totalAllTime`. `GET https://api.llama.fi/summary/fees/{slug}?dataType=dailyRevenue` → `totalDataChart [[ts, usd]]`, `gecko_id`. `GET https://api.llama.fi/protocols` → 8190 rows `slug, symbol, gecko_id, category, tvl`. Token mapping via `gecko_id` where present, else `config/protocol_map.yaml`.

### CoinMetrics community API — verified 2026-09-06
| item | value |
|---|---|
| catalog | `GET https://community-api.coinmetrics.io/v4/catalog-v2/asset-metrics?assets=btc,eth` → 31 metrics per asset incl. **`CapMVRVCur`, `CapMrktCurUSD`, `SplyCur`, `SplyExNtv`, `SplyExUSD`, `FlowInExUSD`, `FlowOutExUSD`, `HashRate`, `AdrActCnt`, `FeeTotNtv`**. `CapRealUSD` → **403 not available with community credentials** (MVRV is available directly instead). No SOPR in the community tier → LTH-SOPR **unavailable** free; optional Glassnode adapter (`GLASSNODE_API_KEY`, 401 verified without key). |
| timeseries | `GET .../v4/timeseries/asset-metrics?assets=btc&metrics=CapMVRVCur,SplyExNtv&frequency=1d&page_size=10000&start_time=2010-01-01` → 5894 rows from 2010-07-18; values are strings; `*-status: flash` marks provisional last days; `next_page_token` paging. |
| rate limit | header `x-ratelimit-limit: 6000;w=20` (sliding window), `x-ratelimit-remaining` |

### Other BTC/ETH chain data — verified 2026-09-06
* blockchain.info charts: `GET https://api.blockchain.info/charts/{hash-rate,n-unique-addresses,market-price,transaction-fees-usd}?timespan=30days&format=json` → `values[] {x, y}`. `mvrv`, `nvt` → 404.
* mempool.space `GET https://mempool.space/api/v1/mining/hashrate/3d` → `currentHashrate, currentDifficulty`.
* ETH staking: beaconcha.in `GET https://beaconcha.in/api/v1/epoch/latest` → **401 key required** (free registration; optional `BEACONCHAIN_API_KEY`). Free fallback: ultrasound.money `GET https://ultrasound.money/api/v2/fees/supply-parts` → `beaconBalancesSum` (gwei), `beaconDepositsSum`, `slot` — an unofficial endpoint of an open-source project, labelled **unofficial** on the dashboard. Staking ratio = `beaconBalancesSum/1e9 / SplyCur(eth)`. The public beacon node `https://ethereum-beacon-api.publicnode.com` responds (`finality_checkpoints`, `validator_balances?id=…`) but an aggregate needs the full validator set and is not used.

## 6. Events — verified 2026-09-06
* Snapshot: `POST https://hub.snapshot.org/graphql` with `proposals(first, where:{state:"active", space_in:[…]}) {id title choices start end state scores scores_total space{id name} link}` and `spaces(where:{id_in})`. Headers `ratelimit-limit: 100`, `ratelimit-reset: 60` → 100 req/min. Tracked spaces live in `config/events.yaml`.
* Tally: `POST https://api.tally.xyz/query` → **401 without key**; optional `TALLY_API_KEY`.
* Upgrades, listings, regulatory dates, options expiries: `config/events.yaml` (hand-maintained, weekly review issue) and Deribit `expiration_timestamp`.

## 7. Venues
`config/venues.yaml` only (qualitative inputs with `reviewed_on`). No live endpoint.

## Summary of gaps (dashboard shows these as unavailable / optional)
| indicator | free status | optional keyed source |
|---|---|---|
| Binance / Bybit liquidations | OKX sample only (mode a); collector script (mode b) | Coinglass |
| Spot ETF flows | unavailable | to be verified |
| LTH-SOPR | unavailable | Glassnode |
| ETH staking ratio | ultrasound.money (unofficial) | beaconcha.in |
| Price history > 365 d | CoinPaprika + exchange klines + CoinMetrics | CoinGecko paid |

## Appendix: verification log (auto-generated from `summary.json`)
| id | status | latency | bytes | checked_at |
|---|---|---|---|---|
| coingecko.markets | 200 | 0.25 s | 197418 | 2026-09-06T06:10:42+00:00 |
| coingecko.coin | 200 | 0.17 s | 3874 | 2026-09-06T06:10:43+00:00 |
| coingecko.categories | 200 | 0.3 s | 54777 | 2026-09-06T06:10:43+00:00 |
| coingecko.market_chart | 200 | 0.12 s | 37768 | 2026-09-06T06:10:44+00:00 |
| coingecko.market_chart_max | 401 | 0.18 s | 335 | 2026-09-06T06:10:44+00:00 |
| coinpaprika.tickers | 200 | 0.36 s | 203931 | 2026-09-06T06:10:45+00:00 |
| coinpaprika.coin | 200 | 0.27 s | 3382 | 2026-09-06T06:10:46+00:00 |
| binance.spot.klines | 200 | 0.36 s | 178314 | 2026-09-06T06:10:46+00:00 |
| bybit.spot.kline | 200 | 0.39 s | 88992 | 2026-09-06T06:10:47+00:00 |
| okx.spot.candles | 200 | 0.28 s | 34971 | 2026-09-06T06:10:48+00:00 |
| okx.spot.history_candles | 200 | 0.26 s | 11651 | 2026-09-06T06:10:49+00:00 |
| coinbase.candles | 200 | 0.06 s | 21165 | 2026-09-06T06:10:49+00:00 |
| kraken.ohlc | 200 | 0.22 s | 59244 | 2026-09-06T06:10:50+00:00 |
| pypi.ccxt | 200 | 0.3 s | 2636312 | 2026-09-06T06:10:50+00:00 |
| binance.fut.premiumIndex | 200 | 0.57 s | 199243 | 2026-09-06T06:10:51+00:00 |
| binance.fut.fundingRate | 200 | 0.22 s | 63119 | 2026-09-06T06:10:52+00:00 |
| binance.fut.fundingInfo | 200 | 0.23 s | 132997 | 2026-09-06T06:10:53+00:00 |
| binance.fut.openInterest | 200 | 0.21 s | 69 | 2026-09-06T06:10:54+00:00 |
| binance.fut.openInterestHist | 200 | 0.24 s | 85501 | 2026-09-06T06:10:54+00:00 |
| binance.fut.exchangeInfo | 200 | 0.7 s | 1106082 | 2026-09-06T06:10:55+00:00 |
| binance.fut.ticker24h | 200 | 0.23 s | 282253 | 2026-09-06T06:10:56+00:00 |
| binance.fut.allForceOrders | 404 | 0.21 s | 1434 | 2026-09-06T06:10:57+00:00 |
| binance.fut.klines | 200 | 0.21 s | 699 | 2026-09-06T06:10:57+00:00 |
| binance.dapi.premiumIndex | 200 | 0.54 s | 7231 | 2026-09-06T06:10:58+00:00 |
| binance.dapi.exchangeInfo | 200 | 0.69 s | 35676 | 2026-09-06T06:10:59+00:00 |
| bybit.linear.tickers | 200 | 0.57 s | 639288 | 2026-09-06T06:11:00+00:00 |
| bybit.funding.history | 200 | 0.41 s | 17401 | 2026-09-06T06:11:01+00:00 |
| bybit.oi | 200 | 1.33 s | 19166 | 2026-09-06T06:11:02+00:00 |
| bybit.instruments | 200 | 0.51 s | 792537 | 2026-09-06T06:11:03+00:00 |
| okx.funding | 200 | 0.27 s | 524 | 2026-09-06T06:11:04+00:00 |
| okx.funding.history | 200 | 0.26 s | 19196 | 2026-09-06T06:11:05+00:00 |
| okx.oi | 200 | 0.23 s | 64442 | 2026-09-06T06:11:06+00:00 |
| okx.oi.hist | 200 | 0.26 s | 37712 | 2026-09-06T06:11:06+00:00 |
| okx.instruments.futures | 200 | 0.38 s | 214200 | 2026-09-06T06:11:07+00:00 |
| okx.instruments.swap | 200 | 0.26 s | 496899 | 2026-09-06T06:11:08+00:00 |
| okx.liquidations | 200 | 0.26 s | 12670 | 2026-09-06T06:11:09+00:00 |
| okx.mark | 200 | 0.21 s | 40136 | 2026-09-06T06:11:09+00:00 |
| okx.tickers.swap | 200 | 0.22 s | 139392 | 2026-09-06T06:11:10+00:00 |
| deribit.instruments | 200 | 0.25 s | 853939 | 2026-09-06T06:11:10+00:00 |
| deribit.book_summary | 200 | 0.35 s | 434325 | 2026-09-06T06:11:11+00:00 |
| deribit.ticker | 200 | 0.17 s | 700 | 2026-09-06T06:11:12+00:00 |
| deribit.dvol | 200 | 0.19 s | 1122 | 2026-09-06T06:11:13+00:00 |
| deribit.index | 200 | 0.22 s | 165 | 2026-09-06T06:11:13+00:00 |
| deribit.instruments.future | 200 | 0.17 s | 11786 | 2026-09-06T06:11:14+00:00 |
| coinglass.optional | 200 | 0.46 s | 39 | 2026-09-06T06:11:14+00:00 |
| binance.spot.depth | 200 | 0.82 s | 320048 | 2026-09-06T06:11:15+00:00 |
| binance.spot.trades | 200 | 0.37 s | 147333 | 2026-09-06T06:11:17+00:00 |
| binance.spot.exchangeInfo | 200 | 0.25 s | 5335 | 2026-09-06T06:11:17+00:00 |
| bybit.spot.orderbook | 200 | 0.32 s | 9073 | 2026-09-06T06:11:18+00:00 |
| bybit.spot.trades | 200 | 0.33 s | 11188 | 2026-09-06T06:11:19+00:00 |
| okx.books | 200 | 0.22 s | 25879 | 2026-09-06T06:11:19+00:00 |
| okx.books.full | 200 | 0.47 s | 286229 | 2026-09-06T06:11:20+00:00 |
| okx.trades | 200 | 0.35 s | 61961 | 2026-09-06T06:11:21+00:00 |
| coinbase.book | 200 | 0.1 s | 1157247 | 2026-09-06T06:11:22+00:00 |
| coinbase.trades | 200 | 0.15 s | 119261 | 2026-09-06T06:11:23+00:00 |
| kraken.depth | 200 | 0.19 s | 35061 | 2026-09-06T06:11:23+00:00 |
| kraken.trades | 200 | 0.19 s | 68789 | 2026-09-06T06:11:24+00:00 |
| llama.stablecoins | 200 | 0.26 s | 551291 | 2026-09-06T06:11:24+00:00 |
| llama.stablecoincharts | 200 | 0.27 s | 1265906 | 2026-09-06T06:11:25+00:00 |
| llama.stablecoin.id | 200 | 0.67 s | 21070240 | 2026-09-06T06:11:26+00:00 |
| fred.observations | 400 | 0.27 s | 219 | 2026-09-06T06:11:28+00:00 |
| fred.series.meta | 400 | 0.12 s | 219 | 2026-09-06T06:11:29+00:00 |
| fred.csv.WALCL | 200 | 0.12 s | 23244 | 2026-09-06T06:11:29+00:00 |
| fred.csv.WTREGEN | 200 | 0.55 s | 21440 | 2026-09-06T06:11:30+00:00 |
| fred.csv.RRPONTSYD | 200 | 2.02 s | 95020 | 2026-09-06T06:11:31+00:00 |
| fred.csv.DFII10 | 200 | 1.27 s | 98783 | 2026-09-06T06:11:33+00:00 |
| fred.csv.DTWEXBGS | 200 | 0.09 s | 104045 | 2026-09-06T06:11:35+00:00 |
| fred.csv.VIXCLS | 200 | 0.12 s | 161102 | 2026-09-06T06:11:35+00:00 |
| llama.emissions | 402 | 0.15 s | 66 | 2026-09-06T06:11:36+00:00 |
| llama.emission.arbitrum | 402 | 0.12 s | 66 | 2026-09-06T06:11:36+00:00 |
| llama.fees.overview | 200 | 1.73 s | 4207652 | 2026-09-06T06:11:37+00:00 |
| llama.fees.summary | 200 | 0.54 s | 763185 | 2026-09-06T06:11:39+00:00 |
| llama.protocols | 200 | 0.42 s | 8747806 | 2026-09-06T06:11:40+00:00 |
| coinmetrics.catalog | 200 | 0.52 s | 14354 | 2026-09-06T06:11:42+00:00 |
| coinmetrics.metrics | 403 | 0.13 s | 152 | 2026-09-06T06:11:42+00:00 |
| blockchain.info.charts | 200 | 0.59 s | 861 | 2026-09-06T06:11:43+00:00 |
| mempool.space | 200 | 0.2 s | 389 | 2026-09-06T06:11:44+00:00 |
| beaconchain.epoch | 401 | 0.2 s | 96 | 2026-09-06T06:11:45+00:00 |
| glassnode.optional | 401 | 0.55 s | 1110 | 2026-09-06T06:11:45+00:00 |
| snapshot.graphql | 200 | 0.37 s | 25 | 2026-09-06T06:11:46+00:00 |
| tally.graphql | 401 | 0.27 s | 85 | 2026-09-06T06:11:47+00:00 |
