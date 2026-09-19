"""Work order 9 §4.1 — widen the usable daily-close history for the resistance base-rate model.

    PYTHONPATH=src uv run --no-sync python scripts/backfill_prices_history.py [--floor 2015-01-01]

Binance daily klines only reach 2018-06 (and each alt to its Binance listing). Coinbase Exchange
(a permitted source, not runner-blocked) reaches 2015 for the majors. This pages Coinbase daily
candles back to `floor` for the Tier-1 list, VERIFIES each series against the Binance overlap
(median absolute close difference must be small) before use, and upserts the pre-Binance rows into
prices_daily under venue=coinbase. The canonical series prefers Binance where both exist, so this
only fills days Binance is missing (mostly pre-2018 for BTC/ETH/LTC). Reproducible and idempotent;
run from ~/crypto-monitor. No model or threshold changes — it only lengthens the price history."""

from __future__ import annotations

import argparse
import time
from datetime import UTC, date, datetime, timedelta

import httpx
import polars as pl

from monitor import archive
from monitor.meta import git_sha, utc_now

BASE = "https://api.exchange.coinbase.com"
# Tier-1 bases mapped to their Coinbase USD product (BNB is not listed on Coinbase — skipped)
PRODUCTS = {
    "BTC": "BTC-USD", "ETH": "ETH-USD", "SOL": "SOL-USD", "XRP": "XRP-USD", "ADA": "ADA-USD",
    "AVAX": "AVAX-USD", "LINK": "LINK-USD", "LTC": "LTC-USD", "DOT": "DOT-USD", "DOGE": "DOGE-USD",
    "APT": "APT-USD", "ARB": "ARB-USD", "SUI": "SUI-USD",
}
MAX_MEDIAN_DIFF = 0.02  # overlap sanity: median |coinbase/binance - 1| must be under 2%


def _candles(client: httpx.Client, product: str, start: date, end: date) -> list[list]:
    r = client.get(
        f"/products/{product}/candles",
        params={"granularity": 86400, "start": start.isoformat(), "end": end.isoformat()},
    )
    r.raise_for_status()
    return r.json()  # [[time, low, high, open, close, volume], ...], newest first


def fetch_series(client: httpx.Client, base: str, product: str, floor: date, today: date) -> pl.DataFrame:
    rows = []
    win_start = floor
    while win_start < today:
        win_end = min(win_start + timedelta(days=290), today)
        try:
            data = _candles(client, product, win_start, win_end)
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 404:
                return pl.DataFrame()  # product not listed
            raise
        for k in data:
            d = datetime.fromtimestamp(k[0], tz=UTC).date()
            rows.append({"date": d, "open": float(k[3]), "high": float(k[2]), "low": float(k[1]),
                         "close": float(k[4]), "volume_base": float(k[5])})
        win_start = win_end
        time.sleep(0.35)
    if not rows:
        return pl.DataFrame()
    return pl.DataFrame(rows).unique(subset=["date"], keep="last").sort("date")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--floor", default="2015-01-01")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    floor = date.fromisoformat(args.floor)
    today = utc_now().date()
    sha, now = git_sha(), utc_now()

    existing = archive.read("prices_daily")
    bstart = {}  # earliest binance date per base (the overlap anchor)
    if existing is not None:
        b = existing.filter(pl.col("venue") == "binance").group_by("base").agg(pl.col("date").min().alias("m"))
        bstart = {r["base"]: r["m"] for r in b.to_dicts()}

    added = []
    with httpx.Client(base_url=BASE, timeout=30, headers={"User-Agent": "crypto-monitor/backfill"}) as client:
        for base, product in PRODUCTS.items():
            cb = fetch_series(client, base, product, floor, today)
            if not cb.height:
                print(f"{base}: no coinbase history ({product}); skipped")
                continue
            # verify against the binance overlap before use
            anchor = bstart.get(base)
            verdict = "no binance overlap"
            if anchor is not None and existing is not None:
                bo = existing.filter((pl.col("base") == base) & (pl.col("venue") == "binance")).select("date", "close")
                j = cb.select("date", pl.col("close").alias("cb")).join(bo, on="date", how="inner")
                if j.height >= 30:
                    md = float((j["cb"] / j["close"] - 1.0).abs().median())
                    verdict = f"median |Δ| {md:.4f}"
                    if md > MAX_MEDIAN_DIFF:
                        print(f"{base}: FAILED overlap check ({verdict}); not used")
                        continue
            # keep only pre-Binance rows (canonical series prefers Binance where both exist)
            pre = cb.filter(pl.col("date") < anchor) if anchor is not None else cb
            if not pre.height:
                print(f"{base}: coinbase {cb['date'].min()}..{cb['date'].max()}, {verdict}; no pre-binance rows to add")
                continue
            df = pre.with_columns(
                pl.lit("coinbase").alias("venue"), pl.lit(product).alias("symbol"), pl.lit(base).alias("base"),
                pl.lit(None, dtype=pl.Float64).alias("volume_quote"),
                pl.lit("coinbase.backfill").alias("source"), pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
            )
            added.append(df)
            print(f"{base}: +{pre.height} pre-binance days ({pre['date'].min()}..{pre['date'].max()}), {verdict}")

    if not added:
        print("nothing to add")
        return
    allnew = pl.concat(added, how="diagonal_relaxed")
    print(f"\ntotal rows to add: {allnew.height}")
    if args.dry_run:
        print("dry run — not written")
        return
    archive.upsert("prices_daily", allnew)
    print("upserted into prices_daily")


if __name__ == "__main__":
    main()
