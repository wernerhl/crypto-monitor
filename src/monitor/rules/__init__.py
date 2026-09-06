"""Trigger rules (notes §4.5 Rules 4.1–4.3, §5 Rule 5.1, §7 Rule 7.1) as pure functions.

Each returns a `RuleFire` with the inputs it saw and the thresholds it used, so the dashboard
can show both. Thresholds come ONLY from `config/thresholds.yaml` (passed in as `th`); the
functions never read any other source. `fired` is None when an input is unavailable.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel


class RuleFire(BaseModel):
    rule_id: str
    asset: str
    ts: datetime
    fired: bool | None
    inputs: dict[str, float | str | None]
    thresholds: dict[str, float]
    note: str | None = None


def _missing(inputs: dict) -> list[str]:
    return [k for k, v in inputs.items() if v is None]


def crowded_long(
    asset: str,
    ts: datetime,
    z_fr: float | None,
    oi_rel_pctile: float | None,
    lambda_minus_2: float | None,
    depth_2pct: float | None,
    th: dict,
) -> RuleFire:
    """Rule 4.1: z^FR > 2, OI^rel above its 90th percentile, Λ^−(2)/D(0.02) > 1."""
    t = th["crowded_long"]
    ratio = (lambda_minus_2 / depth_2pct) if (lambda_minus_2 is not None and depth_2pct) else None
    inputs = {
        "z_fr": z_fr,
        "oi_rel_pctile": oi_rel_pctile,
        "lambda_minus_2": lambda_minus_2,
        "depth_2pct": depth_2pct,
        "lambda_over_depth": ratio,
    }
    thr = {
        "z_fr_min": t["z_fr_min"],
        "oi_rel_percentile_min": t["oi_rel_percentile_min"],
        "lambda_over_depth_min": t["lambda_over_depth_min"],
    }
    miss = _missing(inputs)
    if miss:
        return RuleFire(
            rule_id="4.1",
            asset=asset,
            ts=ts,
            fired=None,
            inputs=inputs,
            thresholds=thr,
            note=f"unavailable: {', '.join(miss)}",
        )
    fired = (
        z_fr > t["z_fr_min"]
        and oi_rel_pctile >= t["oi_rel_percentile_min"]
        and ratio > t["lambda_over_depth_min"]
    )
    return RuleFire(
        rule_id="4.1", asset=asset, ts=ts, fired=bool(fired), inputs=inputs, thresholds=thr
    )


def capitulation(
    asset: str,
    ts: datetime,
    z_fr: float | None,
    oi_change_5d: float | None,
    long_liq_24h_pctile: float | None,
    th: dict,
) -> RuleFire:
    """Rule 4.2: z^FR < −2, OI down more than 20 % over 5 days, 24-h long-liquidation volume
    above its 95th percentile."""
    t = th["capitulation"]
    inputs = {
        "z_fr": z_fr,
        "oi_change_5d": oi_change_5d,
        "long_liq_24h_pctile": long_liq_24h_pctile,
    }
    thr = {
        "z_fr_max": t["z_fr_max"],
        "oi_drop_5d_min": t["oi_drop_5d_min"],
        "long_liq_24h_percentile_min": t["long_liq_24h_percentile_min"],
    }
    miss = _missing(inputs)
    if miss:
        return RuleFire(
            rule_id="4.2",
            asset=asset,
            ts=ts,
            fired=None,
            inputs=inputs,
            thresholds=thr,
            note=f"unavailable: {', '.join(miss)}",
        )
    fired = (
        z_fr < t["z_fr_max"]
        and oi_change_5d <= -t["oi_drop_5d_min"]
        and long_liq_24h_pctile >= t["long_liq_24h_percentile_min"]
    )
    return RuleFire(
        rule_id="4.2", asset=asset, ts=ts, fired=bool(fired), inputs=inputs, thresholds=thr
    )


def vol_underpricing(
    asset: str, ts: datetime, vrp: float | None, phi: float | None, th: dict
) -> RuleFire:
    """Rule 4.3: VRP < 0 and Φ > 1 → no short-vol positions."""
    t = th["vol_underpricing"]
    inputs = {"vrp": vrp, "phi": phi}
    thr = {"vrp_max": t["vrp_max"], "phi_min": t["phi_min"]}
    miss = _missing(inputs)
    if miss:
        return RuleFire(
            rule_id="4.3",
            asset=asset,
            ts=ts,
            fired=None,
            inputs=inputs,
            thresholds=thr,
            note=f"unavailable: {', '.join(miss)}",
        )
    return RuleFire(
        rule_id="4.3",
        asset=asset,
        ts=ts,
        fired=bool(vrp < t["vrp_max"] and phi > t["phi_min"]),
        inputs=inputs,
        thresholds=thr,
    )


def cliff(
    asset: str,
    ts: datetime,
    unlock_share_of_float: float | None,
    unlock_days_of_volume: float | None,
    th: dict,
    unlock_date: str | None = None,
) -> RuleFire:
    """Rule 5.1: a single unlock exceeding 1 % of float or two days of real volume."""
    t = th["cliff"]
    inputs = {
        "unlock_share_of_float": unlock_share_of_float,
        "unlock_days_of_volume": unlock_days_of_volume,
        "unlock_date": unlock_date,
    }
    thr = {
        "single_unlock_float_share_min": t["single_unlock_float_share_min"],
        "single_unlock_days_of_volume_min": t["single_unlock_days_of_volume_min"],
    }
    if unlock_share_of_float is None and unlock_days_of_volume is None:
        return RuleFire(
            rule_id="5.1",
            asset=asset,
            ts=ts,
            fired=None,
            inputs=inputs,
            thresholds=thr,
            note="unavailable: no unlock schedule",
        )
    a = (
        unlock_share_of_float is not None
        and unlock_share_of_float > t["single_unlock_float_share_min"]
    )
    b = (
        unlock_days_of_volume is not None
        and unlock_days_of_volume > t["single_unlock_days_of_volume_min"]
    )
    return RuleFire(
        rule_id="5.1", asset=asset, ts=ts, fired=bool(a or b), inputs=inputs, thresholds=thr
    )


def gate_rule(
    asset: str,
    ts: datetime,
    dtl_days: float | None,
    round_trip_cost: float | None,
    expected_net_return: float | None,
    th: dict,
) -> RuleFire:
    """Rule 7.1: DTL(ρ = 0.1) ≤ 3 days and round-trip impact ≤ ¼ expected net return. `fired`
    here means the gate is CLOSED (the asset fails)."""
    t = th["gate"]
    inputs = {
        "dtl_days": dtl_days,
        "round_trip_cost": round_trip_cost,
        "expected_net_return": expected_net_return,
    }
    thr = {
        "participation_rate": t["participation_rate"],
        "dtl_max_days": t["dtl_max_days"],
        "impact_cost_share_of_net_return_max": t["impact_cost_share_of_net_return_max"],
    }
    if dtl_days is None:
        return RuleFire(
            rule_id="7.1",
            asset=asset,
            ts=ts,
            fired=None,
            inputs=inputs,
            thresholds=thr,
            note="unavailable: no real ADV",
        )
    closed = dtl_days > t["dtl_max_days"]
    if round_trip_cost is not None and expected_net_return is not None and expected_net_return > 0:
        closed = (
            closed
            or round_trip_cost > t["impact_cost_share_of_net_return_max"] * expected_net_return
        )
    return RuleFire(
        rule_id="7.1", asset=asset, ts=ts, fired=bool(closed), inputs=inputs, thresholds=thr
    )
