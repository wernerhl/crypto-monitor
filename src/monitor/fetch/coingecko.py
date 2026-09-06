"""CoinGecko adapter (free tier; optional demo key via COINGECKO_DEMO_KEY).
Verified 2026-09-06: `coins/markets` (250/page), `coins/{id}` for categories, `market_chart`
limited to 365 days. Without a key the adapter spaces requests 6.5 s apart (observed 429
after ~6 fast requests, Retry-After 60)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import polars as pl

from monitor.fetch.base import Envelope, Record, SanityError, run_dataset
from monitor.schema.tables import CategoryMemberRow, CoinMetaRow, MarketRow, rows_to_df

SOURCE = "coingecko"


def fetch_markets(pages: int = 2, ts: datetime | None = None, force: bool = False) -> Path:
    def go(c) -> list[Record]:
        return [
            c.get(
                "/coins/markets",
                params={
                    "vs_currency": "usd",
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": p,
                    "sparkline": "false",
                },
            )
            for p in range(1, pages + 1)
        ]

    return run_dataset(SOURCE, "markets", "daily", go, ts=ts, force=force)


def parse_markets(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    as_of = fetched.date()
    rows: list[MarketRow] = []
    for rec in env.records:
        for m in rec.body:
            rows.append(
                MarketRow(
                    as_of=as_of,
                    id=m["id"],
                    symbol=m["symbol"].upper(),
                    name=m["name"],
                    rank=m.get("market_cap_rank"),
                    price_usd=m.get("current_price"),
                    market_cap_usd=m.get("market_cap"),
                    fdv_usd=m.get("fully_diluted_valuation"),
                    volume_24h_usd=m.get("total_volume"),
                    circulating_supply=m.get("circulating_supply"),
                    total_supply=m.get("total_supply"),
                    max_supply=m.get("max_supply"),
                    supply_source="reported",
                    source=SOURCE,
                    fetched_at=fetched,
                    git_sha=env.git_sha,
                )
            )
    df = rows_to_df(MarketRow, rows)
    if df.height < 200:
        raise SanityError(f"coingecko markets: only {df.height} rows")
    last = max(
        datetime.fromisoformat(m["last_updated"].replace("Z", "+00:00"))
        for rec in env.records
        for m in rec.body[:5]
        if m.get("last_updated")
    )
    if (fetched - last).total_seconds() > 6 * 3600:
        raise SanityError("coingecko markets: last_updated older than 6 h")
    if (df["price_usd"].fill_null(1) <= 0).any():
        raise SanityError("coingecko markets: non-positive price")
    return df


def fetch_coin_meta(ids: list[str], ts: datetime | None = None, force: bool = False) -> Path:
    """One `coins/{id}` call per id (categories). Callers cap `ids` to fit the rate budget."""

    def go(c) -> list[Record]:
        return [
            c.get(
                f"/coins/{i}",
                params={
                    "localization": "false",
                    "tickers": "false",
                    "market_data": "false",
                    "community_data": "false",
                    "developer_data": "false",
                    "sparkline": "false",
                },
            )
            for i in ids
        ]

    return run_dataset(SOURCE, "coin_meta", "daily", go, ts=ts, force=force, meta={"ids": ids})


def parse_coin_meta(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = [
        CoinMetaRow(
            id=b["id"],
            symbol=b["symbol"].upper(),
            name=b["name"],
            categories=[c for c in (b.get("categories") or []) if c],
            asset_platform_id=b.get("asset_platform_id"),
            source=SOURCE,
            fetched_at=fetched,
            git_sha=env.git_sha,
        )
        for rec in env.records
        for b in [rec.body]
        if isinstance(b, dict) and "id" in b
    ]
    return rows_to_df(CoinMetaRow, rows)


def fetch_market_chart(
    coin_id: str, days: int = 365, ts: datetime | None = None, force: bool = False
) -> Path:
    """Daily price / market cap / volume history, capped at 365 days on the free tier."""

    def go(c) -> list[Record]:
        return [
            c.get(
                f"/coins/{coin_id}/market_chart",
                params={"vs_currency": "usd", "days": min(days, 365), "interval": "daily"},
            )
        ]

    return run_dataset(
        SOURCE, f"market_chart_{coin_id}", "daily", go, ts=ts, force=force, meta={"id": coin_id}
    )


def parse_market_chart(env: Envelope) -> pl.DataFrame:
    b = env.records[0].body
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = [
        {
            "date": datetime.fromtimestamp(p[0] / 1000, tz=UTC).date(),
            "id": env.meta["id"],
            "price_usd": p[1],
            "market_cap_usd": m[1],
            "volume_24h_usd": v[1],
            "source": SOURCE,
            "fetched_at": fetched,
            "git_sha": env.git_sha,
        }
        for p, m, v in zip(b["prices"], b["market_caps"], b["total_volumes"], strict=False)
    ]
    return pl.DataFrame(rows)


def fetch_category_members(
    categories: list[str], ts: datetime | None = None, force: bool = False
) -> Path:
    """One `coins/markets?category=` call per category (250 coins by market cap each)."""

    def go(c) -> list[Record]:
        return [
            c.get(
                "/coins/markets",
                params={
                    "vs_currency": "usd",
                    "category": cat,
                    "order": "market_cap_desc",
                    "per_page": 250,
                    "page": 1,
                    "sparkline": "false",
                },
            )
            for cat in categories
        ]

    return run_dataset(
        SOURCE, "category_members", "daily", go, ts=ts, force=force, meta={"categories": categories}
    )


def parse_category_members(env: Envelope) -> pl.DataFrame:
    fetched = datetime.fromisoformat(env.fetched_at)
    rows = [
        CategoryMemberRow(
            as_of=fetched.date(),
            category_id=cat,
            id=m["id"],
            symbol=m["symbol"].upper(),
            rank=m.get("market_cap_rank"),
            source=SOURCE,
            fetched_at=fetched,
            git_sha=env.git_sha,
        )
        for rec, cat in zip(env.records, env.meta["categories"], strict=True)
        for m in rec.body
    ]
    df = rows_to_df(CategoryMemberRow, rows)
    if df.filter(pl.col("category_id") == "stablecoins").height < 50:
        raise SanityError("coingecko category members: stablecoins list suspiciously short")
    return df
