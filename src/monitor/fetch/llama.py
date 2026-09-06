"""DefiLlama adapters — stablecoins (`stablecoins.llama.fi`), unlocks (`defillama-datasets.llama.fi`,
the free host; `api.llama.fi/emissions*` is paid), fees/revenue and TVL (`api.llama.fi`).
All verified 2026-09-06 (docs/data_sources.md §4–5)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset

CATEGORY_TO_CLASS = {
    "insiders": "team",
    "team": "team",
    "privateSale": "investors",
    "investors": "investors",
    "publicSale": "public",
    "airdrop": "community",
    "noncirculating": "ecosystem",
    "farming": "ecosystem",
    "liquidity": "ecosystem",
    "ecosystem": "ecosystem",
    "treasury": "ecosystem",
    "foundation": "ecosystem",
}


def _s(x: int | str) -> datetime:
    return datetime.fromtimestamp(int(x), tz=UTC)


# --------------------------------------------------------------------------- stablecoins
def fetch_stablecoins(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "llama_stables",
        "stablecoins",
        "daily",
        lambda c: [
            c.get("/stablecoins", params={"includePrices": "true"}),
            c.get("/stablecoincharts/all"),
        ],
        ts=ts,
        force=force,
    )


def parse_stablecoins(env: Envelope) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (per-coin snapshot, total daily history)."""
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for a in env.records[0].body["peggedAssets"]:
        if a.get("pegType") != "peggedUSD":
            continue
        circ = (a.get("circulating") or {}).get("peggedUSD")
        prev_m = (a.get("circulatingPrevMonth") or {}).get("peggedUSD")
        rows.append(
            {
                "as_of": fetched.date(),
                "id": str(a["id"]),
                "symbol": a["symbol"],
                "name": a["name"],
                "gecko_id": a.get("gecko_id"),
                "peg_mechanism": a.get("pegMechanism"),
                "supply_usd": circ,
                "supply_prev_day": (a.get("circulatingPrevDay") or {}).get("peggedUSD"),
                "supply_prev_week": (a.get("circulatingPrevWeek") or {}).get("peggedUSD"),
                "supply_prev_month": prev_m,
                "price": a.get("price"),
                "discount_to_par": (1.0 - a["price"]) if a.get("price") else None,
                "source": "llama_stables",
                "fetched_at": fetched,
                "git_sha": env.git_sha,
            }
        )
    snap = pl.DataFrame(rows)
    hist = pl.DataFrame(
        [
            {
                "date": _s(r["date"]).date(),
                "total_usd": r["totalCirculatingUSD"]["peggedUSD"],
                "source": "llama_stables",
                "fetched_at": fetched,
                "git_sha": env.git_sha,
            }
            for r in env.records[1].body
            if r.get("totalCirculatingUSD", {}).get("peggedUSD")
        ]
    )
    if snap.height < 20 or hist.height < 1000:
        raise SanityError("llama stablecoins: too few rows")
    return snap, hist


# --------------------------------------------------------------------------- unlocks
def fetch_unlocks(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "llama_datasets",
        "unlocks",
        "daily",
        lambda c: [c.get("/emissionsIndex")],
        ts=ts,
        force=force,
    )


def parse_unlocks(env: Envelope) -> tuple[pl.DataFrame, pl.DataFrame]:
    """Returns (schedule of future cliff/linear unlock events by recipient class, supply summary).
    Class mapping from DefiLlama `category` (CATEGORY_TO_CLASS); unknown -> 'unknown' (flagged)."""
    fetched = datetime.fromisoformat(env.fetched_at)
    ev_rows, sup_rows = [], []
    for rec in env.records[0].body["data"]:
        gecko = rec.get("gecko_id") or (
            rec.get("token", "").split(":")[1]
            if rec.get("token", "").startswith("coingecko:")
            else None
        )
        if not gecko:
            continue
        sup_rows.append(
            {
                "as_of": fetched.date(),
                "id": gecko,
                "protocol": rec.get("protocolSlug"),
                "circ_supply": rec.get("circSupply"),
                "circ_supply_30d_ago": rec.get("circSupply30d"),
                "total_locked": rec.get("totalLocked"),
                "max_supply": rec.get("maxSupply"),
                "unlocks_per_day": rec.get("unlocksPerDay"),
                "source": "llama_datasets",
                "fetched_at": fetched,
                "git_sha": env.git_sha,
            }
        )
        for ev in rec.get("unlockEvents") or []:
            when = _s(ev["timestamp"]).date()
            for a in ev.get("cliffAllocations") or []:
                cat = a.get("category") or "unknown"
                ev_rows.append(
                    {
                        "id": gecko,
                        "date": when,
                        "kind": "cliff",
                        "recipient": a.get("recipient"),
                        "category": cat,
                        "recipient_class": CATEGORY_TO_CLASS.get(cat, "unknown"),
                        "amount": float(a.get("amount") or 0),
                        "source": "llama_datasets",
                        "fetched_at": fetched,
                        "git_sha": env.git_sha,
                    }
                )
            for a in ev.get("linearAllocations") or []:
                cat = a.get("category") or "unknown"
                # linear vesting: DefiLlama reports the tranche starting at this event; store its per-day rate
                ev_rows.append(
                    {
                        "id": gecko,
                        "date": when,
                        "kind": "linear_start",
                        "recipient": a.get("recipient"),
                        "category": cat,
                        "recipient_class": CATEGORY_TO_CLASS.get(cat, "unknown"),
                        "amount": float(a.get("amount") or 0),
                        "source": "llama_datasets",
                        "fetched_at": fetched,
                        "git_sha": env.git_sha,
                    }
                )
    ev = pl.DataFrame(
        ev_rows,
        schema={
            "id": pl.Utf8,
            "date": pl.Date,
            "kind": pl.Utf8,
            "recipient": pl.Utf8,
            "category": pl.Utf8,
            "recipient_class": pl.Utf8,
            "amount": pl.Float64,
            "source": pl.Utf8,
            "fetched_at": pl.Datetime("us", "UTC"),
            "git_sha": pl.Utf8,
        },
    )
    sup = pl.DataFrame(sup_rows)
    if sup.height < 100:
        raise SanityError("llama unlocks: too few tokens")
    return ev, sup


def fetch_unlock_detail(slugs: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [c.get(f"/emissions/{s}") for s in slugs]

    return run_dataset(
        "llama_datasets", "unlock_detail", "daily", go, ts=ts, force=force, meta={"slugs": slugs}
    )


def parse_unlock_detail(env: Envelope) -> pl.DataFrame:
    """Daily unlocked amount by label (documentedData), with the label's class from `categories`."""
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = []
    for rec, slug in zip(env.records, env.meta["slugs"], strict=True):
        b = rec.body
        if not isinstance(b, dict) or "documentedData" not in b:
            continue
        cats = b.get("categories") or {}
        label_class = {
            lab: CATEGORY_TO_CLASS.get(cat, "unknown") for cat, labs in cats.items() for lab in labs
        }
        gecko = b.get("gecko_id") or ((b.get("metadata") or {}).get("token", "") or "")
        for series in b["documentedData"]["data"]:
            lab = series["label"]
            prev = None
            for pt in series["data"]:
                cum = pt.get("unlocked")
                if cum is None:
                    continue
                inc = cum - prev if prev is not None else 0.0
                prev = cum
                if inc > 0:
                    rows.append(
                        {
                            "protocol": slug,
                            "gecko_id": gecko,
                            "date": _s(pt["timestamp"]).date(),
                            "label": lab,
                            "recipient_class": label_class.get(lab, "unknown"),
                            "amount": float(inc),
                            "source": "llama_datasets",
                            "fetched_at": fetched,
                            "git_sha": env.git_sha,
                        }
                    )
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "protocol": pl.Utf8,
                "gecko_id": pl.Utf8,
                "date": pl.Date,
                "label": pl.Utf8,
                "recipient_class": pl.Utf8,
                "amount": pl.Float64,
                "source": pl.Utf8,
                "fetched_at": pl.Datetime("us", "UTC"),
                "git_sha": pl.Utf8,
            }
        )
    )


# --------------------------------------------------------------------------- fees and TVL
def fetch_fees_tvl(ts: datetime | None = None, force: bool = False) -> Path:
    return run_dataset(
        "llama_api",
        "fees_tvl",
        "daily",
        lambda c: [
            c.get(
                "/overview/fees",
                params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
            ),
            c.get("/protocols"),
        ],
        ts=ts,
        force=force,
    )


def parse_fees_tvl(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    fees = {p["slug"]: p for p in env.records[0].body["protocols"]}
    rows = []
    for p in env.records[1].body:
        f = fees.get(p.get("slug"), {})
        rows.append(
            {
                "as_of": fetched.date(),
                "protocol": p.get("slug"),
                "name": p.get("name"),
                "symbol": p.get("symbol"),
                "gecko_id": p.get("gecko_id"),
                "category": p.get("category"),
                "tvl_usd": p.get("tvl"),
                "fees_24h": f.get("total24h"),
                "fees_7d": f.get("total7d"),
                "fees_30d": f.get("total30d"),
                "fees_1y": f.get("total1y"),
                "source": "llama_api",
                "fetched_at": fetched,
                "git_sha": env.git_sha,
            }
        )
    df = pl.DataFrame(rows)
    if df.height < 1000:
        raise SanityError("llama protocols: too few rows")
    return df


def json_dumps(o) -> str:
    return json.dumps(o, default=str)


def as_date(d: date | datetime) -> date:
    return d.date() if isinstance(d, datetime) else d
