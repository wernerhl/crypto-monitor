"""Websocket liquidation parsers and the hour buffer (work order B1)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

from monitor.fetch import binance, bybit
from monitor.fetch.base import Envelope, RawStore, Record
from monitor.fetch.liq_ws import HourBuffer


def _env(venue: str, body: list) -> Envelope:
    return Envelope(
        venue,
        "liquidations_ws",
        "2026/09/08/0700",
        "2026-09-08T07:59:00+00:00",
        "abc",
        [Record("wss://x", 101, "2026-09-08T07:59:00+00:00", body)],
    )


def test_binance_force_order_sell_closes_a_long_and_handles_multipliers():
    msgs = [
        {
            "e": "forceOrder",
            "E": 1757314800000,
            "o": {
                "s": "BTCUSDT",
                "S": "SELL",
                "q": "0.5",
                "p": "60000",
                "ap": "59990",
                "z": "0.5",
                "T": 1757314800123,
            },
        },
        {
            "e": "forceOrder",
            "E": 1757314801000,
            "o": {
                "s": "1000PEPEUSDT",
                "S": "BUY",
                "q": "1000",
                "p": "0.01",
                "ap": "0.01",
                "z": "1000",
                "T": 1757314801000,
            },
        },
    ]
    df = binance.parse_liquidations_ws(_env("binance", msgs))
    assert df.height == 2
    r = df.sort("ts").to_dicts()
    assert (
        r[0]["side_closed"] == "long"
        and r[0]["base"] == "BTC"
        and r[0]["notional_usd"] == 0.5 * 59990
    )
    assert r[0]["ts"] == datetime.fromtimestamp(1757314800.123, tz=UTC)
    assert r[1]["side_closed"] == "short" and r[1]["base"] == "PEPE"
    assert abs(r[1]["size_base"] - 1_000_000) < 1e-6 and abs(r[1]["notional_usd"] - 10.0) < 1e-9
    assert abs(r[1]["price"] - 1e-5) < 1e-12


def test_bybit_all_liquidation_buy_means_a_long_was_closed():
    msgs = [
        {
            "topic": "allLiquidation.ETHUSDT",
            "type": "snapshot",
            "ts": 1757314800000,
            "data": [
                {"T": 1757314800500, "s": "ETHUSDT", "S": "Buy", "v": "2", "p": "3000"},
                {"T": 1757314800600, "s": "ETHUSDT", "S": "Sell", "v": "1", "p": "3001"},
            ],
        }
    ]
    df = bybit.parse_liquidations_ws(_env("bybit", msgs))
    assert df.height == 2 and set(df["side_closed"]) == {"long", "short"}
    assert df.filter(df["side_closed"] == "long")["notional_usd"][0] == 6000.0
    assert (df["venue"] == "bybit").all()


def test_hour_buffer_rolls_over_and_merges_with_an_existing_envelope(tmp_path):
    store = RawStore(tmp_path)
    b = HourBuffer(store, "binance")
    t0 = datetime(2026, 9, 8, 7, 10, tzinfo=UTC)
    b.add({"n": 1}, t0)
    b.add({"n": 2}, t0.replace(minute=59))
    b.add({"n": 3}, t0.replace(hour=8, minute=0))  # rolls the 07:00 hour to disk
    p = store.path("binance_liquidations_ws", t0, "hourly")
    assert p.exists() and p.name == "binance_liquidations_ws_0700.json.gz"
    env = store.read(p)
    assert [m["n"] for r in env.records for m in r.body] == [1, 2]
    # a restart inside the same hour appends instead of overwriting
    b2 = HourBuffer(store, "binance")
    b2.add({"n": 9}, t0.replace(minute=30))
    b2.flush()
    env = store.read(p)
    assert [m["n"] for r in env.records for m in r.body] == [1, 2, 9]
    assert json.loads(json.dumps(env.to_json()))["meta"]["kind"] == "websocket"
    b.flush()
    assert store.path("binance_liquidations_ws", t0.replace(hour=8), "hourly").exists()


def test_binance_coin_m_forced_orders_are_converted_from_contracts():
    msgs = [
        {
            "e": "forceOrder",
            "E": 1757314800000,
            "o": {
                "s": "BTCUSD_PERP",
                "S": "SELL",
                "q": "50",
                "p": "80000",
                "ap": "80000",
                "z": "50",
                "T": 1757314800123,
            },
        },
        {
            "e": "forceOrder",
            "E": 1757314801000,
            "o": {
                "s": "ETHUSD_251226",
                "S": "BUY",
                "q": "30",
                "p": "4000",
                "ap": "4000",
                "z": "30",
                "T": 1757314801000,
            },
        },
    ]
    df = binance.parse_liquidations_ws(_env("binance", msgs))
    r = df.sort("ts").to_dicts()
    assert (
        r[0]["base"] == "BTC"
        and r[0]["notional_usd"] == 5000.0
        and abs(r[0]["size_base"] - 5000.0 / 80000) < 1e-12
    )
    assert (
        r[1]["base"] == "ETH" and r[1]["notional_usd"] == 300.0 and r[1]["side_closed"] == "short"
    )
