"""Pydantic row models for every processed table. Each carries provenance:
`source`, `fetched_at`, `git_sha` (build prompt 0.5). Polars schemas are derived from these
so parquet columns and validation never drift apart."""

from __future__ import annotations

from datetime import date, datetime
from typing import get_args, get_origin

import polars as pl
from pydantic import BaseModel, Field


class Provenance(BaseModel):
    source: str
    fetched_at: datetime
    git_sha: str


class MarketRow(Provenance):
    """Daily aggregator snapshot (CoinGecko `coins/markets` or CoinPaprika `tickers`)."""

    as_of: date
    id: str
    symbol: str
    name: str
    rank: int | None
    price_usd: float | None
    market_cap_usd: float | None
    fdv_usd: float | None
    volume_24h_usd: float | None
    circulating_supply: float | None
    total_supply: float | None
    max_supply: float | None
    supply_source: str = "reported"  # reported | derived (mcap/price)


class CoinMetaRow(Provenance):
    id: str
    symbol: str
    name: str
    categories: list[str]
    asset_platform_id: str | None


class CategoryMemberRow(Provenance):
    """Membership of a CoinGecko category (`coins/markets?category=`), one row per coin."""

    as_of: date
    category_id: str
    id: str
    symbol: str
    rank: int | None


class PerpSnapshotRow(Provenance):
    """One perpetual contract on one venue at one timestamp."""

    ts: datetime
    venue: str
    symbol: str
    base: str
    multiplier: float = 1.0
    mark_price: float | None
    index_price: float | None
    funding_rate: float | None
    funding_interval_h: float | None
    next_funding_time: datetime | None
    oi_base: float | None
    oi_usd: float | None
    volume_24h_usd: float | None


class DailyPriceRow(Provenance):
    date: date
    venue: str
    symbol: str
    base: str
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float | None


class VenueListingRow(Provenance):
    """Which symbols each venue lists, spot and perp, from its instrument list."""

    ts: datetime
    venue: str
    market: str  # spot | perp
    symbol: str
    base: str
    quote: str
    multiplier: float = 1.0
    status: str
    delivery: datetime | None = None


class UniverseRow(Provenance):
    as_of: date
    id: str
    symbol: str
    name: str
    tier: int | None  # None = excluded
    excluded_reason: str | None
    sector: str | None
    market_cap_usd: float | None
    rank: int | None
    perp_venues: int
    perp_venue_list: list[str]
    oi_median_usd: float | None
    oi_window_days: int
    depth_2pct_usd: float | None
    depth_status: str  # measured | pending_phase3
    spot_venues: int
    spot_venue_list: list[str]
    adv_30d_usd: float | None
    adv_basis: str  # reported | wash_filtered
    adv_window_days: int
    meta_status: str = "categories"  # categories | pending (per-coin metadata not fetched yet)
    tier_rule_version: int = Field(default=1)


def polars_schema(model: type[BaseModel]) -> dict[str, pl.DataType]:
    """Map a pydantic model to a polars schema (nullable everywhere; polars has no NOT NULL)."""
    out: dict[str, pl.DataType] = {}
    for name, f in model.model_fields.items():
        t = f.annotation
        # unwrap Optional[X] / X | None
        if get_origin(t) is not list and get_args(t):
            t = next((a for a in get_args(t) if a is not type(None)), t)
        base = t
        if get_origin(base) is list:
            out[name] = pl.List(pl.Utf8)
        elif base is str:
            out[name] = pl.Utf8
        elif base is int:
            out[name] = pl.Int64
        elif base is float:
            out[name] = pl.Float64
        elif base is bool:
            out[name] = pl.Boolean
        elif base is date:
            out[name] = pl.Date
        elif base is datetime:
            out[name] = pl.Datetime("us", "UTC")
        else:  # pragma: no cover
            raise TypeError(f"unmapped {name}: {t}")
    return out


def rows_to_df(model: type[BaseModel], rows: list[BaseModel]) -> pl.DataFrame:
    schema = polars_schema(model)
    if not rows:
        return pl.DataFrame(schema=schema)
    dumped = [r.model_dump() for r in rows]
    return pl.DataFrame({name: [d[name] for d in dumped] for name in schema}, schema=schema)


# --------------------------------------------------------------------------- phase 3 tables
class OrderBookDepthRow(Provenance):
    """D_v(δ) for one venue-symbol snapshot (notes Definition 7.1) plus coverage flags."""

    ts: datetime
    venue: str
    symbol: str
    base: str
    mid: float
    spread_bps: float
    bid_depth_usd: float  # Σ p·q for bids within δ of mid
    ask_depth_usd: float
    depth_usd: float  # bid + ask
    delta: float  # δ used (0.02)
    coverage_bid_pct: float  # how far the book actually reached below mid (%)
    coverage_ask_pct: float
    truncated: bool  # book ended before δ on either side -> depth is a lower bound
    levels: int


class TradeStatsRow(Provenance):
    """Recent-trade statistics per venue-symbol for the wash filters (notes §7)."""

    ts: datetime
    venue: str
    symbol: str
    base: str
    n_trades: int
    span_s: float
    notional_usd: float
    median_size_usd: float
    benford_chi2: float
    benford_p: float
    first_digit_shares: list[str]  # nine shares as strings d1..d9 (kept human-readable)


class LiquidationRow(Provenance):
    """One force-closed order (OKX `liquidation-orders`, mode a). `side_closed` is the
    position side that was liquidated: long -> forced sell, short -> forced buy."""

    ts: datetime
    venue: str
    symbol: str
    base: str
    side_closed: str
    price: float
    size_base: float
    notional_usd: float


class OptionRow(Provenance):
    """Deribit option book summary row with an estimated delta (Black-76 from mark_iv)."""

    ts: datetime
    currency: str
    instrument: str
    expiry: datetime
    strike: float
    option_type: str
    mark_iv: float | None
    bid_iv: float | None = None
    ask_iv: float | None = None
    open_interest: float | None
    underlying_price: float | None
    mark_price: float | None
    delta_est: float | None
    t_years: float


class FuturesMarkRow(Provenance):
    """Dated futures mark vs spot/index for basis (notes §5.1)."""

    ts: datetime
    venue: str
    symbol: str
    base: str
    expiry: datetime
    mark_price: float
    index_price: float | None
    settle_ccy: str


class HourlyPriceRow(Provenance):
    ts: datetime
    venue: str
    symbol: str
    base: str
    open: float
    high: float
    low: float
    close: float
    volume_base: float
    volume_quote: float | None


class LongShortRow(Provenance):
    """Binance top-trader long/short position ratio (context; long share for λ)."""

    ts: datetime
    venue: str
    symbol: str
    base: str
    long_share: float
