"""Gated Coinglass adapter (work order 4, item 1): inert without a key; parsers on documented
response shapes (fixtures; not live captures)."""

from __future__ import annotations

import json
import os
from datetime import date
from pathlib import Path

import pytest

from monitor.fetch import coinglass
from monitor.fetch.base import Envelope

FX = Path(__file__).resolve().parents[1] / "fixtures" / "coinglass"


def _env(name: str) -> Envelope:
    return Envelope.from_json(json.loads((FX / f"{name}.json").read_text()))


def test_adapter_is_inert_without_a_key(monkeypatch):
    monkeypatch.delenv(coinglass.ENV_KEY, raising=False)
    assert coinglass.available() is False
    with pytest.raises(coinglass.KeyMissingError):
        coinglass.fetch_oi_history(["BTC"], 0, 1)


def test_parsers_read_the_documented_shapes():
    oi = coinglass.parse_oi_history(_env("oi_aggregated_history"))
    assert oi.height == 2 and oi["base"][0] == "BTC" and oi["oi_usd"][1] == 10_700_000_000.0
    assert oi["date"][0] == date(2021, 1, 1)
    f = coinglass.parse_funding_history(_env("funding_oi_weighted_history"))
    assert (
        f.height == 2
        and abs(f["funding_ann"][0] - 0.02 / 100 * 3 * 365) < 1e-12
        and f["funding_ann"][1] < 0
    )
    liq = coinglass.parse_liq_history(_env("liquidation_aggregated_history"))
    assert (
        liq.height == 2
        and liq["long_liq_usd"][0] == 12_345_678.9
        and liq["long_liq_usd"][1] is None
    )


@pytest.mark.skipif(
    not os.environ.get(coinglass.ENV_KEY), reason="COINGLASS_API_KEY not set; live check skipped"
)
def test_live_oi_history_endpoint_when_key_present(tmp_path):  # pragma: no cover - keyed
    from monitor.fetch.base import RawStore

    p = coinglass.fetch_oi_history(["BTC"], 1609459200000, 1612137600000, force=True)
    env = RawStore().read(p)
    df = coinglass.parse_oi_history(env)
    assert df.height > 0 and df["oi_usd"].min() > 0
