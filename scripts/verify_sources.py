"""Live verification of every endpoint listed in docs/data_sources.md.

Usage: python scripts/verify_sources.py [--out DIR] [--only id1,id2]

Writes one gzipped raw body per endpoint to DIR/<id>.json.gz, a summary.json and a
markdown table on stdout. No third-party dependencies (stdlib only) so it can be run
before the project environment exists.
"""

from __future__ import annotations

import argparse
import gzip
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path

UA = "crypto-monitor-verify/0.1 (+https://github.com/wernerhl/crypto-monitor)"
NOW_MS = int(time.time() * 1000)
FRED_KEY = os.environ.get("FRED_API_KEY", "")

SNAPSHOT_Q = {
    "query": """{ proposals(first: 5, where: {state: "active", space_in: ["aave.eth","uniswapgovernance.eth","arbitrumfoundation.eth"]}, orderBy: "created", orderDirection: desc) { id title space { id } start end state scores_total } }"""
}

ENDPOINTS: list[dict] = [
    # 3.1 prices
    dict(
        id="coingecko.markets",
        url="https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false",
    ),
    dict(
        id="coingecko.coin",
        url="https://api.coingecko.com/api/v3/coins/bitcoin?localization=false&tickers=false&market_data=false&community_data=false&developer_data=false&sparkline=false",
    ),
    dict(id="coingecko.categories", url="https://api.coingecko.com/api/v3/coins/categories/list"),
    dict(
        id="coingecko.market_chart",
        url="https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=365&interval=daily",
    ),
    dict(
        id="coingecko.market_chart_max",
        url="https://api.coingecko.com/api/v3/coins/bitcoin/market_chart?vs_currency=usd&days=max&interval=daily",
    ),
    dict(id="coinpaprika.tickers", url="https://api.coinpaprika.com/v1/tickers?limit=250"),
    dict(id="coinpaprika.coin", url="https://api.coinpaprika.com/v1/coins/btc-bitcoin"),
    dict(
        id="binance.spot.klines",
        url="https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1h&limit=1000",
    ),
    dict(
        id="bybit.spot.kline",
        url="https://api.bybit.com/v5/market/kline?category=spot&symbol=BTCUSDT&interval=60&limit=1000",
    ),
    dict(
        id="okx.spot.candles",
        url="https://www.okx.com/api/v5/market/candles?instId=BTC-USDT&bar=1H&limit=300",
    ),
    dict(
        id="okx.spot.history_candles",
        url="https://www.okx.com/api/v5/market/history-candles?instId=BTC-USDT&bar=1H&limit=100",
    ),
    dict(
        id="coinbase.candles",
        url="https://api.exchange.coinbase.com/products/BTC-USD/candles?granularity=3600",
    ),
    dict(id="kraken.ohlc", url="https://api.kraken.com/0/public/OHLC?pair=XBTUSD&interval=60"),
    dict(id="pypi.ccxt", url="https://pypi.org/pypi/ccxt/json"),
    # 3.2 derivatives
    dict(id="binance.fut.premiumIndex", url="https://fapi.binance.com/fapi/v1/premiumIndex"),
    dict(
        id="binance.fut.fundingRate",
        url="https://fapi.binance.com/fapi/v1/fundingRate?symbol=BTCUSDT&limit=1000",
    ),
    dict(id="binance.fut.fundingInfo", url="https://fapi.binance.com/fapi/v1/fundingInfo"),
    dict(
        id="binance.fut.openInterest",
        url="https://fapi.binance.com/fapi/v1/openInterest?symbol=BTCUSDT",
    ),
    dict(
        id="binance.fut.openInterestHist",
        url="https://fapi.binance.com/futures/data/openInterestHist?symbol=BTCUSDT&period=1h&limit=500",
    ),
    dict(id="binance.fut.exchangeInfo", url="https://fapi.binance.com/fapi/v1/exchangeInfo"),
    dict(id="binance.fut.ticker24h", url="https://fapi.binance.com/fapi/v1/ticker/24hr"),
    dict(
        id="binance.fut.allForceOrders",
        url="https://fapi.binance.com/fapi/v1/allForceOrders?symbol=BTCUSDT&limit=10",
    ),
    dict(
        id="binance.fut.klines",
        url="https://fapi.binance.com/fapi/v1/klines?symbol=BTCUSDT&interval=1h&limit=5",
    ),
    dict(id="binance.dapi.premiumIndex", url="https://dapi.binance.com/dapi/v1/premiumIndex"),
    dict(id="binance.dapi.exchangeInfo", url="https://dapi.binance.com/dapi/v1/exchangeInfo"),
    dict(id="bybit.linear.tickers", url="https://api.bybit.com/v5/market/tickers?category=linear"),
    dict(
        id="bybit.funding.history",
        url="https://api.bybit.com/v5/market/funding/history?category=linear&symbol=BTCUSDT&limit=200",
    ),
    dict(
        id="bybit.oi",
        url="https://api.bybit.com/v5/market/open-interest?category=linear&symbol=BTCUSDT&intervalTime=1h&limit=200",
    ),
    dict(
        id="bybit.instruments",
        url="https://api.bybit.com/v5/market/instruments-info?category=linear&limit=1000",
    ),
    dict(
        id="okx.funding", url="https://www.okx.com/api/v5/public/funding-rate?instId=BTC-USDT-SWAP"
    ),
    dict(
        id="okx.funding.history",
        url="https://www.okx.com/api/v5/public/funding-rate-history?instId=BTC-USDT-SWAP&limit=100",
    ),
    dict(id="okx.oi", url="https://www.okx.com/api/v5/public/open-interest?instType=SWAP"),
    dict(
        id="okx.oi.hist",
        url="https://www.okx.com/api/v5/rubik/stat/contracts/open-interest-volume?ccy=BTC&period=1H",
    ),
    dict(
        id="okx.instruments.futures",
        url="https://www.okx.com/api/v5/public/instruments?instType=FUTURES",
    ),
    dict(
        id="okx.instruments.swap", url="https://www.okx.com/api/v5/public/instruments?instType=SWAP"
    ),
    dict(
        id="okx.liquidations",
        url="https://www.okx.com/api/v5/public/liquidation-orders?instType=SWAP&state=filled&uly=BTC-USDT",
    ),
    dict(id="okx.mark", url="https://www.okx.com/api/v5/public/mark-price?instType=SWAP"),
    dict(id="okx.tickers.swap", url="https://www.okx.com/api/v5/market/tickers?instType=SWAP"),
    dict(
        id="deribit.instruments",
        url="https://www.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=option&expired=false",
    ),
    dict(
        id="deribit.book_summary",
        url="https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option",
    ),
    dict(
        id="deribit.ticker",
        url="https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL",
    ),
    dict(
        id="deribit.dvol",
        url=f"https://www.deribit.com/api/v2/public/get_volatility_index_data?currency=BTC&resolution=3600&start_timestamp={NOW_MS - 86400_000}&end_timestamp={NOW_MS}",
    ),
    dict(
        id="deribit.index",
        url="https://www.deribit.com/api/v2/public/get_index_price?index_name=btc_usd",
    ),
    dict(
        id="deribit.instruments.future",
        url="https://www.deribit.com/api/v2/public/get_instruments?currency=BTC&kind=future&expired=false",
    ),
    dict(
        id="coinglass.optional",
        url="https://open-api-v4.coinglass.com/api/futures/liquidation/coin-list",
    ),
    # 3.3 books
    dict(
        id="binance.spot.depth",
        url="https://api.binance.com/api/v3/depth?symbol=BTCUSDT&limit=5000",
    ),
    dict(
        id="binance.spot.trades",
        url="https://api.binance.com/api/v3/trades?symbol=BTCUSDT&limit=1000",
    ),
    dict(
        id="binance.spot.exchangeInfo",
        url="https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT",
    ),
    dict(
        id="bybit.spot.orderbook",
        url="https://api.bybit.com/v5/market/orderbook?category=spot&symbol=BTCUSDT&limit=200",
    ),
    dict(
        id="bybit.spot.trades",
        url="https://api.bybit.com/v5/market/recent-trade?category=spot&symbol=BTCUSDT&limit=60",
    ),
    dict(id="okx.books", url="https://www.okx.com/api/v5/market/books?instId=BTC-USDT&sz=400"),
    dict(
        id="okx.books.full",
        url="https://www.okx.com/api/v5/market/books-full?instId=BTC-USDT&sz=5000",
    ),
    dict(id="okx.trades", url="https://www.okx.com/api/v5/market/trades?instId=BTC-USDT&limit=500"),
    dict(id="coinbase.book", url="https://api.exchange.coinbase.com/products/BTC-USD/book?level=2"),
    dict(id="coinbase.trades", url="https://api.exchange.coinbase.com/products/BTC-USD/trades"),
    dict(id="kraken.depth", url="https://api.kraken.com/0/public/Depth?pair=XBTUSD&count=500"),
    dict(id="kraken.trades", url="https://api.kraken.com/0/public/Trades?pair=XBTUSD"),
    # 3.4 stablecoins & macro
    dict(id="llama.stablecoins", url="https://stablecoins.llama.fi/stablecoins?includePrices=true"),
    dict(id="llama.stablecoincharts", url="https://stablecoins.llama.fi/stablecoincharts/all"),
    dict(id="llama.stablecoin.id", url="https://stablecoins.llama.fi/stablecoin/1"),
    dict(
        id="fred.observations",
        url=f"https://api.stlouisfed.org/fred/series/observations?series_id=WALCL&file_type=json&api_key={FRED_KEY or 'MISSING'}",
    ),
    dict(
        id="fred.series.meta",
        url=f"https://api.stlouisfed.org/fred/series?series_id=WALCL&file_type=json&api_key={FRED_KEY or 'MISSING'}",
    ),
    dict(
        id="fred.csv.WALCL",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=WALCL",
        kind="csv",
    ),
    dict(
        id="fred.csv.WTREGEN",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=WTREGEN",
        kind="csv",
    ),
    dict(
        id="fred.csv.RRPONTSYD",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=RRPONTSYD",
        kind="csv",
    ),
    dict(
        id="fred.csv.DFII10",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=DFII10",
        kind="csv",
    ),
    dict(
        id="fred.csv.DTWEXBGS",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=DTWEXBGS",
        kind="csv",
    ),
    dict(
        id="fred.csv.VIXCLS",
        url="https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS",
        kind="csv",
    ),
    # 3.5 supply & on-chain
    dict(id="llama.emissions", url="https://api.llama.fi/emissions"),
    dict(id="llama.emission.arbitrum", url="https://api.llama.fi/emission/arbitrum"),
    dict(
        id="llama.fees.overview",
        url="https://api.llama.fi/overview/fees?excludeTotalDataChart=true&excludeTotalDataChartBreakdown=true",
    ),
    dict(
        id="llama.fees.summary",
        url="https://api.llama.fi/summary/fees/uniswap?dataType=dailyRevenue",
    ),
    dict(id="llama.protocols", url="https://api.llama.fi/protocols"),
    dict(
        id="coinmetrics.catalog",
        url="https://community-api.coinmetrics.io/v4/catalog-v2/asset-metrics?assets=btc,eth",
    ),
    dict(
        id="coinmetrics.metrics",
        url="https://community-api.coinmetrics.io/v4/timeseries/asset-metrics?assets=btc,eth&metrics=CapRealUSD,CapMrktCurUSD,SplyCur,SplyAct1yr&frequency=1d&page_size=10&start_time=2026-08-20",
    ),
    dict(
        id="blockchain.info.charts",
        url="https://api.blockchain.info/charts/n-transactions?timespan=30days&format=json",
    ),
    dict(id="mempool.space", url="https://mempool.space/api/v1/mining/hashrate/3d"),
    dict(id="beaconchain.epoch", url="https://beaconcha.in/api/v1/epoch/latest"),
    dict(
        id="glassnode.optional",
        url="https://api.glassnode.com/v1/metrics/distribution/balance_exchanges?a=BTC",
    ),
    # 3.6 events
    dict(
        id="snapshot.graphql",
        url="https://hub.snapshot.org/graphql",
        method="POST",
        body=SNAPSHOT_Q,
    ),
    dict(
        id="tally.graphql",
        url="https://api.tally.xyz/query",
        method="POST",
        body={"query": "{ chains { id } }"},
    ),
]

INTERESTING_HEADERS = [
    "x-mbx-used-weight",
    "x-mbx-used-weight-1m",
    "x-ratelimit-limit",
    "x-ratelimit-remaining",
    "x-ratelimit-reset",
    "retry-after",
    "x-bapi-limit",
    "x-bapi-limit-status",
    "x-bapi-limit-reset-timestamp",
    "ratelimit-limit",
    "ratelimit-remaining",
    "ratelimit-reset",
    "cf-ray",
    "content-type",
    "content-encoding",
]


def summarize(obj, depth=0):
    if isinstance(obj, dict):
        keys = list(obj.keys())[:25]
        return {
            "type": "dict",
            "n": len(obj),
            "keys": keys,
            "first": summarize(obj[keys[0]], depth + 1) if keys and depth < 2 else None,
        }
    if isinstance(obj, list):
        return {
            "type": "list",
            "n": len(obj),
            "first": summarize(obj[0], depth + 1) if obj and depth < 2 else None,
        }
    return {"type": type(obj).__name__, "sample": str(obj)[:60]}


def fetch(ep: dict) -> dict:
    method = ep.get("method", "GET")
    data = None
    headers = {"User-Agent": UA, "Accept": "application/json, text/csv, */*"}
    if ep.get("body") is not None:
        data = json.dumps(ep["body"]).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(ep["url"], data=data, method=method, headers=headers)
    t0 = time.perf_counter()
    rec = dict(
        id=ep["id"],
        url=ep["url"].replace(FRED_KEY, "<FRED_API_KEY>") if FRED_KEY else ep["url"],
        method=method,
        checked_at=datetime.now(UTC).isoformat(timespec="seconds"),
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            raw = r.read()
            status = r.status
            hdrs = {k.lower(): v for k, v in r.headers.items()}
    except urllib.error.HTTPError as e:
        raw = e.read()
        status = e.code
        hdrs = {k.lower(): v for k, v in e.headers.items()}
    except Exception as e:
        rec.update(status=None, error=repr(e), elapsed_s=round(time.perf_counter() - t0, 2))
        return rec, None
    rec.update(
        status=status,
        elapsed_s=round(time.perf_counter() - t0, 2),
        bytes=len(raw),
        headers={k: v for k, v in hdrs.items() if k in INTERESTING_HEADERS},
    )
    if ep.get("kind") == "csv":
        text = raw.decode("utf-8", "replace")
        lines = text.splitlines()
        rec["shape"] = {"type": "csv", "n_lines": len(lines), "head": lines[:2], "tail": lines[-2:]}
    else:
        try:
            obj = json.loads(raw)
            rec["shape"] = summarize(obj)
        except Exception:
            rec["shape"] = {"type": "text", "head": raw[:300].decode("utf-8", "replace")}
    return rec, raw


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="tests/fixtures/live_verification")
    ap.add_argument("--only", default="")
    args = ap.parse_args()
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    only = set(filter(None, args.only.split(",")))
    results = []
    for ep in ENDPOINTS:
        if only and ep["id"] not in only:
            continue
        rec, raw = fetch(ep)
        results.append(rec)
        if raw is not None:
            ext = ".csv.gz" if ep.get("kind") == "csv" else ".json.gz"
            with gzip.open(out / (ep["id"] + ext), "wb") as f:
                f.write(raw)
        flag = "OK " if rec.get("status") == 200 else "!! "
        print(
            f"{flag}{rec['id']:<32} {rec.get('status')} {rec.get('elapsed_s')}s {rec.get('bytes', '')}B {json.dumps(rec.get('shape'))[:160]}",
            flush=True,
        )
        time.sleep(0.4)
    (out / "summary.json").write_text(json.dumps(results, indent=1))
    print(f"\nwrote {len(results)} records to {out}/summary.json")


if __name__ == "__main__":
    sys.exit(main())
