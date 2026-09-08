"""Macro context (notes §3.3), stablecoin growth, and the event strip (notes §12 item 6)."""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl

from monitor.compute.state import net_liquidity


def macro_table(fred: pl.DataFrame, as_of: date) -> tuple[pl.DataFrame, dict]:
    """Latest value and 4-week change per series, plus net liquidity L = WALCL − TGA − RRP in USD bn
    (units reconciled: WALCL, WTREGEN millions; RRPONTSYD billions)."""
    rows = []
    latest: dict[str, float] = {}
    for sid, g in fred.sort("date").group_by("series_id", maintain_order=True):
        sid = sid[0]
        g = g.filter(pl.col("date") <= as_of)
        if not g.height:
            continue
        last = g.tail(1).to_dicts()[0]
        prior = g.filter(pl.col("date") <= last["date"] - timedelta(days=28)).tail(1)
        chg = (last["value"] - prior["value"][0]) if prior.height else None
        latest[sid] = last["value"]
        rows.append(
            {
                "series_id": sid,
                "series": last["series"],
                "unit": last["unit"],
                "date": last["date"],
                "value": last["value"],
                "change_4w": chg,
                "source": last["source"],
            }
        )
    nl = None
    if all(k in latest for k in ("WALCL", "WTREGEN", "RRPONTSYD")):
        nl = net_liquidity(latest["WALCL"], latest["WTREGEN"], latest["RRPONTSYD"])
        # 4-week change of net liquidity from the series' own 4-week changes
        ch = {r["series_id"]: r["change_4w"] for r in rows}
        nl_chg = None
        if all(ch.get(k) is not None for k in ("WALCL", "WTREGEN", "RRPONTSYD")):
            nl_chg = ch["WALCL"] / 1e3 - ch["WTREGEN"] / 1e3 - ch["RRPONTSYD"]
        rows.append(
            {
                "series_id": "NETLIQ",
                "series": "net_liquidity",
                "unit": "busd",
                "date": max(r["date"] for r in rows),
                "value": nl,
                "change_4w": nl_chg,
                "source": "computed",
            }
        )
    return pl.DataFrame(rows), {"net_liquidity_busd": nl}


def stablecoin_growth(total_hist: pl.DataFrame, as_of: date, window: int = 30) -> pl.DataFrame:
    """30-day growth of total USD stablecoin supply for every date (the z^{SC−} input)."""
    h = total_hist.sort("date").unique(subset=["date"], keep="last")
    return (
        h.with_columns(
            (pl.col("total_usd") / pl.col("total_usd").shift(window) - 1.0).alias("growth_30d")
        )
        .filter(pl.col("date") <= as_of)
        .select("date", "total_usd", "growth_30d")
    )


def event_strip(
    as_of: date,
    horizon_days: int,
    cliffs: pl.DataFrame | None,
    proposals: pl.DataFrame | None,
    manual_events: list[dict],
    expiries: pl.DataFrame | None,
    symbols: dict[str, str],
    cliff_th: dict | None = None,
    expiry_oi_share_min: float = 0.10,
) -> pl.DataFrame:
    """Next-four-weeks strip: qualifying cliffs (Rule 5.1: > 1 % of float or > 2 days of real
    volume — linear vesting is aggregated in ESP, not listed), governance, hand-maintained
    events (upgrades, listings, regulatory), and options expiries that are monthly/quarterly
    (last Friday of the month) or hold at least `expiry_oi_share_min` of the currency's OI."""
    end = as_of + timedelta(days=horizon_days)
    th = cliff_th or {
        "single_unlock_float_share_min": 0.01,
        "single_unlock_days_of_volume_min": 2.0,
    }
    rows = []
    if cliffs is not None and cliffs.height:
        for r in cliffs.to_dicts():
            big = (r.get("share_of_float") or 0) > th["single_unlock_float_share_min"] or (
                r.get("days_of_volume") or 0
            ) > th["single_unlock_days_of_volume_min"]
            if not big:
                continue
            rows.append(
                {
                    "date": r["date"],
                    "kind": "unlock cliff",
                    "asset": symbols.get(r["id"], r["id"]),
                    "title": f"{r['unlock_tokens']:,.0f} tokens ({', '.join(r['classes'])})",
                    "detail": f"{(r['share_of_float'] or 0) * 100:.2f} % of float; {r['days_of_volume']:.1f} days of real volume"
                    if r.get("days_of_volume") is not None
                    else f"{(r['share_of_float'] or 0) * 100:.2f} % of float",
                    "source": "llama_datasets",
                    "link": None,
                }
            )
    if proposals is not None and proposals.height:
        for r in proposals.to_dicts():
            d = r["end"].date()
            if as_of <= d <= end:
                rows.append(
                    {
                        "date": d,
                        "kind": "governance",
                        "asset": r["space"],
                        "title": r["title"][:100],
                        "detail": f"{r['state']}, ends {r['end']:%Y-%m-%d %H:%M} UTC",
                        "source": "snapshot",
                        "link": r.get("link"),
                    }
                )
    for e in manual_events:
        d = e["date"] if isinstance(e["date"], date) else date.fromisoformat(str(e["date"]))
        if as_of <= d <= end:
            rows.append(
                {
                    "date": d,
                    "kind": e.get("kind", "event"),
                    "asset": e.get("asset"),
                    "title": e.get("title"),
                    "detail": e.get("detail"),
                    "source": "config/events.yaml",
                    "link": e.get("source"),
                }
            )
    if expiries is not None and expiries.height:
        tot = {
            r["currency"]: r["oi"]
            for r in expiries.group_by("currency")
            .agg(pl.col("total_oi").sum().alias("oi"))
            .to_dicts()
        }
        for r in expiries.to_dicts():
            d = r["expiry"].date()
            if not (as_of <= d <= end):
                continue
            share = r["total_oi"] / tot[r["currency"]] if tot.get(r["currency"]) else 0.0
            if not (is_last_friday(d) or share >= expiry_oi_share_min):
                continue
            rows.append(
                {
                    "date": d,
                    "kind": "options expiry",
                    "asset": r["currency"],
                    "title": f"OI {r['total_oi']:,.0f} contracts ({share * 100:.0f} % of {r['currency']} OI); largest strike {r['max_oi_strike']:,.0f}",
                    "detail": "monthly/quarterly expiry" if is_last_friday(d) else "large expiry",
                    "source": "deribit",
                    "link": None,
                }
            )
    schema = {
        "date": pl.Date,
        "kind": pl.Utf8,
        "asset": pl.Utf8,
        "title": pl.Utf8,
        "detail": pl.Utf8,
        "source": pl.Utf8,
        "link": pl.Utf8,
    }
    return pl.DataFrame(rows, schema=schema).sort("date") if rows else pl.DataFrame(schema=schema)


def is_last_friday(d: date) -> bool:
    return d.weekday() == 4 and (d + timedelta(days=7)).month != d.month
