#!/usr/bin/env python3
"""Collector of naira and election-market quotes around Nigeria's 2027 presidential vote.

Standard library only. Each run writes one gzip file under data/YYYY/MM/DD/ holding a list
of samples. Outside the event windows a run takes one sample. Inside them it samples every
event_sample_seconds for event_run_seconds. All times are UTC.
"""
import concurrent.futures as cf
import datetime as dt
import gzip
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.abspath(__file__))
CFG = json.load(open(os.path.join(ROOT, "config.json")))
STATE_PATH = os.path.join(ROOT, "state.json")
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/126.0 Safari/537.36")
LEVELS = CFG["book_levels"]
NADS = CFG["p2p_ads_per_side"]


def now():
    return dt.datetime.now(dt.timezone.utc)


def iso(t=None):
    return (t or now()).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def get(url, body=None, timeout=20, ua=UA):
    headers = {"User-Agent": ua, "Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, headers=headers, data=data)
    for attempt in (0, 1):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            if attempt or e.code not in (429, 500, 502, 503, 504):
                raise
            time.sleep(2.5)


def short_id(x):
    return hashlib.sha1(str(x).encode()).hexdigest()[:10]


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def levels(rows, reverse):
    out = []
    for r in rows:
        if isinstance(r, dict):
            p, q = num(r.get("price")), num(r.get("volume", r.get("size")))
        else:
            p, q = num(r[0]), num(r[1])
        if p is not None and q is not None:
            out.append([p, q])
    out.sort(key=lambda z: z[0], reverse=reverse)
    return out[:LEVELS]


# ---------- exchanges with an order book ----------

def quidax_book(state):
    d = get("https://app.quidax.io/api/v1/markets/usdtngn/depth?limit=50")["data"]
    return {"ts": d.get("timestamp"), "bids": levels(d["bids"], True), "asks": levels(d["asks"], False)}


def quidax_tickers(state):
    out = {}
    for m in ("usdtngn", "btcngn", "cngnngn"):
        t = get("https://app.quidax.io/api/v1/markets/tickers/" + m)["data"]
        k = t["ticker"]
        out[m] = {"at": t.get("at"), "bid": num(k["buy"]), "ask": num(k["sell"]),
                  "last": num(k["last"]), "vol24": num(k["vol"])}
    return out


def quidax_trades(state):
    rows = get("https://app.quidax.io/api/v1/trades/usdtngn?limit=100")["data"]
    last = state.get("quidax_last_trade_id", 0)
    new = [[r["trade_id"], r["timestamp"], num(r["price"]), num(r["base_volume"]), r["type"]]
           for r in rows if r["trade_id"] > last]
    new.sort()
    if new:
        state["quidax_last_trade_id"] = new[-1][0]
    return {"n_returned": len(rows), "gap": bool(last and new and len(new) == len(rows)), "rows": new}


def luno_book(state):
    d = get("https://api.luno.com/api/1/orderbook_top?pair=USDTNGN")
    return {"ts": d.get("timestamp"), "bids": levels(d["bids"], True), "asks": levels(d["asks"], False)}


def luno_tickers(state):
    d = get("https://api.luno.com/api/1/tickers?pair=USDTNGN&pair=USDCNGN&pair=XBTNGN")["tickers"]
    return {t["pair"]: {"ts": t["timestamp"], "bid": num(t["bid"]), "ask": num(t["ask"]),
                        "last": num(t["last_trade"]), "vol24": num(t["rolling_24_hour_volume"])}
            for t in d}


def luno_trades(state):
    floor = int((time.time() - 23.5 * 3600) * 1000)
    since = max(state.get("luno_last_ts", 0) + 1, floor)
    truncated = state.get("luno_last_ts", 0) + 1 < floor and state.get("luno_last_ts", 0) > 0
    new = []
    for _ in range(30):
        rows = get("https://api.luno.com/api/1/trades?pair=USDTNGN&since=%d" % since).get("trades") or []
        if not rows:
            break
        rows.sort(key=lambda r: (r["timestamp"], r["sequence"]))
        new += [[r["sequence"], r["timestamp"], num(r["price"]), num(r["volume"]),
                 "buy" if r["is_buy"] else "sell"] for r in rows]
        since = rows[-1]["timestamp"] + 1
        if len(rows) < 100:
            break
        time.sleep(0.4)
    if new:
        state["luno_last_ts"] = new[-1][1]
    return {"gap": bool(truncated), "rows": new}


def busha_quotes(state):
    d = get("https://api.busha.co/v1/pairs", timeout=30)["data"]
    want = {"USDTNGN", "USDCNGN", "BTCNGN", "CNGNNGN"}
    return {p["id"]: {"buy": num(p["buy_price"]["amount"]), "sell": num(p["sell_price"]["amount"])}
            for p in d if p["id"] in want}


def binance_btc(state):
    d = get("https://data-api.binance.vision/api/v3/ticker/bookTicker?symbol=BTCUSDT")
    return {"bid": num(d["bidPrice"]), "ask": num(d["askPrice"])}


# ---------- prediction markets ----------

def polymarket_books(state):
    out = {}
    for name, tok in CFG["polymarket"]["yes_tokens"].items():
        b = get("https://clob.polymarket.com/book?token_id=" + tok)
        out[name] = {"ts": b.get("timestamp"), "last": num(b.get("last_trade_price")),
                     "bids": levels(b["bids"], True)[:10], "asks": levels(b["asks"], False)[:10]}
    return out


def kalshi_markets(state):
    d = get("https://api.elections.kalshi.com/trade-api/v2/markets?limit=50&series_ticker="
            + CFG["kalshi_series"])
    return {m["ticker"]: {"name": m.get("yes_sub_title"), "bid": num(m.get("yes_bid_dollars")),
                          "ask": num(m.get("yes_ask_dollars")), "last": num(m.get("last_price_dollars")),
                          "volume": num(m.get("volume_fp")), "oi": num(m.get("open_interest_fp")),
                          "status": m.get("status")}
            for m in d.get("markets", [])}


# ---------- peer-to-peer advertisements ----------
# side "ask": the advertiser sells USDT for naira. side "bid": the advertiser buys USDT.
# Each ad is [price, available USDT, min naira, max naira, completed orders, completion rate,
# hashed advertiser id]. Bitget adds a flag for promotional ads aimed at new users.

def p2p_okx(state):
    out = {}
    for side, key in (("sell", "ask"), ("buy", "bid")):
        d = get("https://www.okx.com/v3/c2c/tradingOrders/books?quoteCurrency=NGN&baseCurrency=USDT"
                "&side=%s&paymentMethod=all&userType=all" % side)["data"][side]
        out[key + "_n"] = len(d)
        out[key] = [[num(a["price"]), num(a["availableAmount"]), num(a["quoteMinAmountPerOrder"]),
                     num(a["quoteMaxAmountPerOrder"]), a.get("completedOrderQuantity"),
                     num(a.get("completedRate")), short_id(a.get("publicUserId"))] for a in d[:NADS]]
    return out


def p2p_bitget(state):
    out = {}
    for side, key in ((1, "ask"), (2, "bid")):
        d = get("https://www.bitget.com/v1/p2p/pub/adv/queryAdvList",
                body={"side": side, "pageNo": 1, "pageSize": NADS + 10, "coinCode": "USDT",
                      "fiatCode": "NGN", "languageType": 0})["data"]["dataList"]
        out[key + "_n"] = len(d)
        out[key] = [[num(a["price"]), num(a.get("lastAmount")), num(a.get("minAmount")),
                     num(a.get("maxAmount")), num(a.get("turnoverNum")), num(a.get("turnoverRate")),
                     short_id(a.get("encryptUserId")),
                     int(bool(a.get("rookieFriendlyFlag") or a.get("advertiseIsEvent")))]
                    for a in d[:NADS + 10]]
    return out


def p2p_mexc(state):
    out = {}
    base = ("https://p2p.mexc.com/api/market?allowTrade=false&amount=&blockTrade=false"
            "&coinId=128f589271cb4951b03e71e6323eb7be&countryCode=&currency=NGN&follow=false"
            "&haveTrade=false&page=1&payMethod=&tradeType=")
    for side, key in (("SELL", "ask"), ("BUY", "bid")):
        d = get(base + side)["data"]
        out[key + "_n"] = len(d)
        out[key] = [[num(a["price"]), num(a.get("availableQuantity")), num(a.get("minTradeLimit")),
                     num(a.get("maxTradeLimit")), None, None,
                     short_id((a.get("merchant") or {}).get("memberId"))] for a in d[:NADS]]
    return out


def p2p_htx(state):
    out = {}
    base = ("https://www.htx.com/-/x/otc/v1/data/trade-market?coinId=2&currency=15&tradeType=%s"
            "&currPage=1&payMethod=0&acceptOrder=0&blockType=general&online=1&range=0"
            "&onlyTradable=false&isFollowed=false")
    for side, key in (("sell", "ask"), ("buy", "bid")):
        d = get(base % side)["data"]
        out[key + "_n"] = len(d)
        out[key] = [[num(a["price"]), num(a.get("tradeCount")), num(a.get("minTradeLimit")),
                     num(a.get("maxTradeLimit")), a.get("tradeMonthTimes"),
                     num(a.get("orderCompleteRate")), short_id(a.get("uid"))] for a in d[:NADS]]
    return out


# ---------- slower sources ----------

def cngn_pools(state):
    out = {}
    for net, addr in CFG["cngn_pools"]:
        try:
            a = get("https://api.geckoterminal.com/api/v2/networks/%s/pools/%s" % (net, addr))["data"]["attributes"]
            out[net] = {"src": "geckoterminal", "name": a.get("name"),
                        "base_usd": num(a.get("base_token_price_usd")),
                        "quote_usd": num(a.get("quote_token_price_usd")),
                        "base_in_quote": num(a.get("base_token_price_quote_token")),
                        "reserve_usd": num(a.get("reserve_in_usd")),
                        "vol24_usd": num((a.get("volume_usd") or {}).get("h24"))}
        except Exception as e:
            try:
                p = get("https://api.dexscreener.com/latest/dex/pairs/%s/%s" % (net, addr))
                p = (p.get("pairs") or [p.get("pair")])[0]
                out[net] = {"src": "dexscreener", "name": p["baseToken"]["symbol"] + "/" + p["quoteToken"]["symbol"],
                            "base_usd": num(p.get("priceUsd")), "base_in_quote": num(p.get("priceNative")),
                            "reserve_usd": num((p.get("liquidity") or {}).get("usd")),
                            "vol24_usd": num((p.get("volume") or {}).get("h24"))}
            except Exception as e2:
                out[net] = {"error": (repr(e) + " / " + repr(e2))[:200]}
        time.sleep(1.0)
    return out


def polymarket_event(state):
    ev = get("https://gamma-api.polymarket.com/events?slug=" + CFG["polymarket"]["event_slug"], timeout=30)[0]
    rows = []
    for m in ev.get("markets", []):
        v = num(m.get("volume")) or 0
        if v > 0:
            rows.append([m.get("question"), v, num(m.get("bestBid")), num(m.get("bestAsk")),
                         num(m.get("lastTradePrice"))])
    return {"volume": num(ev.get("volume")), "liquidity": num(ev.get("liquidity")), "markets": rows}


# ---------- daily sources ----------

def cbn_official(state):
    d = get("https://www.cbn.gov.ng/api/GetAllExchangeRates", timeout=90)
    us = [r for r in d if r.get("currency", "").strip().upper() == "US DOLLAR"]
    us.sort(key=lambda r: r["ratedate"], reverse=True)
    return [[r["ratedate"], num(r["buyingrate"]), num(r["centralrate"]), num(r["sellingrate"])] for r in us[:15]]


def yahoo_bars(symbol, interval, rng):
    y = get("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=%s&range=%s"
            % (symbol, interval, rng), timeout=30, ua="Mozilla/5.0")["chart"]["result"][0]
    q = y["indicators"]["quote"][0]
    return [[t, c] for t, c in zip(y.get("timestamp") or [], q.get("close") or []) if c is not None]


def yahoo_daily(state):
    return {"NGN=X_1m": yahoo_bars("NGN%3DX", "1m", "2d"), "BZ=F_5m": yahoo_bars("BZ%3DF", "5m", "2d")}


FAST = {"quidax_book": quidax_book, "quidax_tickers": quidax_tickers, "quidax_trades": quidax_trades,
        "luno_book": luno_book, "luno_tickers": luno_tickers, "luno_trades": luno_trades,
        "busha": busha_quotes, "binance_btc": binance_btc,
        "polymarket_books": polymarket_books, "kalshi": kalshi_markets}
P2P = {"p2p_okx": p2p_okx, "p2p_bitget": p2p_bitget, "p2p_mexc": p2p_mexc, "p2p_htx": p2p_htx}
SLOW = {"cngn_pools": cngn_pools, "polymarket_event": polymarket_event}
DAILY = {"cbn_official": cbn_official, "yahoo": yahoo_daily}


def run_source(name, fn, state):
    t0 = time.time()
    try:
        data = fn(state)
        return name, {"t": iso(), "ms": int((time.time() - t0) * 1000), "ok": True, "data": data}
    except Exception as e:
        return name, {"t": iso(), "ms": int((time.time() - t0) * 1000), "ok": False, "error": repr(e)[:200]}


def in_event(t):
    for a, b in CFG["event_windows_utc"]:
        if dt.datetime.fromisoformat(a.replace("Z", "+00:00")) <= t < dt.datetime.fromisoformat(b.replace("Z", "+00:00")):
            return True
    return False


def sample(state, event):
    t = now()
    ts = time.time()
    todo = dict(FAST)
    p2p_every = CFG["p2p_seconds_event"] if event else CFG["p2p_seconds_baseline"]
    if ts - state.get("last_p2p", 0) >= p2p_every - 30:
        todo.update(P2P)
        state["last_p2p"] = ts
    if ts - state.get("last_slow", 0) >= CFG["slow_seconds"] - 30:
        todo.update(SLOW)
        state["last_slow"] = ts
    day = t.strftime("%Y-%m-%d")
    done = state.setdefault("daily_done", {})
    tries = state.setdefault("daily_tries", {})
    for name, fn in DAILY.items():
        if done.get(name) != day and tries.get(name, "").count(day) < 6:
            todo[name] = fn
            tries[name] = (tries.get(name, "") if day in tries.get(name, "") else "") + day + ";"
    out = {"t": iso(t), "event_mode": event, "src": {}}
    with cf.ThreadPoolExecutor(max_workers=12) as ex:
        for name, res in ex.map(lambda kv: run_source(kv[0], kv[1], state), todo.items()):
            out["src"][name] = res
            if name in DAILY and res["ok"]:
                done[name] = day
    return out


def write(samples):
    t = dt.datetime.fromisoformat(samples[0]["t"].replace("Z", "+00:00"))
    d = os.path.join(ROOT, "data", t.strftime("%Y"), t.strftime("%m"), t.strftime("%d"))
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, t.strftime("%H%M%S") + ".json.gz")
    with gzip.open(path, "wt", compresslevel=9) as f:
        json.dump(samples, f, separators=(",", ":"))
    return path


def main():
    state = json.load(open(STATE_PATH)) if os.path.exists(STATE_PATH) else {}
    event = in_event(now()) or "--event" in sys.argv
    budget = CFG["event_run_seconds"] if event else 0
    for a in sys.argv[1:]:
        if a.startswith("--seconds="):
            budget = int(a.split("=")[1])
    step = CFG["event_sample_seconds"]
    start = time.time()
    samples, paths = [], []
    while True:
        t0 = time.time()
        samples.append(sample(state, event))
        if len(samples) >= 10 or time.time() - start + step > budget:
            paths.append(write(samples))
            samples = []
            json.dump(state, open(STATE_PATH, "w"))
        if time.time() - start + step > budget:
            break
        time.sleep(max(0.0, step - (time.time() - t0)))
    last = json.load(gzip.open(paths[-1], "rt"))[-1]
    status = {k: v["ok"] for k, v in last["src"].items()}
    json.dump({"t": last["t"], "event_mode": event, "files": [os.path.relpath(p, ROOT) for p in paths],
               "ok": status}, open(os.path.join(ROOT, "latest.json"), "w"), indent=1)
    print(json.dumps({"t": last["t"], "event": event, "files": len(paths),
                      "failed": [k for k, v in status.items() if not v]}))
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a") as f:
            f.write("event=%s\n" % ("true" if in_event(now()) else "false"))


if __name__ == "__main__":
    main()
