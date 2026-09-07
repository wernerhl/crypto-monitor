"""Phase 5: venue panel, book risk, trade structures, screens (daily), factor model and IC
(weekly). Reads the example book from config/book.yaml."""

from __future__ import annotations

import contextlib
import json
import logging
from datetime import date, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import yaml

from monitor import archive
from monitor.compute import crosssection as xs
from monitor.compute import sectors as sec
from monitor.compute import trades as tr
from monitor.compute import venue as ven
from monitor.meta import dump_json, git_sha, utc_now
from monitor.paths import CONFIG, SITE_DATA
from monitor.stress import covariance as cov
from monitor.stress import es as es_mod
from monitor.stress import scenarios as sc

log = logging.getLogger("monitor.risk")


def _cfg(name: str) -> dict:
    return yaml.safe_load((CONFIG / name).read_text())


def _latest(table: str, key: str) -> pl.DataFrame | None:
    df = archive.read(table)
    if df is None or not df.height:
        return None
    return df.filter(pl.col(key) == df[key].max())


def _universe() -> pl.DataFrame:
    u = archive.read("universe")
    return u.filter(pl.col("as_of") == u["as_of"].max())


def _daily_close_by_base() -> pl.DataFrame:
    from monitor.jobs_hourly import _daily_prices_by_base

    return _daily_prices_by_base()


def _book(uni: pl.DataFrame) -> tuple[dict, list[dict]]:
    book = _cfg("book.yaml")
    sym = dict(zip(uni["id"], uni["symbol"], strict=True))
    positions = [
        {**p, "asset_symbol": sym.get(p["asset"], p["asset"].upper())} for p in book["positions"]
    ]
    return book, positions


# --------------------------------------------------------------------------- venue panel
def compute_venue_panel(now: datetime, sha: str) -> dict:
    vcfg = _cfg("venues.yaml")
    wash = archive.read("wash_filters")
    wash_by_venue = {}
    if wash is not None and wash.height:
        w = (
            wash.filter(pl.col("date") == wash["date"].max())
            .group_by("venue")
            .agg((1 - pl.col("pass").cast(pl.Float64).mean()).alias("fail_share"))
        )
        wash_by_venue = dict(zip(w["venue"], w["fail_share"], strict=True))
    venues = ven.venue_table(vcfg, wash_by_venue)
    uni = _universe()
    book, positions = _book(uni)
    exposure = ven.exposure_table(book, venues)
    low = ven.low_score_aggregate(exposure, vcfg, book["nav_usd"])
    stables = _latest("stablecoins", "as_of")
    st_exp = ven.stablecoin_exposure(book, stables)
    disc = (
        {r["symbol"]: r["discount_to_par"] for r in stables.to_dicts()}
        if stables is not None
        else {}
    )
    systemic = [
        {
            **s,
            "discount_to_par": disc.get(s["name"]) if s.get("kind") == "stablecoin" else None,
            "direct_exposure_usd": sum(
                p["weight"] for p in positions if p["asset_symbol"] == s["name"]
            )
            * book["nav_usd"],
            "collateral_usd": sum(
                float(c["usd"])
                for c in book.get("collateral", [])
                if c.get("stablecoin") == s["name"]
            ),
        }
        for s in vcfg["systemic_exposures"]
    ]
    archive.upsert(
        "venue_scores",
        venues.with_columns(
            pl.lit(now.date()).alias("as_of"),
            pl.lit("config/venues.yaml").alias("source"),
            pl.lit(now).alias("fetched_at"),
            pl.lit(sha).alias("git_sha"),
        ),
    )
    return {
        "venues": venues.to_dicts(),
        "exposure": exposure.to_dicts(),
        "low_score": low,
        "stablecoins": st_exp.to_dicts(),
        "systemic": systemic,
        "reviewed_on": str(vcfg["reviewed_on"]),
        "nav_usd": book["nav_usd"],
    }


# --------------------------------------------------------------------------- book risk
def compute_book_risk(now: datetime, sha: str) -> dict:
    th = _cfg("thresholds.yaml")["sizing"]
    uni = _universe()
    book, positions = _book(uni)
    nav = book["nav_usd"]
    prices = _daily_close_by_base()
    bases = sorted({p["asset_symbol"] for p in positions})
    w = np.array([sum(p["weight"] for p in positions if p["asset_symbol"] == b) for b in bases])
    px = (
        prices.filter(pl.col("base").is_in(bases))
        .pivot(index="date", on="base", values="close")
        .sort("date")
        .tail(th["covariance_window_days"] + 1)
    )
    note = []
    sigma = None
    n_obs = 0
    if px.height > 30 and all(b in px.columns for b in bases):
        rets = np.diff(np.log(px.select(bases).to_numpy()), axis=0)
        ok = np.isfinite(rets).all(axis=1)
        rets = rets[ok]
        n_obs = int(rets.shape[0])
        vs = archive.read("vol_state")
        p_high_path = None
        if vs is not None and vs.height:
            # the path is not stored; use the latest filtered probability as a flat weight
            p_high = (
                float(vs.sort("date")["p_high"][-1])
                if vs.sort("date")["p_high"][-1] is not None
                else None
            )
            p_high_path = np.full(n_obs, p_high) if p_high is not None else None
        if p_high_path is None:
            p_high_path = np.full(n_obs, 0.5)
            note.append("vol-state probabilities unavailable: equal state weights")
        s_high = cov.weighted_ledoit_wolf(rets, p_high_path)
        s_low = cov.weighted_ledoit_wolf(rets, 1 - p_high_path)
        frag = _latest("fragility", "ts")
        phi = float(frag["phi"][0]) if frag is not None and frag["phi"][0] is not None else None
        sigma, cnote = cov.stress_covariance(
            s_high, s_low, float(p_high_path[-1]), phi, th["eta_fragility_tilt"]
        )
        if cnote != "ok":
            note.append(cnote)
    else:
        note.append("insufficient price history for the covariance")
    out = {
        "nav_usd": nav,
        "gross": float(sum(abs(p["weight"]) for p in positions)),
        "net": float(w.sum()),
        "assets": bases,
        "weights": w.tolist(),
        "n_obs": n_obs,
        "notes": note,
    }
    if sigma is not None:
        out["vol_ann_stress"] = cov.portfolio_vol(w, sigma)
        out["n_eff_stress"] = cov.effective_bets(w, sigma)
        lw_plain = cov.weighted_ledoit_wolf(rets, np.ones(n_obs))
        out["n_eff_current"] = cov.effective_bets(w, lw_plain) if lw_plain is not None else None
        out["vol_ann_current"] = cov.portfolio_vol(w, lw_plain) if lw_plain is not None else None
        e = es_mod.simulate_es(w, sigma, th["student_t_df"], alpha=th["es_alpha"])
        out["es_1d"] = e["es"]
        out["var_1d"] = e["var"]
        out["es_params"] = {k: e[k] for k in ("alpha", "df", "n", "horizon_days")}
        out["gross_scalar_for_vol_target"] = (
            th["vol_target_ann"] / out["vol_ann_stress"] if out["vol_ann_stress"] else None
        )
    # factor exposure vector w'B
    betas = archive.read("factor_betas")
    if betas is not None and betas.height:
        b = betas.filter(pl.col("week") == betas["week"].max())
        sym_to_id = dict(zip(uni["symbol"], uni["id"], strict=True))
        cols = [c for c in b.columns if c.startswith("beta_")]
        expo = {}
        for c in cols:
            tot = 0.0
            for base, wt in zip(bases, w, strict=True):
                r = b.filter(pl.col("id") == sym_to_id.get(base))
                if r.height:
                    tot += wt * float(r[c][0])
            expo[c.removeprefix("beta_")] = tot
        out["factor_exposure"] = expo
        out["factor_week"] = str(b["week"][0])
    else:
        out["factor_exposure"] = None
        note.append("factor betas not yet estimated (weekly job)")
    # scenarios
    pos = _latest("positioning", "ts")
    lam = {r["base"]: r["lambda_minus_2"] for r in pos.to_dicts()} if pos is not None else {}
    dep = {r["base"]: r["depth_2pct_usd"] for r in pos.to_dicts()} if pos is not None else {}
    sig = {r["base"]: r["sigma_daily"] for r in pos.to_dicts()} if pos is not None else {}
    out["cascade"] = sc.cascade_pnl(positions, lam, dep, sig, nav)
    vcfg = _cfg("venues.yaml")
    out["systemic"] = sc.systemic_pnl(
        positions, vcfg["systemic_exposures"], book.get("collateral", []), nav
    )
    exp_v: dict[str, float] = {}
    for p in positions:
        exp_v[p["venue"]] = exp_v.get(p["venue"], 0.0) + abs(p["weight"]) * nav
    for c in book.get("collateral", []):
        exp_v[c["venue"]] = exp_v.get(c["venue"], 0.0) + float(c["usd"])
    out["venue_halt"] = sc.venue_halt_pnl(exp_v, book.get("recovery_assumption_on_halt", 0.4), nav)
    archive.upsert(
        "book_risk",
        pl.DataFrame(
            [
                {
                    "as_of": now.date(),
                    "gross": out["gross"],
                    "net": out["net"],
                    "vol_ann_stress": out.get("vol_ann_stress"),
                    "n_eff_stress": out.get("n_eff_stress"),
                    "n_eff_current": out.get("n_eff_current"),
                    "es_1d": out.get("es_1d"),
                    "cascade_share_nav": out["cascade"]["total_share_nav"],
                    "n_obs": n_obs,
                    "notes": "; ".join(note),
                    "source": "prices_daily+positioning+book.yaml",
                    "fetched_at": now,
                    "git_sha": sha,
                }
            ]
        ),
    )
    return out


# --------------------------------------------------------------------------- trade structures
def compute_trades(now: datetime, sha: str) -> pl.DataFrame:
    th = _cfg("thresholds.yaml")
    vcfg = _cfg("venues.yaml")
    fees = vcfg.get("fees", {})
    vs = archive.read("venue_scores")
    scores = (
        dict(zip(vs["venue"], vs["grade"], strict=True)) if vs is not None and vs.height else {}
    )
    uni = _universe()
    prices = _daily_close_by_base()
    spot = {
        r["base"]: r["close"]
        for r in prices.sort("date").group_by("base").agg(pl.col("close").last()).to_dicts()
    }
    marks = _latest("futures_marks", "ts")
    frames = []
    if marks is not None:
        m = marks.filter(pl.col("expiry") > now + timedelta(days=7))
        frames.append(
            tr.basis_table(m, spot, now, fees, scores, th["carry"]["stablecoin_borrow_ann"])
        )
    fd = archive.read("funding_daily")
    pos = _latest("positioning", "ts")
    z = {r["base"]: r["z_fr"] for r in pos.to_dicts()} if pos is not None else {}
    perps = _latest("perp_snapshot", "ts")
    best = {}
    if perps is not None:
        bv = perps.sort("oi_usd", descending=True).group_by("base").agg(pl.col("venue").first())
        best = dict(zip(bv["base"], bv["venue"], strict=True))
    if fd is not None:
        t1 = set(uni.filter(pl.col("tier") == 1)["symbol"])
        frames.append(
            tr.funding_carry(
                fd.filter(pl.col("base").is_in(t1)),
                now.date(),
                th["carry"]["funding_mean_window_days"],
                th["carry"]["shrink_factor"],
                fees,
                scores,
                best,
                th["carry"]["stablecoin_borrow_ann"],
                z,
            )
        )
    om = _latest("options_metrics", "ts")
    frag = _latest("fragility", "ts")
    rf = _latest("rule_fires", "ts")
    r43 = (
        {r["asset"]: r["fired"] for r in rf.filter(pl.col("rule_id") == "4.3").to_dicts()}
        if rf is not None
        else {}
    )
    if om is not None:
        frames.append(
            tr.vol_premium(
                om.to_dicts(),
                float(frag["phi"][0]) if frag is not None and frag["phi"][0] is not None else None,
                r43,
                scores,
            )
        )
    cl = _latest("cliffs", "as_of")
    rf_all = archive.read("rule_fires")
    if cl is not None and rf_all is not None:
        f51 = (
            rf_all.filter(pl.col("rule_id") == "5.1")
            .sort("ts")
            .unique(subset=["asset", "inputs"], keep="last")
        )
        fires = {}
        for r in f51.to_dicts():
            with contextlib.suppress(Exception):
                fires[(r["asset"], json.loads(r["inputs"]).get("unlock_date"))] = r["fired"]
        frames.append(
            tr.unlock_short(
                cl,
                fires,
                now.date(),
                dict(zip(uni["id"], uni["tier"], strict=True)),
                dict(zip(uni["id"], uni["symbol"], strict=True)),
                z,
                fd,
                best,
                scores,
                fees,
            )
        )
    frames = [f for f in frames if f.height]
    if not frames:
        return pl.DataFrame()
    df = pl.concat(
        [f.with_columns(pl.col("expiry").cast(pl.Datetime("us", "UTC"))) for f in frames],
        how="diagonal_relaxed",
    ).with_columns(
        pl.lit(now.date()).alias("as_of"),
        pl.lit(now).alias("fetched_at"),
        pl.lit(sha).alias("git_sha"),
    )
    archive.upsert("trade_structures", df)
    return df


# --------------------------------------------------------------------------- screens (weekly + daily view)
def seed_sectors(write: bool = True) -> dict[str, str]:
    """Seed `sector_map.assignments` from CoinGecko categories; the review is manual."""
    meta = archive.read("coin_meta")
    if meta is None:
        return {}
    uni = _universe()
    cats = {r["id"]: r["categories"] for r in meta.to_dicts()}
    out = {i: sec.assign(cats.get(i, [])) for i in uni["id"].to_list()}
    if write:
        p = CONFIG / "universe.yaml"
        cfg = yaml.safe_load(p.read_text())
        cfg["sector_map"]["assignments"] = out
        cfg["sector_map"]["seeded_on"] = str(date.today())
        p.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True, width=120))
    return out


def compute_screens(now: datetime, sha: str, as_of: date | None = None) -> pl.DataFrame:
    uni = _universe()
    as_of = as_of or uni["as_of"][0]
    cfg = _cfg("universe.yaml")
    sectors = (cfg.get("sector_map") or {}).get("assignments") or {}
    prices = _daily_close_by_base()
    sym = dict(zip(uni["id"], uni["symbol"], strict=True))
    base_to_id = {v: k for k, v in sym.items()}
    pr = prices.with_columns(
        pl.col("base").replace_strict(base_to_id, default=None).alias("id")
    ).drop_nulls("id")
    mom = xs.momentum_4w_skip1(pr, as_of)
    markets = _latest("markets", "as_of")
    mk = (
        markets.filter(pl.col("source") == markets["source"][0])
        .unique(subset=["id"])
        .select("id", "market_cap_usd", "fdv_usd", "circulating_supply", "total_supply")
    )
    esp = archive.read("esp")
    e13 = (
        esp.filter((pl.col("as_of") == esp["as_of"].max()) & (pl.col("horizon_days") == 13)).select(
            "id", "esp_days_of_volume", "adv_is_reported"
        )
        if esp is not None
        else None
    )
    dil = _latest("dilution", "as_of")
    liq = _latest("liquidity", "date")
    df = (
        uni.filter(pl.col("tier").is_in([2, 3]))
        .select("id", "symbol", "tier")
        .join(mk, on="id", how="left")
        .join(mom, on="id", how="left")
    )
    df = df.with_columns(
        pl.col("id").replace_strict(sectors, default="other").alias("sector"),
        (pl.col("market_cap_usd") / pl.col("fdv_usd")).alias("float_ratio"),
    )
    if e13 is not None:
        df = df.join(e13, on="id", how="left")
    if dil is not None:
        df = df.join(dil.select("id", "dilution"), on="id", how="left")
    if liq is not None:
        df = df.join(liq.select("id", "gate_pass", "adv_real_usd"), on="id", how="left")
    else:
        df = df.with_columns(
            pl.lit(None, dtype=pl.Boolean).alias("gate_pass"),
            pl.lit(None, dtype=pl.Float64).alias("adv_real_usd"),
        )
    for col in ("mom_4w_skip1", "float_ratio"):
        df = xs.sector_standardise(df, col)
    ic = _latest("screen_ic", "as_of")
    icd = {r["screen"]: r for r in ic.to_dicts()} if ic is not None else {}
    df = df.with_columns(
        pl.lit(as_of).alias("as_of"),
        pl.lit(
            json.dumps(
                {
                    k: {"ic": v["ic"], "ic_se": v["ic_se"], "n_weeks": v["n_weeks"]}
                    for k, v in icd.items()
                }
            )
        ).alias("ic_json"),
        pl.lit("universe+markets+prices_daily+esp+dilution+liquidity").alias("source"),
        pl.lit(now).alias("fetched_at"),
        pl.lit(sha).alias("git_sha"),
    )
    archive.upsert("screens", df)
    return df


def compute_factor_model(now: datetime, sha: str) -> dict[str, int]:
    """Weekly: factor returns, rolling betas, screen ICs (walk-forward on the archive)."""
    uni = _universe()
    cfg = _cfg("universe.yaml")
    sectors = (cfg.get("sector_map") or {}).get("assignments") or {}
    prices = _daily_close_by_base()
    sym = dict(zip(uni["id"], uni["symbol"], strict=True))
    base_to_id = {v: k for k, v in sym.items()}
    pr = prices.with_columns(
        pl.col("base").replace_strict(base_to_id, default=None).alias("id")
    ).drop_nulls("id")
    wk = xs.weekly_returns(pr.select("date", "id", "close"))
    if wk.height < 100:
        return {"factor_model": 0}
    # characteristics per week: market cap (from markets history when available; else current), momentum, amihud
    markets = archive.read("markets")
    mc = (
        markets.select("as_of", "id", "market_cap_usd")
        .with_columns(pl.col("as_of").dt.truncate("1w").alias("week"))
        .group_by("id", "week")
        .agg(pl.col("market_cap_usd").last().alias("mcap"))
    )
    cur = markets.filter(pl.col("as_of") == markets["as_of"].max()).select(
        "id", pl.col("market_cap_usd").alias("mcap_now")
    )
    weeks = wk.select("week").unique()
    chars = (
        wk.select("id", "week")
        .join(mc, on=["id", "week"], how="left")
        .join(cur, on="id", how="left")
        .with_columns(pl.coalesce(pl.col("mcap"), pl.col("mcap_now")).alias("mcap"))
        .drop("mcap_now")
    )
    # momentum (4w skip 1) and amihud per week from daily data
    mom_rows, ami_rows = [], []
    from monitor.compute.liquidity import amihud

    for w_ in weeks["week"].to_list():
        m = xs.momentum_4w_skip1(pr, w_)
        mom_rows.append(m.with_columns(pl.lit(w_).alias("week")))
        a = amihud(
            pr.filter(pl.col("date") <= w_)
            .rename({"id": "base_id"})
            .with_columns(pl.col("base_id").alias("base"))
            .select("base", "date", "close", "volume_quote"),
            30,
        ).rename({"base": "id"})
        ami_rows.append(a.select("id", "amihud").with_columns(pl.lit(w_).alias("week")))
    chars = (
        chars.join(pl.concat(mom_rows), on=["id", "week"], how="left")
        .join(pl.concat(ami_rows), on=["id", "week"], how="left")
        .with_columns(pl.col("id").replace_strict(sectors, default="other").alias("sector"))
    )
    weekly = wk.join(chars.select("id", "week", "mcap"), on=["id", "week"], how="left")
    F = xs.factor_returns(weekly, chars)
    betas = xs.rolling_betas(wk.select("id", "week", "ret"), F)
    counts = {}
    if F.height:
        archive.upsert(
            "factor_returns",
            F.with_columns(
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
                pl.lit("prices_daily+markets").alias("source"),
            ),
        )
        counts["factor_returns"] = F.height
    if betas.height:
        archive.upsert(
            "factor_betas",
            betas.with_columns(
                pl.lit(now).alias("fetched_at"),
                pl.lit(sha).alias("git_sha"),
                pl.lit("factor_returns").alias("source"),
            ),
        )
        counts["factor_betas"] = betas.height
    # screen ICs: next-week return vs screen value
    ret_next = wk.select("id", "week", pl.col("ret").alias("ret_next")).with_columns(
        (pl.col("week") - pl.duration(weeks=1)).alias("week")
    )
    ic_rows = []
    for col in ("mom_4w_skip1", "amihud", "mcap"):
        r = xs.spearman_ic(chars, ret_next, col)
        ic_rows.append(
            {
                "as_of": now.date(),
                "screen": col,
                **r,
                "source": "prices_daily+markets",
                "fetched_at": now,
                "git_sha": sha,
            }
        )
    archive.upsert("screen_ic", pl.DataFrame(ic_rows))
    counts["screen_ic"] = len(ic_rows)
    return counts


# --------------------------------------------------------------------------- JSON
def compute_all_risk(now: datetime | None = None) -> dict:
    now = now or utc_now()
    sha = git_sha()
    out = {"venue": compute_venue_panel(now, sha), "book": compute_book_risk(now, sha)}
    t = compute_trades(now, sha)
    out["trades"] = t.to_dicts() if t.height else []
    s = compute_screens(now, sha)
    out["screens"] = s.to_dicts() if s.height else []
    write_risk_json(out)
    return out


def write_risk_json(out: dict, path: Path = SITE_DATA) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / "risk.json").write_text(
        dump_json({"generated_at": utc_now().isoformat(), "git_sha": git_sha(), **out})
    )
