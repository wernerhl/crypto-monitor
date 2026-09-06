"""Scheduled supply (notes Section 5) and context (§3.3, §12 event strip) on hand-built fixtures."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta

import polars as pl
import pytest

from monitor.compute import context as ctx
from monitor.compute import supply as sup

AS_OF = date(2026, 9, 6)
PI = {
    "team": 0.5,
    "investors": 0.7,
    "public": 0.5,
    "community": 0.6,
    "ecosystem": 0.2,
    "unknown": 0.7,
}


def _sched():
    return pl.DataFrame(
        [
            # id, date, kind, recipient_class, amount
            dict(
                id="a",
                date=AS_OF + timedelta(days=5),
                kind="cliff",
                recipient="VC",
                category="privateSale",
                recipient_class="investors",
                amount=1000.0,
            ),
            dict(
                id="a",
                date=AS_OF + timedelta(days=10),
                kind="cliff",
                recipient="Team",
                category="insiders",
                recipient_class="team",
                amount=400.0,
            ),
            dict(
                id="a",
                date=AS_OF + timedelta(days=60),
                kind="cliff",
                recipient="Eco",
                category="noncirculating",
                recipient_class="ecosystem",
                amount=5000.0,
            ),
            dict(
                id="b",
                date=AS_OF + timedelta(days=3),
                kind="cliff",
                recipient="?",
                category="mystery",
                recipient_class="unknown",
                amount=100.0,
            ),
        ]
    )


def test_esp_float_and_days_of_volume():
    # a, h=13: investors 1000×0.7 + team 400×0.5 = 900 expected sold; float 100 000 -> 0.009
    # price 2, ADV_real 300 -> 2×900/300 = 6 days of volume
    e = sup.esp(
        _sched(), AS_OF, 13, {"a": 100_000.0, "b": 1_000.0}, {"a": 2.0, "b": 1.0}, {"a": 300.0}, PI
    )
    r = {x["id"]: x for x in e.to_dicts()}
    assert r["a"]["expected_sold_tokens"] == pytest.approx(900.0) and r["a"][
        "esp_float"
    ] == pytest.approx(0.009)
    assert (
        r["a"]["esp_days_of_volume"] == pytest.approx(6.0)
        and r["a"]["n_events"] == 2
        and not r["a"]["pi_default_used"]
    )
    # b: unknown class -> pi 0.7 flagged; no ADV -> days of volume None
    assert (
        r["b"]["pi_default_used"]
        and r["b"]["esp_days_of_volume"] is None
        and r["b"]["esp_float"] == pytest.approx(70 / 1000)
    )
    # h=90 includes the ecosystem cliff: 900 + 5000×0.2 = 1900
    e90 = sup.esp(_sched(), AS_OF, 90, {"a": 100_000.0}, {"a": 2.0}, {"a": 300.0}, PI)
    assert e90.filter(pl.col("id") == "a")["expected_sold_tokens"][0] == pytest.approx(1900.0)


def test_dilution():
    # unlocks within 365 d for a: 1000 + 400 + 5000 = 6400; emissions 10/day -> 3650; float 100 000 -> 0.1005
    d = sup.dilution(_sched(), AS_OF, {"a": 100_000.0}, {"a": 10.0})
    assert d["dilution"][0] == pytest.approx(0.1005) and d["unlock_365"][0] == 6400.0


def test_cliffs_rule_inputs():
    c = sup.cliffs(_sched(), AS_OF, 28, {"a": 100_000.0}, {"a": 2.0}, {"a": 300.0})
    r = {x["date"]: x for x in c.filter(pl.col("id") == "a").to_dicts()}
    d5 = r[AS_OF + timedelta(days=5)]
    # 1000 tokens / 100 000 float = 1 % of float; 2×1000/300 = 6.67 days of volume
    assert d5["share_of_float"] == pytest.approx(0.01) and d5["days_of_volume"] == pytest.approx(
        2000 / 300
    )
    assert (AS_OF + timedelta(days=60)) not in r  # outside the 28-day horizon


def test_expand_linear_spreads_tranche_evenly():
    ev = pl.DataFrame(
        [
            dict(
                id="a",
                date=AS_OF,
                kind="linear_start",
                recipient="x",
                category="insiders",
                recipient_class="team",
                amount=400.0,
                source="s",
                fetched_at=datetime.now(UTC),
                git_sha="g",
            )
        ]
    )
    out = sup.expand_linear(ev, horizon_days=4)
    assert (
        out.height == 4
        and out["amount"].sum() == pytest.approx(400.0)
        and out["kind"].unique().to_list() == ["linear"]
    )
    assert out["date"].to_list() == [AS_OF + timedelta(days=k) for k in range(4)]


def test_macro_table_net_liquidity_and_changes():
    rows = []
    for sid, unit, v0, v1 in (
        ("WALCL", "musd", 6_700_000.0, 6_737_204.0),
        ("WTREGEN", "musd", 900_000.0, 967_935.0),
        ("RRPONTSYD", "busd", 5.0, 0.675),
    ):
        rows.append(
            dict(
                date=AS_OF - timedelta(days=35),
                value=v0,
                series_id=sid,
                series=sid.lower(),
                unit=unit,
                source="fred_csv",
            )
        )
        rows.append(
            dict(
                date=AS_OF - timedelta(days=2),
                value=v1,
                series_id=sid,
                series=sid.lower(),
                unit=unit,
                source="fred_csv",
            )
        )
    m, extra = ctx.macro_table(pl.DataFrame(rows), AS_OF)
    r = {x["series_id"]: x for x in m.to_dicts()}
    assert extra["net_liquidity_busd"] == pytest.approx(5768.594)
    # 4-week change: (37 204 − 67 935)/1e3 − (0.675 − 5) = −30.731 + 4.325 = −26.406
    assert r["NETLIQ"]["change_4w"] == pytest.approx(-26.406)
    assert r["WALCL"]["change_4w"] == pytest.approx(37_204.0)


def test_stablecoin_growth():
    h = pl.DataFrame(
        {
            "date": [AS_OF - timedelta(days=k) for k in range(40, -1, -1)],
            "total_usd": [100.0 + k for k in range(41)],
        }
    )
    g = ctx.stablecoin_growth(h, AS_OF, window=30)
    # last value 140 vs 30 days earlier 110 -> 27.27 %
    assert g["growth_30d"][-1] == pytest.approx(140 / 110 - 1)


def test_event_strip_orders_and_filters():
    cl = pl.DataFrame(
        [
            dict(
                id="a",
                date=AS_OF + timedelta(days=5),
                unlock_tokens=1000.0,
                classes=["investors"],
                share_of_float=0.01,
                days_of_volume=6.7,
                usd=2000.0,
            )
        ]
    )
    props = pl.DataFrame(
        [
            dict(
                id="p1",
                space="aave.eth",
                space_name="Aave",
                title="Proposal",
                state="active",
                start=datetime.now(UTC),
                end=datetime.combine(AS_OF + timedelta(days=2), datetime.min.time(), tzinfo=UTC),
                scores_total=1.0,
                link="l",
            )
        ]
    )
    manual = [
        {
            "date": str(AS_OF + timedelta(days=40)),
            "asset": "ETH",
            "kind": "upgrade",
            "title": "too far",
            "source": "u",
        },
        {
            "date": str(AS_OF + timedelta(days=1)),
            "asset": "ETH",
            "kind": "upgrade",
            "title": "soon",
            "source": "u",
        },
    ]
    exp = pl.DataFrame(
        [
            dict(
                currency="BTC",
                expiry=datetime.combine(
                    AS_OF + timedelta(days=19), datetime.min.time(), tzinfo=UTC
                ),
                total_oi=50000.0,
                max_oi_strike=80000.0,
            )
        ]
    )
    s = ctx.event_strip(AS_OF, 28, cl, props, manual, exp, {"a": "AAA"})
    assert s["kind"].to_list() == ["upgrade", "governance", "unlock cliff", "options expiry"]
    assert (
        "too far" not in s["title"].to_list()
        and s.filter(pl.col("kind") == "unlock cliff")["asset"][0] == "AAA"
    )
