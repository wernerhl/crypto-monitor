# election-venues

Quotes for the naira and for election markets around Nigeria's presidential vote of
16 January 2027, collected for the paper on where an emerging market is priced when its
exchanges are closed. This branch holds the collector and its data. It shares no files with
`main`.

## What runs

The workflow `.github/workflows/election-venues.yml` on `main` runs `collect.py` every ten
minutes on a GitHub-hosted runner and commits the new files here.

| Period (UTC) | Sampling |
|---|---|
| Until 14 January 2027 | One sample per run. Peer-to-peer ads and slow sources every 30 minutes |
| 14 to 21 January 2027 | One sample per minute in runs of 9 minutes that start one after another. Peer-to-peer ads every 5 minutes |

The dates and intervals are set in `config.json`. A change to `config.json` on this branch
starts a run at once.

## Sources

| Key | Source | Content |
|---|---|---|
| `quidax_book`, `quidax_tickers`, `quidax_trades` | Quidax | USDT/NGN order book (25 levels), tickers for USDT, BTC and cNGN against the naira, new trades |
| `luno_book`, `luno_tickers`, `luno_trades` | Luno | USDT/NGN order book (25 levels), tickers, new trades |
| `busha` | Busha | Buy and sell quotes for USDT, USDC, BTC and cNGN against the naira |
| `binance_btc` | Binance | BTC/USDT best bid and ask, used for the BTC-implied naira rate |
| `p2p_okx`, `p2p_bitget`, `p2p_mexc`, `p2p_htx` | Peer-to-peer desks | Best USDT/NGN advertisements on each side |
| `polymarket_books`, `polymarket_event` | Polymarket | Order books for Tinubu, Obi and Atiku, event volume |
| `kalshi` | Kalshi | Bid, ask, last price and volume for series KXPRESNIGERIA |
| `cngn_pools` | GeckoTerminal, DexScreener as fallback | cNGN pools on Celo, Base and BNB Chain |
| `cbn_official` | Central Bank of Nigeria | Official dollar rate, last 15 dates, once a day |
| `yahoo` | Yahoo Finance | NGN=X one-minute bars and Brent five-minute bars, once a day |

Binance and Bybit do not quote the naira on their peer-to-peer desks or are not reachable
from the runner, so they are absent.

## Files

- `data/YYYY/MM/DD/HHMMSS.json.gz` holds a list of samples. Each sample has its start time
  `t` and one entry per source with the time of the reply, the latency in milliseconds, a
  success flag and the data.
- Order book levels are `[price, USDT]`. Trades are `[id, time, price, USDT, taker side]`.
  Quidax reports seconds and Luno milliseconds.
- A peer-to-peer advertisement is `[price, available USDT, minimum naira, maximum naira,
  completed orders, completion rate, hashed advertiser id]`. Side `ask` lists advertisers who
  sell USDT. Bitget rows carry a final flag for promotional ads aimed at new users.
- `state.json` stores the last trade seen on each exchange. `latest.json` reports the last
  run and which sources answered.

## Building the panel

```
python3 build_panel.py
```

writes `panel.csv` with one row per sample (best bid and ask, depth within 0.5 percent of
the mid, peer-to-peer medians, prediction-market quotes, cNGN-implied rates, the official
rate) and `trades.csv`.

## Limits

- Order books, quotes and advertisements exist only from the first run on 24 November 2026.
  Quidax one-minute candles and on-chain cNGN swaps can be pulled for earlier dates.
- A scheduled run on GitHub can start late or be skipped, so the baseline has uneven gaps.
- Luno serves trades for the last 24 hours only. A gap longer than that loses trades, and
  the sample then carries `"gap": true`.
- The vote count of the electoral commission is not collected yet.
