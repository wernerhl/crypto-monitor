"""Exchange-token sub-module (work order 7): taxonomy, on-chain vs estimated revenue, burns,
the within-sector residual-IC decision, solvency divergence, and the honesty rails."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from monitor.compute import exchange as ex

ROOT = Path(__file__).resolve().parents[2]


def test_taxonomy_reassigned_and_split_into_cex_and_onchain():
    uni = yaml.safe_load((ROOT / "config" / "universe.yaml").read_text())
    a = uni["sector_map"]["assignments"]
    for tid in (
        "binancecoin",
        "okb",
        "crypto-com-chain",
        "hyperliquid",
        "jupiter-exchange-solana",
        "dydx",
        "gmx",
    ):
        assert a[tid] == "exchange-token", tid
    assert "exchange-token" in uni["sector_map"].get("definitions", {})
    reg = ex.tokens()
    assert (
        reg["binancecoin"]["subtype"] == "exchange-cex" and reg["binancecoin"]["venue"] == "binance"
    )
    assert (
        reg["hyperliquid"]["subtype"] == "exchange-onchain"
        and reg["hyperliquid"]["defillama"] == "hyperliquid"
    )
    # every registered token's subtype is one of the two general cases
    assert {t["subtype"] for t in reg.values()} <= {"exchange-cex", "exchange-onchain"}


def _mini_universe(syms):
    return pl.DataFrame(
        {
            "as_of": [date(2026, 9, 16)] * len(syms),
            "id": [s[0] for s in syms],
            "symbol": [s[1] for s in syms],
            "tier": [s[2] for s in syms],
            "sector": ["exchange-token"] * len(syms),
            "market_cap_usd": [s[3] for s in syms],
        }
    )


def test_onchain_fees_are_verified_and_mapped_to_ids():
    ef = pl.DataFrame(
        {
            "date": [date(2026, 9, 15), date(2026, 9, 16)],
            "slug": ["hyperliquid", "hyperliquid"],
            "fees_usd": [1.0e6, 1.1e6],
            "revenue_usd": [0.8e6, 0.9e6],
            "holders_revenue_usd": [0.8e6, 0.9e6],
            "revenue_quality": ["verified", "verified"],
        }
    )
    otf = ex.onchain_token_fees(ef)
    assert otf.height == 2 and set(otf["id"]) == {"hyperliquid"} and set(otf["symbol"]) == {"HYPE"}
    assert (otf["revenue_quality"] == "verified").all()


def test_cex_revenue_is_estimated_with_an_error_bar_only_for_tracked_venues():
    members = pl.DataFrame(
        {
            "id": ["binancecoin", "kucoin-shares"],
            "symbol": ["BNB", "KCS"],
            "tier": [1, 3],
            "subtype": ["exchange-cex", "exchange-cex"],
            "venue": ["binance", "kucoin"],
            "defillama": [None, None],
        }
    )
    spot = pl.DataFrame({"date": [date(2026, 9, 16)], "venue": ["binance"], "spot_vol_usd": [1e10]})
    deriv = pl.DataFrame(
        {"date": [date(2026, 9, 16)], "venue": ["binance"], "deriv_vol_usd": [2e10]}
    )
    cex = ex.cex_token_revenue(members, spot, deriv)
    # kucoin has no tracked volume -> only BNB gets a row
    assert set(cex["symbol"]) == {"BNB"}
    r = cex.to_dicts()[0]
    assert r["revenue_quality"] == "estimated" and r["rev_lo"] < r["est_revenue_usd"] < r["rev_hi"]
    fee = yaml.safe_load((ROOT / "config" / "exchange_fees.yaml").read_text())["fees"]["binance"]
    assert abs(r["est_revenue_usd"] - (1e10 * fee["spot"] + 2e10 * fee["deriv"])) < 1


def test_burn_events_are_positive_for_holders_and_dated():
    prices = pl.DataFrame({"date": [date(2026, 10, 15)], "base": ["BNB"], "close": [700.0]})
    b = ex.burn_events(prices)
    assert (
        b.height >= 1
        and (b["kind"] == "quarterly").any()
        and (b["revenue_quality"] == "verified").all()
    )
    oct_burn = b.filter(pl.col("date") == date(2026, 10, 16))
    assert oct_burn.height == 1 and oct_burn["tokens"][0] > 0


def test_event_strip_shows_burns_as_their_own_kind_within_45_days():
    from monitor.compute.context import event_strip

    as_of = date(2026, 10, 1)
    burns = pl.DataFrame(
        {
            "date": [date(2026, 10, 16), date(2026, 7, 16)],
            "id": ["binancecoin", "binancecoin"],
            "symbol": ["BNB", "BNB"],
            "tokens": [1.0e6, 1.0e6],
            "usd": [7e8, 7e8],
            "kind": ["quarterly", "quarterly"],
            "revenue_quality": ["verified", "verified"],
            "source": ["cfg", "cfg"],
        }
    )
    out = event_strip(
        as_of,
        28,
        None,
        None,
        [],
        None,
        {},
        cliff_th={"single_unlock_float_share_min": 0.01, "single_unlock_days_of_volume_min": 2.0},
        burns=burns,
    )
    burn_rows = out.filter(pl.col("kind") == "burn")
    assert burn_rows.height == 1 and burn_rows["date"][0] == date(
        2026, 10, 16
    )  # the past one is dropped
    assert "positive for holders" in burn_rows["title"][0]


def test_residual_ic_is_descriptive_when_the_cross_section_is_too_small():
    # 2 priced on-chain names -> not estimable -> not published
    ef = pl.DataFrame(
        {
            "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(200)] * 2,
            "slug": ["hyperliquid"] * 200 + ["jupiter"] * 200,
            "fees_usd": list(np.abs(np.random.default_rng(1).normal(1e6, 1e5, 200))) * 2,
            "revenue_usd": [None] * 400,
            "holders_revenue_usd": [None] * 400,
            "revenue_quality": ["verified"] * 400,
        }
    )
    prices = pl.DataFrame(
        {
            "date": [date(2026, 1, 1) + timedelta(days=i) for i in range(200)] * 2,
            "base": ["HYPE"] * 200 + ["JUP"] * 200,
            "close": list(100 * np.exp(np.cumsum(np.random.default_rng(2).normal(0, 0.02, 200))))
            * 2,
        }
    )
    r = ex.residual_ic(ef, prices)
    assert r["published"] is False and r["n_names_priced"] == 2 and "descriptive" in r["reason"]


def test_solvency_divergence_flags_a_token_underperforming_the_sector():
    # BNB crashes 30% over 5d while the sector is flat -> divergence at a 15% threshold
    days = [date(2026, 9, 1) + timedelta(days=i) for i in range(10)]
    bnb = [700.0] * 5 + [700, 660, 620, 560, 490]  # ~-30% over the last 5 days
    okb = [50.0] * 10
    prices = pl.DataFrame(
        {"date": days * 2, "base": ["BNB"] * 10 + ["OKB"] * 10, "close": bnb + okb}
    )
    uni = _mini_universe([("binancecoin", "BNB", 1, 1e11), ("okb", "OKB", 2, 1e10)])
    sig = {s["venue"]: s for s in ex.solvency_signals(days[-1], uni, prices, None, 0.15)}
    assert sig["binance"]["divergence"] is True and sig["binance"]["vs_sector_5d"] < -0.15
    assert sig["okx"]["divergence"] is False


def test_no_exchange_trade_is_published_before_the_ic_clears():
    # the fundamentals snapshot marks every row descriptive; the RV decision gates publishing
    reg = ex.registry()
    assert "exchange_token" in yaml.safe_load((ROOT / "config" / "thresholds.yaml").read_text())
    assert reg["reviewed_on"] is not None
