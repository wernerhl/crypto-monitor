"""Coinglass (keyed, optional) — aggregated open interest, OI-weighted funding and aggregated
liquidation history back to 2021 (work order 4, item 1). The adapter is INERT without
`COINGLASS_API_KEY`: `available()` is false, the fetchers raise `KeyMissingError`, and nothing in
the jobs depends on it. With a key, `backfill.rebuild_leverage_history` writes
`coinglass_oi_history`, `coinglass_funding_history` and `coinglass_liq_history`, and the
fragility builder extends its funding and OI/cap inputs back to 2021 so the leverage
components get walk-forward history and the validation can be re-run.

Endpoints (Coinglass API v4, documented; NOT live-verified in this build because no key was
available — see docs/data_sources.md): base https://open-api-v4.coinglass.com, header
`CG-API-KEY`, `GET /api/futures/open-interest/aggregated-history?symbol=BTC&interval=1d`,
`GET /api/futures/funding-rate/oi-weight-history?symbol=BTC&interval=1d`,
`GET /api/futures/liquidation/aggregated-history?symbol=BTC&interval=1d`; responses are
`{"code": "0", "msg": "success", "data": [...]}` with millisecond `time` fields. The parsers
accept the documented field names and their older aliases and drop rows they cannot read."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, run_dataset

SOURCE = "coinglass"
BASE = "https://open-api-v4.coinglass.com"
ENV_KEY = "COINGLASS_API_KEY"


class KeyMissingError(RuntimeError):
    pass


def available() -> bool:
    return bool(os.environ.get(ENV_KEY))


def _require_key() -> None:
    if not available():
        raise KeyMissingError(f"{ENV_KEY} not set; the Coinglass adapter is inert")


def _fetch(
    dataset: str, path: str, symbols: list[str], start_ms: int, end_ms: int, ts, force
) -> Path:
    _require_key()  # the client adds the CG-API-KEY header from config/sources.yaml

    def go(c) -> list[Record]:
        return [
            c.get(
                path,
                params={
                    "symbol": sym,
                    "interval": "1d",
                    "start_time": start_ms,
                    "end_time": end_ms,
                    "limit": 4500,
                },
            )
            for sym in symbols
        ]

    return run_dataset(SOURCE, dataset, "daily", go, ts=ts, force=force)


def fetch_oi_history(symbols, start_ms, end_ms, ts=None, force=False) -> Path:
    return _fetch(
        "oi_aggregated_history",
        "/api/futures/open-interest/aggregated-history",
        symbols,
        start_ms,
        end_ms,
        ts,
        force,
    )


def fetch_funding_history(symbols, start_ms, end_ms, ts=None, force=False) -> Path:
    return _fetch(
        "funding_oi_weighted_history",
        "/api/futures/funding-rate/oi-weight-history",
        symbols,
        start_ms,
        end_ms,
        ts,
        force,
    )


def fetch_liq_history(symbols, start_ms, end_ms, ts=None, force=False) -> Path:
    return _fetch(
        "liquidation_aggregated_history",
        "/api/futures/liquidation/aggregated-history",
        symbols,
        start_ms,
        end_ms,
        ts,
        force,
    )


def _sym(rec: Record) -> str:
    q = rec.url.split("symbol=")[-1].split("&")[0] if "symbol=" in rec.url else ""
    return q.upper()


def _rows(rec: Record) -> list[dict]:
    body = rec.body if isinstance(rec.body, dict) else {}
    if str(body.get("code", "0")) not in ("0", "200"):
        return []
    data = body.get("data")
    return data if isinstance(data, list) else []


def _day(ms) -> datetime | None:
    try:
        return datetime.fromtimestamp(int(ms) / 1000, tz=UTC)
    except (TypeError, ValueError, OSError):
        return None


def _num(d: dict, *keys) -> float | None:
    for k in keys:
        if k in d and d[k] is not None:
            try:
                return float(d[k])
            except (TypeError, ValueError):
                return None
    return None


def parse_oi_history(env: Envelope) -> pl.DataFrame:
    """date, base, oi_usd (aggregated across exchanges; the daily close of the OI candle)."""
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        base = _sym(rec)
        for d in _rows(rec):
            t = _day(d.get("time") or d.get("t"))
            v = _num(d, "close", "c", "openInterest", "open_interest")
            if t is None or v is None:
                continue
            rows.append(
                {
                    "date": t.date(),
                    "base": base,
                    "oi_usd": v,
                    "source": SOURCE,
                    "fetched_at": fetched,
                    "git_sha": env.git_sha,
                }
            )
    schema = {
        "date": pl.Date,
        "base": pl.Utf8,
        "oi_usd": pl.Float64,
        "source": pl.Utf8,
        "fetched_at": pl.Datetime("us", "UTC"),
        "git_sha": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def parse_funding_history(env: Envelope, periods_per_day: int = 3) -> pl.DataFrame:
    """date, base, funding_ann: the OI-weighted funding candle close (rate per period, in
    percent per the docs) annualised as rate/100 × periods_per_day × 365."""
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        base = _sym(rec)
        for d in _rows(rec):
            t = _day(d.get("time") or d.get("t"))
            v = _num(d, "close", "c", "fundingRate", "funding_rate")
            if t is None or v is None:
                continue
            rows.append(
                {
                    "date": t.date(),
                    "base": base,
                    "funding_ann": v / 100.0 * periods_per_day * 365.0,
                    "source": SOURCE,
                    "fetched_at": fetched,
                    "git_sha": env.git_sha,
                }
            )
    schema = {
        "date": pl.Date,
        "base": pl.Utf8,
        "funding_ann": pl.Float64,
        "source": pl.Utf8,
        "fetched_at": pl.Datetime("us", "UTC"),
        "git_sha": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def parse_liq_history(env: Envelope) -> pl.DataFrame:
    """date, base, long_liq_usd, short_liq_usd (aggregated across exchanges)."""
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec in env.records:
        base = _sym(rec)
        for d in _rows(rec):
            t = _day(d.get("time") or d.get("t"))
            lo = _num(
                d, "aggregated_long_liquidation_usd", "longLiquidationUsd", "long_liquidation_usd"
            )
            sh = _num(
                d,
                "aggregated_short_liquidation_usd",
                "shortLiquidationUsd",
                "short_liquidation_usd",
            )
            if t is None or (lo is None and sh is None):
                continue
            rows.append(
                {
                    "date": t.date(),
                    "base": base,
                    "long_liq_usd": lo,
                    "short_liq_usd": sh,
                    "source": SOURCE,
                    "fetched_at": fetched,
                    "git_sha": env.git_sha,
                }
            )
    schema = {
        "date": pl.Date,
        "base": pl.Utf8,
        "long_liq_usd": pl.Float64,
        "short_liq_usd": pl.Float64,
        "source": pl.Utf8,
        "fetched_at": pl.Datetime("us", "UTC"),
        "git_sha": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)
