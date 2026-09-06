"""Scheduled supply (notes Section 5).

* `esp` — ESP_i(h) = Σ_{τ∈(t,t+h]} Σ_c π_c U_{i,τ,c} / Float_i (eq. 5.1) and
  ESP^vol_i(h) = P_i × Σ π_c U / ADV^real_i (eq. 5.2, in days of real volume).
* `dilution` — ι = (Float_{t+365} − Float_t) / Float_t (§5) from the schedule plus emissions.
* `cliffs` — Rule 5.1 inputs per single unlock event: share of float and days of real volume.
π_c per recipient class from `config/thresholds.yaml → supply.pi_c`; a class not in the map
uses `pi_c['unknown']` and the row is flagged `pi_source = default`.
"""

from __future__ import annotations

from datetime import date, timedelta

import polars as pl


def _pi(class_col: pl.Expr, pi_c: dict[str, float]) -> pl.Expr:
    expr = pl.lit(pi_c.get("unknown", 0.7))
    for k, v in pi_c.items():
        expr = pl.when(class_col == k).then(pl.lit(float(v))).otherwise(expr)
    return expr


def esp(
    schedule: pl.DataFrame,
    as_of: date,
    horizon_days: int,
    float_supply: dict[str, float],
    price: dict[str, float],
    adv_real: dict[str, float],
    pi_c: dict[str, float],
) -> pl.DataFrame:
    """Per asset: expected sell pressure over (as_of, as_of + h] in units of float and in days of
    real volume. `schedule` has id, date, amount (tokens), recipient_class (cliff rows and
    linear tranches already expanded to daily amounts)."""
    w = schedule.filter(
        (pl.col("date") > as_of) & (pl.col("date") <= as_of + timedelta(days=horizon_days))
    )
    w = w.with_columns(
        _pi(pl.col("recipient_class"), pi_c).alias("pi"),
        (pl.col("recipient_class") == "unknown").alias("pi_default"),
    )
    g = w.group_by("id").agg(
        (pl.col("amount") * pl.col("pi")).sum().alias("expected_sold_tokens"),
        pl.col("amount").sum().alias("unlock_tokens"),
        pl.col("pi_default").any().alias("pi_default_used"),
        pl.len().cast(pl.Int64).alias("n_events"),
    )
    rows = []
    for r in g.to_dicts():
        i = r["id"]
        fl, px, adv = float_supply.get(i), price.get(i), adv_real.get(i)
        rows.append(
            {
                **r,
                "horizon_days": horizon_days,
                "esp_float": (r["expected_sold_tokens"] / fl) if fl else None,
                "esp_days_of_volume": (px * r["expected_sold_tokens"] / adv)
                if (px and adv)
                else None,
                "float_supply": fl,
                "price": px,
                "adv_real_usd": adv,
            }
        )
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "id": pl.Utf8,
                "expected_sold_tokens": pl.Float64,
                "unlock_tokens": pl.Float64,
                "pi_default_used": pl.Boolean,
                "n_events": pl.Int64,
                "horizon_days": pl.Int64,
                "esp_float": pl.Float64,
                "esp_days_of_volume": pl.Float64,
                "float_supply": pl.Float64,
                "price": pl.Float64,
                "adv_real_usd": pl.Float64,
            }
        )
    )


def dilution(
    schedule: pl.DataFrame,
    as_of: date,
    float_supply: dict[str, float],
    emissions_per_day: dict[str, float] | None = None,
) -> pl.DataFrame:
    """ι = (Float_{t+365} − Float_t) / Float_t where Float_{t+365} = Float_t + Σ unlocks in the next
    365 days + 365 × daily emissions (when known). Assets with neither a schedule nor emissions
    data get None (unknown), never zero."""
    w = (
        schedule.filter((pl.col("date") > as_of) & (pl.col("date") <= as_of + timedelta(days=365)))
        .group_by("id")
        .agg(pl.col("amount").sum().alias("unlock_365"))
    )
    em = emissions_per_day or {}
    known = set(schedule["id"].to_list()) | set(em)
    rows = []
    for i, fl in float_supply.items():
        if not fl:
            continue
        if i not in known:
            rows.append(
                {
                    "id": i,
                    "float_now": fl,
                    "unlock_365": None,
                    "emissions_365": None,
                    "dilution": None,
                }
            )
            continue
        u = float(w.filter(pl.col("id") == i)["unlock_365"].sum()) if w.height else 0.0
        e = 365.0 * em.get(i, 0.0)
        rows.append(
            {
                "id": i,
                "float_now": fl,
                "unlock_365": u,
                "emissions_365": e,
                "dilution": (u + e) / fl,
            }
        )
    schema = {
        "id": pl.Utf8,
        "float_now": pl.Float64,
        "unlock_365": pl.Float64,
        "emissions_365": pl.Float64,
        "dilution": pl.Float64,
    }
    return pl.DataFrame(rows, schema=schema) if rows else pl.DataFrame(schema=schema)


def cliffs(
    schedule: pl.DataFrame,
    as_of: date,
    horizon_days: int,
    float_supply: dict[str, float],
    price: dict[str, float],
    adv_real: dict[str, float],
) -> pl.DataFrame:
    """Single-event cliff inputs (Rule 5.1): for each cliff date within the horizon, the total
    unlock as a share of float and in days of real volume (no π_c: the rule is about the size of
    the event, the ESP is about what is sold)."""
    w = schedule.filter(
        (pl.col("kind") == "cliff")
        & (pl.col("date") > as_of)
        & (pl.col("date") <= as_of + timedelta(days=horizon_days))
    )
    g = w.group_by("id", "date").agg(
        pl.col("amount").sum().alias("unlock_tokens"),
        pl.col("recipient_class").unique().sort().alias("classes"),
    )
    rows = []
    for r in g.to_dicts():
        i = r["id"]
        fl, px, adv = float_supply.get(i), price.get(i), adv_real.get(i)
        rows.append(
            {
                **r,
                "share_of_float": (r["unlock_tokens"] / fl) if fl else None,
                "days_of_volume": (px * r["unlock_tokens"] / adv) if (px and adv) else None,
                "usd": (px * r["unlock_tokens"]) if px else None,
            }
        )
    return (
        pl.DataFrame(rows)
        if rows
        else pl.DataFrame(
            schema={
                "id": pl.Utf8,
                "date": pl.Date,
                "unlock_tokens": pl.Float64,
                "classes": pl.List(pl.Utf8),
                "share_of_float": pl.Float64,
                "days_of_volume": pl.Float64,
                "usd": pl.Float64,
            }
        )
    )


def expand_linear(events: pl.DataFrame, horizon_days: int = 400) -> pl.DataFrame:
    """DefiLlama linear tranches are reported as (start date, total amount). Without the end
    date in the index payload, a tranche is spread evenly over `horizon_days` (documented
    approximation; the per-protocol detail endpoint gives exact daily amounts and replaces
    this when fetched). Cliff rows pass through."""
    cliff = events.filter(pl.col("kind") == "cliff")
    lin = events.filter(pl.col("kind") == "linear_start")
    if not lin.height:
        return cliff
    lin = lin.with_columns((pl.col("amount") / horizon_days).alias("amount"))
    days = pl.DataFrame({"k": list(range(horizon_days))})
    exp = (
        lin.join(days, how="cross")
        .with_columns(
            (pl.col("date") + pl.duration(days=pl.col("k"))).alias("date"),
            pl.lit("linear").alias("kind"),
        )
        .drop("k")
    )
    return pl.concat([cliff, exp.select(cliff.columns)], how="vertical_relaxed")
