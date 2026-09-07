"""Universe and tiering (notes Section 2; build prompt §2).

Pure functions over polars DataFrames:
* `classify_exclusions` — stablecoin / wrapped-LST / exchange-internal / manual.
* `resolve_symbols` — map each aggregator asset to the venue symbols that actually list it.
* `oi_metrics`, `adv_metrics` — trailing-window liquidity inputs.
* `tier` — apply `config/universe.yaml` tier rules.

Tier rules (config, reasoning in the yaml):
  Tier 1: perps on ≥ 2 of {binance, bybit, okx}, 30-day median aggregate OI ≥ oi_30d_median_usd_min,
          aggregated D(0.02) ≥ depth_2pct_usd_min (depth is `pending_phase3` until the order-book
          adapter lands; while pending the depth condition is not applied and the row says so).
  Tier 2: spot on ≥ 2 of {binance, bybit, okx, coinbase, kraken}, 30-day ADV ≥ adv_real_30d_usd_min
          (ADV is reported volume, flagged `adv_basis = reported`, until wash filters land in phase 3).
  Tier 3: everything else in the candidate universe.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import polars as pl

from monitor.schema.tables import UniverseRow, rows_to_df

PERP_VENUES = ("binance", "bybit", "okx")
SPOT_VENUES = ("binance", "bybit", "okx", "coinbase", "kraken")
QUOTES = {
    "binance": {"USDT"},
    "bybit": {"USDT"},
    "okx": {"USDT"},
    "coinbase": {"USD"},
    "kraken": {"USD"},
}
LIVE_STATUS = {"TRADING", "Trading", "live", "online"}


def classify_exclusions(
    markets: pl.DataFrame, meta: pl.DataFrame | None, cfg: dict, members: pl.DataFrame | None = None
) -> pl.DataFrame:
    """Return markets with `excluded_reason` (null when kept), `is_stablecoin` and `meta_status`.

    Category membership lists (`members`) decide stablecoin / wrapped for every candidate; the
    per-coin category tags (`meta`) add the regex check when present. A candidate with neither
    is kept with `meta_status = pending` rather than excluded."""
    ex = cfg["exclusions"]
    stable_ids: set[str] = set()
    wrap_ids: set[str] = set()
    if members is not None and members.height:
        stable_ids = set(
            members.filter(pl.col("category_id").is_in(ex.get("stablecoin_category_ids") or []))[
                "id"
            ].to_list()
        )
        wrap_ids = set(
            members.filter(
                pl.col("category_id").is_in(ex.get("wrapped_or_lst_category_ids") or [])
            )["id"].to_list()
        )
    stable_re = re.compile(ex["stablecoin_category_regex"])
    wrap_re = re.compile(ex["wrapped_or_lst_category_regex"])
    manual: dict[str, str] = ex.get("manual") or {}
    internal = set(ex.get("exchange_internal_ids") or [])
    cats: dict[str, list[str]] = {}
    if meta is not None and meta.height:
        cats = {
            r["id"]: [c.strip() for c in (r["categories"] or [])]
            for r in meta.select("id", "categories").to_dicts()
        }

    def reason(i: str) -> str | None:
        if i in manual:
            return None if manual[i] == "include" else f"manual: {manual[i]}"
        if i in internal:
            return "exchange-internal token"
        if i in stable_ids:
            return "stablecoin"
        if i in wrap_ids:
            return "wrapped / liquid-staking derivative"
        c = cats.get(i)
        if c is not None:
            if any(stable_re.match(x) for x in c):
                return "stablecoin"
            if any(wrap_re.match(x) for x in c):
                return "wrapped / liquid-staking derivative"
        return None

    ids = markets["id"].to_list()
    rs = [reason(i) for i in ids]
    have_any = bool(stable_ids or wrap_ids)
    return markets.with_columns(
        pl.Series("excluded_reason", rs, dtype=pl.Utf8),
        pl.Series("is_stablecoin", [r == "stablecoin" for r in rs]),
        pl.Series(
            "meta_status",
            ["categories" if (i in cats or have_any) else "pending" for i in ids],
            dtype=pl.Utf8,
        ),
    )


def resolve_symbols(
    markets: pl.DataFrame, listings: pl.DataFrame, overrides: dict | None = None
) -> pl.DataFrame:
    """For each asset (by upper-case symbol) list venue symbols for spot and perp markets.
    Ambiguity rule: when several assets share a symbol, the higher-ranked asset wins; the
    loser gets no venue mapping and a note. Overrides (`symbol_map` in config) win over both."""
    overrides = overrides or {}
    quote_ok = pl.concat(
        [pl.DataFrame({"venue": [v] * len(q), "quote": sorted(q)}) for v, q in QUOTES.items()]
    )
    live = listings.filter(pl.col("status").is_in(LIVE_STATUS)).join(
        quote_ok, on=["venue", "quote"]
    )
    by_base: dict[tuple[str, str, str], str] = {}
    for r in live.select("venue", "market", "base", "symbol").to_dicts():
        by_base.setdefault((r["venue"], r["market"], r["base"].upper()), r["symbol"])
    seen: set[str] = set()
    out = []
    for r in markets.sort("rank", nulls_last=True).select("id", "symbol").to_dicts():
        sym = r["symbol"].upper()
        ov = overrides.get(r["id"], {})
        if sym in seen and not ov:
            out.append(
                {
                    "id": r["id"],
                    "spot": {},
                    "perp": {},
                    "note": f"symbol {sym} already claimed by a higher-ranked asset",
                }
            )
            continue
        seen.add(sym)
        spot = {v: ov.get(f"{v}_spot", by_base.get((v, "spot", sym))) for v in SPOT_VENUES}
        perp = {v: ov.get(f"{v}_perp", by_base.get((v, "perp", sym))) for v in PERP_VENUES}
        out.append(
            {
                "id": r["id"],
                "spot": {k: v for k, v in spot.items() if v},
                "perp": {k: v for k, v in perp.items() if v},
                "note": None,
            }
        )
    return pl.DataFrame(
        out,
        schema={
            "id": pl.Utf8,
            "spot": pl.Struct({v: pl.Utf8 for v in SPOT_VENUES}),
            "perp": pl.Struct({v: pl.Utf8 for v in PERP_VENUES}),
            "note": pl.Utf8,
        },
    )


def _pairs(symbol_map: pl.DataFrame, market: str) -> pl.DataFrame:
    rows = [
        (r["id"], v, s) for r in symbol_map.to_dicts() for v, s in (r[market] or {}).items() if s
    ]
    return pl.DataFrame(
        rows, schema={"id": pl.Utf8, "venue": pl.Utf8, "symbol": pl.Utf8}, orient="row"
    )


def oi_metrics(
    perps: pl.DataFrame | None, symbol_map: pl.DataFrame, as_of: date, window_days: int = 30
) -> pl.DataFrame:
    """Median over the trailing window of the daily aggregate OI (USD) across the perp venues.
    One snapshot per venue-symbol-day (the last). Returns id, oi_median_usd, oi_window_days."""
    schema = {"id": pl.Utf8, "oi_median_usd": pl.Float64, "oi_window_days": pl.Int64}
    if perps is None or not perps.height:
        return pl.DataFrame(schema=schema)
    p = perps.with_columns(pl.col("ts").dt.date().alias("day")).filter(
        pl.col("day") > as_of - timedelta(days=window_days)
    )
    p = p.sort("ts").unique(subset=["day", "venue", "symbol"], keep="last")
    daily = (
        p.join(_pairs(symbol_map, "perp"), on=["venue", "symbol"])
        .group_by("id", "day")
        .agg(pl.col("oi_usd").sum().alias("oi_usd"))
    )
    return daily.group_by("id").agg(
        pl.col("oi_usd").median().alias("oi_median_usd"),
        pl.col("day").n_unique().cast(pl.Int64).alias("oi_window_days"),
    )


def adv_metrics(
    prices: pl.DataFrame | None, symbol_map: pl.DataFrame, as_of: date, window_days: int = 30
) -> pl.DataFrame:
    """Trailing mean of daily quote volume summed across spot venues (reported volume)."""
    schema = {"id": pl.Utf8, "adv_30d_usd": pl.Float64, "adv_window_days": pl.Int64}
    if prices is None or not prices.height:
        return pl.DataFrame(schema=schema)
    p = prices.filter(pl.col("date") > as_of - timedelta(days=window_days))
    p = p.with_columns(
        pl.coalesce(pl.col("volume_quote"), pl.col("volume_base") * pl.col("close")).alias("vq")
    )
    daily = (
        p.join(_pairs(symbol_map, "spot"), on=["venue", "symbol"])
        .group_by("id", "date")
        .agg(pl.col("vq").sum().alias("vq"))
    )
    return daily.group_by("id").agg(
        pl.col("vq").mean().alias("adv_30d_usd"),
        pl.col("date").n_unique().cast(pl.Int64).alias("adv_window_days"),
    )


def tier(
    markets: pl.DataFrame,
    symbol_map: pl.DataFrame,
    oi: pl.DataFrame,
    adv: pl.DataFrame,
    cfg: dict,
    as_of: date,
    *,
    depth: pl.DataFrame | None = None,
    sector_map: dict[str, str] | None = None,
    source: str,
    fetched_at: datetime,
    git_sha: str,
) -> pl.DataFrame:
    """Apply the tier rules. `markets` must already carry `excluded_reason`."""
    r1, r2 = cfg["tier_rules"]["tier1"], cfg["tier_rules"]["tier2"]
    top_n = cfg["candidate_universe"]["top_n_by_market_cap"]
    sm = {r["id"]: r for r in symbol_map.to_dicts()}
    oi_d = {r["id"]: r for r in oi.to_dicts()}
    adv_d = {r["id"]: r for r in adv.to_dicts()}
    depth_d = {r["id"]: r for r in depth.to_dicts()} if depth is not None else {}
    sector_map = sector_map or {}
    rows: list[UniverseRow] = []
    kept = 0
    for m in markets.sort("rank", nulls_last=True).to_dicts():
        excluded = m.get("excluded_reason")
        if excluded is None:
            kept += 1
            if kept > top_n:
                excluded = f"outside top {top_n} after exclusions"
        s = sm.get(m["id"], {"spot": {}, "perp": {}})
        perp_v = sorted(k for k, v in (s.get("perp") or {}).items() if v)
        spot_v = sorted(k for k, v in (s.get("spot") or {}).items() if v)
        o, a, d = oi_d.get(m["id"], {}), adv_d.get(m["id"], {}), depth_d.get(m["id"], {})
        oi_med, oi_days = o.get("oi_median_usd"), int(o.get("oi_window_days") or 0)
        adv_usd, adv_days = a.get("adv_30d_usd"), int(a.get("adv_window_days") or 0)
        adv_basis = "wash_filtered" if a.get("is_real") else "reported"
        depth_usd = d.get("depth_2pct_usd")
        depth_status = "measured" if depth_usd is not None else "pending_phase3"
        t: int | None
        if excluded is not None:
            t = None
        elif (
            len(perp_v) >= r1["perp_venues_required"]
            and oi_med is not None
            and oi_med >= r1["oi_30d_median_usd_min"]
            and (depth_status == "pending_phase3" or depth_usd >= r1["depth_2pct_usd_min"])
        ):
            t = 1
        elif (
            len(spot_v) >= r2["spot_venues_required"]
            and adv_usd is not None
            and adv_usd >= r2["adv_real_30d_usd_min"]
        ):
            t = 2
        else:
            t = 3
        rows.append(
            UniverseRow(
                as_of=as_of,
                id=m["id"],
                symbol=m["symbol"],
                name=m["name"],
                tier=t,
                excluded_reason=excluded,
                sector=sector_map.get(m["id"]),
                market_cap_usd=m.get("market_cap_usd"),
                rank=m.get("rank"),
                perp_venues=len(perp_v),
                perp_venue_list=perp_v,
                oi_median_usd=oi_med,
                oi_window_days=oi_days,
                depth_2pct_usd=depth_usd,
                depth_status=depth_status,
                spot_venues=len(spot_v),
                spot_venue_list=spot_v,
                adv_30d_usd=adv_usd,
                adv_basis=adv_basis,
                adv_window_days=adv_days,
                tier_rule_version=cfg["version"],
                source=source,
                fetched_at=fetched_at,
                git_sha=git_sha,
            )
        )
    return rows_to_df(UniverseRow, rows)
