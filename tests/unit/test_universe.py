"""Tiering and exclusions (notes Section 2, build prompt §2) on hand-built fixtures.

Expected values are worked out by hand in the comments of each test.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest
import yaml

from monitor.compute import universe as univ
from monitor.paths import CONFIG

AS_OF = date(2026, 9, 6)
TS = datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def cfg() -> dict:
    c = yaml.safe_load((CONFIG / "universe.yaml").read_text())
    c["candidate_universe"]["top_n_by_market_cap"] = 5
    return c


def _markets() -> pl.DataFrame:
    rows = [
        # id, symbol, rank, mcap
        ("bitcoin", "BTC", 1, 1.6e12),
        ("tether", "USDT", 2, 1.8e11),
        ("ethereum", "ETH", 3, 3.0e11),
        ("wrapped-bitcoin", "WBTC", 4, 1.0e10),
        ("solana", "SOL", 5, 8.0e10),
        ("smallcap", "SML", 6, 1.0e8),
        ("tinycap", "TNY", 7, 5.0e7),
        ("dupe-sol", "SOL", 8, 1.0e7),
    ]
    return pl.DataFrame(
        [
            dict(
                as_of=AS_OF,
                id=i,
                symbol=s,
                name=i,
                rank=r,
                market_cap_usd=m,
                source="coingecko",
                fetched_at=TS,
                git_sha="t",
            )
            for i, s, r, m in rows
        ]
    )


def _meta() -> pl.DataFrame:
    cats = {
        "bitcoin": ["Layer 1 (L1)", "Proof of Work (PoW)"],
        "tether": ["Stablecoins", "USD Stablecoin", "Fiat-backed Stablecoin"],
        "ethereum": ["Smart Contract Platform"],
        "wrapped-bitcoin": ["Wrapped-Tokens", "Tokenized BTC"],
        "solana": ["Layer 1 (L1)"],
        "smallcap": ["DeFi"],
        "tinycap": [],
        # dupe-sol has no meta row -> "no category data yet"
    }
    return pl.DataFrame(
        {"id": list(cats), "categories": list(cats.values()), "fetched_at": [TS] * len(cats)}
    )


def _listings() -> pl.DataFrame:
    rows = []

    def add(venue, market, base, symbol, quote, status="TRADING"):
        rows.append(
            dict(
                ts=TS,
                venue=venue,
                market=market,
                symbol=symbol,
                base=base,
                quote=quote,
                multiplier=1.0,
                status=status,
            )
        )

    for v in ("binance", "bybit", "okx"):
        for b in ("BTC", "ETH", "SOL"):
            add(v, "perp", b, f"{b}USDT" if v != "okx" else f"{b}-USDT-SWAP", "USDT")
            add(v, "spot", b, f"{b}USDT" if v != "okx" else f"{b}-USDT", "USDT")
    add("binance", "perp", "SML", "SMLUSDT", "USDT")  # only one perp venue
    add("binance", "spot", "SML", "SMLUSDT", "USDT")
    add("coinbase", "spot", "SML", "SML-USD", "USD")
    add("kraken", "spot", "BTC", "XBTUSD", "USD")
    add("coinbase", "spot", "BTC", "BTC-USD", "USD")
    add("binance", "spot", "TNY", "TNYUSDT", "USDT")  # one spot venue only
    add("binance", "spot", "ETH", "ETHBUSD", "BUSD")  # wrong quote, must be ignored
    add("bybit", "perp", "ETH", "ETHUSDT-DELISTED", "USDT", status="Closed")  # not live
    return pl.DataFrame(rows)


def test_exclusions(cfg):
    m = univ.classify_exclusions(_markets(), _meta(), cfg)
    r = dict(zip(m["id"], m["excluded_reason"], strict=True))
    assert r["tether"] == "stablecoin"
    assert r["wrapped-bitcoin"] == "wrapped / liquid-staking derivative"
    assert r["bitcoin"] is None and r["ethereum"] is None and r["solana"] is None
    assert r["dupe-sol"] is None  # no metadata yet: kept, flagged pending
    assert m.filter(pl.col("id") == "dupe-sol")["meta_status"][0] == "pending"
    assert m.filter(pl.col("is_stablecoin"))["id"].to_list() == ["tether"]


def test_category_membership_lists_exclude_without_per_coin_meta(cfg):
    members = pl.DataFrame(
        {"category_id": ["stablecoins", "wrapped-tokens"], "id": ["dupe-sol", "tinycap"]}
    )
    m = univ.classify_exclusions(_markets(), None, cfg, members)
    r = dict(zip(m["id"], m["excluded_reason"], strict=True))
    assert r["dupe-sol"] == "stablecoin" and r["tinycap"] == "wrapped / liquid-staking derivative"
    assert r["bitcoin"] is None and (m["meta_status"] == "categories").all()


def test_manual_include_overrides_category(cfg):
    cfg["exclusions"]["manual"] = {"tether": "include"}
    m = univ.classify_exclusions(_markets(), _meta(), cfg)
    assert m.filter(pl.col("id") == "tether")["excluded_reason"][0] is None


def test_resolve_symbols_uses_live_quote_filtered_listings_and_rank_priority():
    sm = univ.resolve_symbols(_markets(), _listings())
    d = {r["id"]: r for r in sm.to_dicts()}
    assert d["bitcoin"]["perp"] == {
        "binance": "BTCUSDT",
        "bybit": "BTCUSDT",
        "okx": "BTC-USDT-SWAP",
    }
    assert (
        d["bitcoin"]["spot"]["kraken"] == "XBTUSD" and d["bitcoin"]["spot"]["coinbase"] == "BTC-USD"
    )
    # ETHBUSD (wrong quote) ignored; the closed bybit perp ignored -> bybit spot still ETHUSDT
    assert d["ethereum"]["spot"]["binance"] == "ETHUSDT"
    assert d["ethereum"]["perp"]["bybit"] == "ETHUSDT"
    # the second asset with symbol SOL gets nothing
    assert not any(d["dupe-sol"]["perp"].values()) and "already claimed" in d["dupe-sol"]["note"]


def test_oi_median_and_window():
    # 3 days of snapshots for BTC on two venues: day1 100+50, day2 120+60, day3 80+40
    # daily aggregates 150, 180, 120 -> median 150, window 3
    sm = univ.resolve_symbols(_markets(), _listings())
    rows = []
    for k, (a, b) in enumerate([(100.0, 50.0), (120.0, 60.0), (80.0, 40.0)]):
        t = TS - timedelta(days=2 - k)
        rows.append(dict(ts=t, venue="binance", symbol="BTCUSDT", oi_usd=a))
        rows.append(dict(ts=t, venue="bybit", symbol="BTCUSDT", oi_usd=b))
        rows.append(
            dict(ts=t - timedelta(hours=3), venue="bybit", symbol="BTCUSDT", oi_usd=999.0)
        )  # earlier same-day snapshot, superseded
    perps = pl.DataFrame(rows).with_columns(pl.col("ts").cast(pl.Datetime("us", "UTC")))
    oi = univ.oi_metrics(perps, sm, AS_OF)
    row = oi.filter(pl.col("id") == "bitcoin").to_dicts()[0]
    assert row["oi_median_usd"] == 150.0 and row["oi_window_days"] == 3


def test_adv_uses_quote_volume_or_base_times_close():
    # binance: quote volumes 10, 20 ; coinbase: base*close = 2*5=10, 4*5=20 -> daily sums 20, 40 -> mean 30
    sm = univ.resolve_symbols(_markets(), _listings())
    d1, d2 = AS_OF - timedelta(days=2), AS_OF - timedelta(days=1)
    prices = pl.DataFrame(
        [
            dict(
                date=d1,
                venue="binance",
                symbol="BTCUSDT",
                close=5.0,
                volume_base=2.0,
                volume_quote=10.0,
            ),
            dict(
                date=d2,
                venue="binance",
                symbol="BTCUSDT",
                close=5.0,
                volume_base=4.0,
                volume_quote=20.0,
            ),
            dict(
                date=d1,
                venue="coinbase",
                symbol="BTC-USD",
                close=5.0,
                volume_base=2.0,
                volume_quote=None,
            ),
            dict(
                date=d2,
                venue="coinbase",
                symbol="BTC-USD",
                close=5.0,
                volume_base=4.0,
                volume_quote=None,
            ),
        ]
    )
    adv = univ.adv_metrics(prices, sm, AS_OF)
    row = adv.filter(pl.col("id") == "bitcoin").to_dicts()[0]
    assert row["adv_30d_usd"] == 30.0 and row["adv_window_days"] == 2


def test_tiering_rules(cfg):
    # thresholds: tier1 OI >= 100M on >= 2 perp venues (depth pending), tier2 ADV >= 10M on >= 2 spot venues
    m = univ.classify_exclusions(_markets(), _meta(), cfg)
    sm = univ.resolve_symbols(m, _listings())
    oi = pl.DataFrame(
        {
            "id": ["bitcoin", "ethereum", "solana", "smallcap"],
            "oi_median_usd": [5e9, 2e9, 5e7, 3e8],
            "oi_window_days": [3, 3, 3, 3],
        }
    )
    adv = pl.DataFrame(
        {
            "id": ["bitcoin", "ethereum", "solana", "smallcap", "tinycap"],
            "adv_30d_usd": [1e10, 5e9, 5e8, 2e7, 5e7],
            "adv_window_days": [2] * 5,
        }
    )
    uni = univ.tier(m, sm, oi, adv, cfg, AS_OF, source="coingecko", fetched_at=TS, git_sha="t")
    t = dict(zip(uni["id"], uni["tier"], strict=True))
    # BTC, ETH: 3 perp venues, OI above threshold -> 1
    assert t["bitcoin"] == 1 and t["ethereum"] == 1
    # SOL: OI median 50M < 100M -> not tier 1; 3 spot venues and ADV 500M -> 2
    assert t["solana"] == 2
    # smallcap: only one perp venue -> not tier 1; spot on binance+coinbase, ADV 20M -> 2
    assert t["smallcap"] == 2
    # tinycap: outside top 5 after exclusions (bitcoin, ethereum, solana, smallcap, tinycap = 5th) -> in; one spot venue -> 3
    assert t["tinycap"] == 3
    # excluded assets have no tier
    assert t["tether"] is None and t["wrapped-bitcoin"] is None
    # dupe-sol: no metadata -> kept (pending) but its symbol is claimed by solana, so no venues -> tier 3... unless outside top-N
    assert t["dupe-sol"] is None  # 6th after exclusions -> outside top 5
    assert (uni["as_of"] == AS_OF).all()
    assert (uni.filter(pl.col("tier") == 1)["depth_status"] == "pending_phase3").all()


def test_no_stablecoin_or_wrapped_asset_in_any_tier(cfg):
    m = univ.classify_exclusions(_markets(), _meta(), cfg)
    sm = univ.resolve_symbols(m, _listings())
    oi = pl.DataFrame(
        {
            "id": ["tether", "wrapped-bitcoin"],
            "oi_median_usd": [1e12, 1e12],
            "oi_window_days": [30, 30],
        }
    )
    adv = pl.DataFrame(
        {
            "id": ["tether", "wrapped-bitcoin"],
            "adv_30d_usd": [1e12, 1e12],
            "adv_window_days": [30, 30],
        }
    )
    uni = univ.tier(m, sm, oi, adv, cfg, AS_OF, source="coingecko", fetched_at=TS, git_sha="t")
    bad = uni.filter(
        pl.col("tier").is_not_null()
        & (
            pl.col("excluded_reason").is_not_null()
            | pl.col("id").is_in(["tether", "wrapped-bitcoin"])
        )
    )
    assert bad.height == 0
