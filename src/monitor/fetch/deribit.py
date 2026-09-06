"""Deribit public API v2 adapter. Verified 2026-09-06 (docs/data_sources.md §2):
`get_instruments`, `get_book_summary_by_currency` (option and future), `get_volatility_index_data`,
`get_index_price`. Delta is not in the book summary; `delta_est` is Black-76 from `mark_iv`
(compute.positioning.black76_delta), checked in tests against `ticker.greeks.delta`."""

from __future__ import annotations

import math
from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.schema.tables import FuturesMarkRow, OptionRow, rows_to_df

VENUE = "deribit"


def _ms(x: int | float) -> datetime:
    return datetime.fromtimestamp(int(x) / 1000, tz=UTC)


def fetch_options(
    currencies: tuple[str, ...] = ("BTC", "ETH"),
    ts: datetime | None = None,
    force: bool = False,
    freq: str = "hourly",
) -> Path:
    now_ms = int((ts or datetime.now(UTC)).timestamp() * 1000)

    def go(c) -> list[Record]:
        recs = []
        for cur in currencies:
            recs.append(
                c.get(
                    "/public/get_instruments",
                    params={"currency": cur, "kind": "option", "expired": "false"},
                )
            )
            recs.append(
                c.get(
                    "/public/get_book_summary_by_currency",
                    params={"currency": cur, "kind": "option"},
                )
            )
            recs.append(
                c.get(
                    "/public/get_book_summary_by_currency",
                    params={"currency": cur, "kind": "future"},
                )
            )
            recs.append(
                c.get(
                    "/public/get_volatility_index_data",
                    params={
                        "currency": cur,
                        "resolution": 3600,
                        "start_timestamp": now_ms - 2 * 86_400_000,
                        "end_timestamp": now_ms,
                    },
                )
            )
            recs.append(
                c.get("/public/get_index_price", params={"index_name": f"{cur.lower()}_usd"})
            )
        return recs

    return run_dataset(
        VENUE, "options", freq, go, ts=ts, force=force, meta={"currencies": list(currencies)}
    )


def black76_delta(f: float, k: float, t: float, iv: float, call: bool) -> float:
    """Black-76 delta with zero rate: Φ(d1) for calls, Φ(d1) − 1 for puts;
    d1 = [ln(F/K) + σ²T/2] / (σ√T). `iv` in decimals (0.55), `t` in years."""
    if t <= 0 or iv <= 0 or f <= 0 or k <= 0:
        return 1.0 if (call and f > k) else (-1.0 if (not call and f < k) else 0.0)
    d1 = (math.log(f / k) + 0.5 * iv * iv * t) / (iv * math.sqrt(t))
    nd1 = 0.5 * (1.0 + math.erf(d1 / math.sqrt(2.0)))
    return nd1 if call else nd1 - 1.0


def parse_options(env: Envelope) -> tuple[pl.DataFrame, pl.DataFrame, pl.DataFrame]:
    """Returns (options, futures_marks, dvol) DataFrames."""
    fetched = datetime.fromisoformat(env.fetched_at)
    per_cur = 5
    opt_rows: list[OptionRow] = []
    fut_rows: list[FuturesMarkRow] = []
    dvol_rows: list[dict] = []
    for i, cur in enumerate(env.meta["currencies"]):
        inst, summ, futs, dvol, idx = (r.body for r in env.records[i * per_cur : (i + 1) * per_cur])
        meta = {x["instrument_name"]: x for x in inst["result"]}
        index_price = idx["result"]["index_price"]
        ts = _ms(summ["usOut"] / 1000) if "usOut" in summ else fetched
        for s in summ["result"]:
            m = meta.get(s["instrument_name"])
            if not m:
                continue
            expiry = _ms(m["expiration_timestamp"])
            t = max((expiry - ts).total_seconds(), 0) / (365.0 * 86400)
            iv = s.get("mark_iv")
            f = s.get("underlying_price")
            delta = (
                black76_delta(f, m["strike"], t, iv / 100.0, m["option_type"] == "call")
                if (iv and f)
                else None
            )
            opt_rows.append(
                OptionRow(
                    ts=ts,
                    currency=cur,
                    instrument=s["instrument_name"],
                    expiry=expiry,
                    strike=float(m["strike"]),
                    option_type=m["option_type"],
                    mark_iv=iv,
                    bid_iv=s.get("bid_iv"),
                    ask_iv=s.get("ask_iv"),
                    open_interest=s.get("open_interest"),
                    underlying_price=f,
                    mark_price=s.get("mark_price"),
                    delta_est=delta,
                    t_years=t,
                    source=VENUE,
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
        for fsum in futs["result"]:
            name = fsum["instrument_name"]
            if name.endswith("PERPETUAL"):
                continue
            # BTC-25DEC26 -> expiry 08:00 UTC on that date (Deribit convention; verified in instruments)
            exp = datetime.strptime(name.split("-")[1], "%d%b%y").replace(hour=8, tzinfo=UTC)
            fut_rows.append(
                FuturesMarkRow(
                    ts=ts,
                    venue=VENUE,
                    symbol=name,
                    base=cur,
                    expiry=exp,
                    mark_price=fsum["mark_price"],
                    index_price=index_price,
                    settle_ccy=cur,
                    source=VENUE,
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
        for row in dvol["result"]["data"]:
            dvol_rows.append(
                {
                    "ts": _ms(row[0]),
                    "currency": cur,
                    "dvol": float(row[4]),
                    "source": VENUE,
                    "fetched_at": fetched,
                    "git_sha": env.git_sha,
                }
            )
    opts = rows_to_df(OptionRow, opt_rows)
    if opts.filter(pl.col("currency") == "BTC").height < 100:
        raise SanityError("deribit options: too few BTC instruments")
    dv = pl.DataFrame(
        dvol_rows,
        schema={
            "ts": pl.Datetime("us", "UTC"),
            "currency": pl.Utf8,
            "dvol": pl.Float64,
            "source": pl.Utf8,
            "fetched_at": pl.Datetime("us", "UTC"),
            "git_sha": pl.Utf8,
        },
    )
    return opts, rows_to_df(FuturesMarkRow, fut_rows), dv
