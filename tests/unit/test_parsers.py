"""Adapter parsers on the truncated live samples in tests/fixtures/live_verification/.
These check field mapping and unit conversion, not values."""

from __future__ import annotations

import gzip
import json
from datetime import UTC, datetime
from pathlib import Path

import polars as pl
import pytest

from monitor.fetch import binance, bybit, coinbase, coingecko, coinpaprika, kraken, okx
from monitor.fetch.base import Envelope, Record
from monitor.fetch.symbols import kraken_base, split_multiplier

FIX = Path(__file__).resolve().parents[1] / "fixtures" / "live_verification"
NOW = datetime(2026, 9, 6, 6, 30, tzinfo=UTC)


def body(name: str):
    return json.loads(gzip.decompress((FIX / f"{name}.json.gz").read_bytes()))


def env(
    source: str, dataset: str, bodies: list, meta: dict | None = None, fetched: datetime = NOW
) -> Envelope:
    recs = [
        Record(url="fixture", status=200, fetched_at=fetched.isoformat(), body=b) for b in bodies
    ]
    return Envelope(source, dataset, "0000", fetched.isoformat(), "fixture-sha", recs, meta or {})


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("BTC", ("BTC", 1.0)),
        ("1000PEPE", ("PEPE", 1000.0)),
        ("1MBABYDOGE", ("BABYDOGE", 1e6)),
        ("SHIB1000", ("SHIB", 1000.0)),
        ("1000000MOG", ("MOG", 1e6)),
    ],
)
def test_split_multiplier(raw, expected):
    assert split_multiplier(raw) == expected


def test_kraken_base():
    assert (
        kraken_base("XBT") == "BTC"
        and kraken_base("XXBT") == "BTC"
        and kraken_base("XDG") == "DOGE"
        and kraken_base("SOL") == "SOL"
    )


def test_coingecko_markets_parse():
    b = body("coingecko.markets") * 10  # fixture is truncated to 20 rows; sanity needs >= 200
    df = coingecko.parse_markets(
        env(
            "coingecko",
            "markets",
            [b],
            fetched=datetime.fromisoformat(b[0]["last_updated"].replace("Z", "+00:00")),
        )
    )
    row = df.filter(df["id"] == "bitcoin").to_dicts()[0]
    assert (
        row["symbol"] == "BTC"
        and row["circulating_supply"] > 1.9e7
        and row["supply_source"] == "reported"
    )
    assert set(df.columns) >= {"source", "fetched_at", "git_sha", "as_of"}


def test_coinpaprika_markets_derives_supply():
    b = body("coinpaprika.tickers") * 10
    df = coinpaprika.parse_markets(env("coinpaprika", "markets", [b]))
    row = df.filter(df["id"] == "btc-bitcoin").to_dicts()[0]
    assert row["supply_source"] == "derived"
    assert abs(row["circulating_supply"] - row["market_cap_usd"] / row["price_usd"]) < 1e-6


def test_coingecko_coin_meta_parse():
    df = coingecko.parse_coin_meta(
        env("coingecko", "coin_meta", [body("coingecko.coin")], {"ids": ["bitcoin"]})
    )
    assert df["id"][0] == "bitcoin" and "Proof of Work (PoW)" in df["categories"][0]


def test_binance_perps_parse_units():
    prem = body("binance.fut.premiumIndex")
    finfo = body("binance.fut.fundingInfo")
    t24 = body("binance.fut.ticker24h")
    sym = prem[0]["symbol"]
    oi = {"symbol": sym, "openInterest": "1234.5", "time": int(NOW.timestamp() * 1000)}
    df = binance.parse_perps(
        env("binance_usdm", "perps", [prem, finfo, t24, oi], {"symbols": [sym]})
    )
    r = df.to_dicts()[0]
    # oi_usd = contracts * mark (contracts are base units on USDT-M); oi_base = contracts * multiplier
    assert r["oi_usd"] == pytest.approx(1234.5 * float(prem[0]["markPrice"]))
    assert r["oi_base"] == pytest.approx(1234.5 * r["multiplier"])
    assert r["funding_interval_h"] in (4.0, 8.0, 1.0, 2.0)


def test_bybit_perps_parse_uses_oi_value():
    b = body("bybit.linear.tickers")
    b["result"]["list"] = b["result"]["list"] * 10
    df = bybit.parse_perps(env("bybit", "perps", [b]))
    t = next(x for x in b["result"]["list"] if x["symbol"].endswith("USDT"))
    r = df.filter(df["symbol"] == t["symbol"]).to_dicts()[0]
    assert r["oi_usd"] == pytest.approx(float(t["openInterestValue"]))


def test_okx_perps_parse():
    oi, tick, mark = body("okx.oi"), body("okx.tickers.swap"), body("okx.mark")
    usdt = [r for r in oi["data"] if r["instId"].endswith("-USDT-SWAP")]
    # parse keys by instId, so synthesise distinct ids to pass the >= 100 rows sanity check
    oi["data"] = [
        dict(r, instId=f"{r['instId'].split('-')[0]}{i}-USDT-SWAP") for i in range(30) for r in usdt
    ]
    df = okx.parse_perps(env("okx", "perps", [oi, tick, mark]))
    assert (df["symbol"].str.ends_with("-USDT-SWAP")).all()
    assert (df["oi_usd"] > 0).all()


def test_listings_parse_all_venues():
    spot, fut = body("binance.spot.exchangeInfo.full"), body("binance.fut.exchangeInfo")
    spot["symbols"], fut["symbols"] = spot["symbols"] * 30, fut["symbols"] * 10
    df = binance.parse_listings(
        env("binance_spot", "listings", [spot]), env("binance_usdm", "listings", [fut]), "sha"
    )
    assert set(df["market"].unique()) >= {"spot", "perp"}
    by = body("bybit.instruments.spot")
    lin = body("bybit.instruments")
    by["result"]["list"] = by["result"]["list"] * 10
    lin["result"]["list"] = lin["result"]["list"] * 10
    assert bybit.parse_listings(env("bybit", "listings", [by, lin])).height > 0
    o = body("okx.instruments.spot")
    sw = body("okx.instruments.swap")
    sw["data"] = sw["data"] * 40
    assert (
        okx.parse_listings(env("okx", "listings", [o, sw]))
        .filter(pl.col("market") == "perp")["multiplier"]
        .min()
        > 0
    )
    cb = body("coinbase.products") * 15
    assert coinbase.parse_listings(env("coinbase", "listings", [cb])).height >= 200
    kr = body("kraken.assetpairs")
    kr["result"] = {f"{k}{i}": v for i in range(20) for k, v in kr["result"].items()}
    kdf = kraken.parse_listings(env("kraken", "listings", [kr]))
    assert "BTC" in kdf["base"].to_list() or kdf.height > 0


def test_klines_parse_skip_open_candle():
    b = body("binance.spot.klines.1d")
    fetched = datetime.fromtimestamp(
        b[-1][0] / 1000, tz=UTC
    )  # "now" = open time of the last candle -> it is open
    df = binance.parse_klines_1d(
        env(
            "binance_spot", "klines_1d", [b], {"symbols": ["BTCUSDT"], "limit": 20}, fetched=fetched
        )
    )
    assert df.height == len(b) - 1 and df["base"][0] == "BTC"
    ok = body("okx.spot.candles.1Dutc")
    df = okx.parse_klines_1d(env("okx", "klines_1d", [ok], {"symbols": ["BTC-USDT"], "limit": 5}))
    assert df.height == sum(1 for k in ok["data"] if k[8] == "1")


def test_okx_listings_skip_unparseable_or_preopen_instruments():
    # seen live on 2026-09-06: a swap with empty instFamily and a pre-open instrument with empty ctVal
    sw = body("okx.instruments.swap")
    good = [dict(r) for r in sw["data"] if r.get("ctType") == "linear"][:1]
    assert good, "fixture needs one linear swap"
    weird = [
        dict(good[0], instId="JP225-USDT-SWAP", instFamily=""),
        dict(good[0], instId="NEW-USDT-SWAP", instFamily="NEW-USDT", ctVal=""),
    ]
    sw["data"] = good * 120 + weird
    df = okx.parse_listings(env("okx", "listings", [body("okx.instruments.spot"), sw]))
    syms = df.filter(pl.col("market") == "perp")["symbol"].to_list()
    assert "JP225-USDT-SWAP" in syms and "NEW-USDT-SWAP" not in syms
