#!/usr/bin/env python3
"""Flatten the collected samples into panel.csv (one row per sample) and trades.csv."""
import csv
import glob
import gzip
import json
import os
import statistics

ROOT = os.path.dirname(os.path.abspath(__file__))


def ok(s, name):
    v = s["src"].get(name)
    return v["data"] if v and v["ok"] else None


def best(book):
    if not book or not book["bids"] or not book["asks"]:
        return None, None
    return book["bids"][0][0], book["asks"][0][0]


def depth(book, pct=0.005):
    b, a = best(book)
    if b is None:
        return None, None
    mid = (a + b) / 2
    return (sum(q for p, q in book["bids"] if p >= mid * (1 - pct)),
            sum(q for p, q in book["asks"] if p <= mid * (1 + pct)))


def p2p(d, ref):
    if not d:
        return None, None, None, None
    out = []
    for key, pick in (("bid", True), ("ask", False)):
        ads = [a for a in d.get(key, []) if a[0] and a[1] and a[1] >= 100
               and not (len(a) > 7 and a[7])
               and (ref is None or abs(a[0] / ref - 1) < 0.08)]
        ads.sort(key=lambda a: a[0], reverse=pick)
        out.append(statistics.median(a[0] for a in ads[:5]) if ads else None)
    return out[0], out[1], d.get("bid_n"), d.get("ask_n")


def mid(b, a):
    return (a + b) / 2 if a is not None and b is not None else None


def main():
    rows, trades = [], {}
    last_slow = {}
    for path in sorted(glob.glob(os.path.join(ROOT, "data", "*", "*", "*", "*.json.gz"))):
        for s in json.load(gzip.open(path, "rt")):
            r = {"t": s["t"], "event_mode": int(s["event_mode"])}
            for v in ("quidax", "luno"):
                bk = ok(s, v + "_book")
                b, a = best(bk)
                db, da = depth(bk) if bk else (None, None)
                r.update({v + "_bid": b, v + "_ask": a, v + "_bid_depth_50bp": db, v + "_ask_depth_50bp": da})
                for row in (ok(s, v + "_trades") or {}).get("rows", []):
                    ms = row[1] * 1000 if v == "quidax" else row[1]
                    trades[(v, row[0])] = [v, row[0], ms, row[2], row[3], row[4]]
            ref = mid(r["quidax_bid"], r["quidax_ask"]) or mid(r["luno_bid"], r["luno_ask"])
            bu = ok(s, "busha") or {}
            r["busha_sell_usdt"] = (bu.get("USDTNGN") or {}).get("sell")
            r["busha_buy_usdt"] = (bu.get("USDTNGN") or {}).get("buy")
            qt, bt = ok(s, "quidax_tickers") or {}, ok(s, "binance_btc")
            if qt.get("btcngn") and bt:
                r["btc_implied"] = mid(qt["btcngn"]["bid"], qt["btcngn"]["ask"]) / mid(bt["bid"], bt["ask"])
            for v in ("okx", "bitget", "mexc", "htx"):
                d = ok(s, "p2p_" + v)
                if d:
                    r[v + "_p2p_bid"], r[v + "_p2p_ask"], r[v + "_bid_ads"], r[v + "_ask_ads"] = p2p(d, ref)
            pm = ok(s, "polymarket_books") or {}
            for name, bk in pm.items():
                b, a = best(bk)
                r["pm_" + name + "_bid"], r["pm_" + name + "_ask"] = b, a
            for tick, m in (ok(s, "kalshi") or {}).items():
                key = "kalshi_" + (m.get("name") or tick).split()[-1].lower()
                r[key + "_bid"], r[key + "_ask"] = m.get("bid"), m.get("ask")
            for net, p in (ok(s, "cngn_pools") or {}).items():
                if p.get("base_usd"):
                    last_slow["cngn_" + net + "_implied"] = 1 / p["base_usd"]
            cb = ok(s, "cbn_official")
            if cb:
                last_slow["cbn_date"], last_slow["cbn_central"] = cb[0][0], cb[0][2]
            r.update(last_slow)
            rows.append(r)
    cols = []
    for r in rows:
        for k in r:
            if k not in cols:
                cols.append(k)
    with open(os.path.join(ROOT, "panel.csv"), "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    with open(os.path.join(ROOT, "trades.csv"), "w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["venue", "id", "ts_ms", "price", "usdt", "taker_side"])
        for k in sorted(trades, key=lambda k: (k[0], k[1])):
            w.writerow(trades[k])
    print("samples", len(rows), "trades", len(trades))


if __name__ == "__main__":
    main()
