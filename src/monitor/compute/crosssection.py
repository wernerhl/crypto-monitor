"""The cross-section: screens, not scores (notes Section 7 of the front-page numbering,
"The Cross-Section"; §9 in the file).

* `sector_standardise` — x̃ = (x − median_sector)/(1.4826 MAD_sector), winsorised at ±3 (eq. 9.1).
* `factor_returns` — weekly tercile long-short factors on the universe as of each date:
  MKT (cap-weighted market), SMB (small − big by market cap), MOM (4-week momentum skipping the
  last week), LIQ (illiquid − liquid by Amihud), sector factors (sector minus market).
* `rolling_betas` — 26-week regressions with exponential weights (half-life 8 weeks); the
  book's exposure vector w'B is a by-product (eq. 9.2). Not used to rank assets.
* `screens` — sector-relative 4-week momentum (skip last week), float ratio, ESP^vol(13),
  dilution, gate status; each with its trailing 52-week Spearman IC and standard error
  against the next week's return (`spearman_ic`).
"""

from __future__ import annotations

import math

import numpy as np
import polars as pl
from scipy import stats

MAD = 1.4826


def sector_standardise(
    df: pl.DataFrame, col: str, sector_col: str = "sector", cap: float = 3.0
) -> pl.DataFrame:
    """Add `<col>_z`: robust z within sector, winsorised at ±cap. Sectors with < 3 members fall
    back to the whole cross-section (flagged in `<col>_z_note`)."""
    med = pl.col(col).median().over(sector_col)
    mad = (pl.col(col) - med).abs().median().over(sector_col)
    n = pl.col(col).count().over(sector_col)
    med_all = pl.col(col).median()
    mad_all = (pl.col(col) - med_all).abs().median()
    z_sec = (pl.col(col) - med) / (MAD * mad)
    z_all = (pl.col(col) - med_all) / (MAD * mad_all)
    z = pl.when((n >= 3) & (mad > 0)).then(z_sec).otherwise(z_all)
    return df.with_columns(
        z.clip(-cap, cap).alias(f"{col}_z"),
        pl.when((n >= 3) & (mad > 0))
        .then(pl.lit(None))
        .otherwise(pl.lit("sector too small: cross-section z"))
        .alias(f"{col}_z_note"),
    )


def weekly_returns(daily: pl.DataFrame) -> pl.DataFrame:
    """Friday-to-Friday log returns per id from daily closes (date, id, close)."""
    d = daily.with_columns(pl.col("date").dt.truncate("1w").alias("week")).sort("id", "date")
    w = (
        d.group_by("id", "week")
        .agg(pl.col("close").last().alias("close"), pl.col("date").last().alias("date"))
        .sort("id", "week")
    )
    return w.with_columns(
        (pl.col("close") / pl.col("close").shift(1).over("id")).log().alias("ret")
    ).drop_nulls("ret")


def _tercile_ls(
    chars: pl.DataFrame, ret_next: pl.DataFrame, col: str, high_minus_low: bool = True
) -> pl.DataFrame:
    """Equal-weighted top-tercile minus bottom-tercile portfolio return per week."""
    j = chars.join(ret_next, on=["id", "week"]).drop_nulls([col, "ret_next"])
    j = j.with_columns(
        pl.col(col).rank(method="average").over("week").alias("rk"),
        pl.col(col).count().over("week").alias("n"),
    )
    j = j.filter(pl.col("n") >= 9).with_columns(
        pl.when(pl.col("rk") > 2 * pl.col("n") / 3)
        .then(1)
        .when(pl.col("rk") <= pl.col("n") / 3)
        .then(-1)
        .otherwise(0)
        .alias("leg")
    )
    out = (
        j.filter(pl.col("leg") != 0)
        .group_by("week")
        .agg(
            pl.col("ret_next").filter(pl.col("leg") == 1).mean().alias("hi"),
            pl.col("ret_next").filter(pl.col("leg") == -1).mean().alias("lo"),
        )
    )
    sign = 1.0 if high_minus_low else -1.0
    return out.with_columns((sign * (pl.col("hi") - pl.col("lo"))).alias("f")).select("week", "f")


def factor_returns(weekly: pl.DataFrame, chars: pl.DataFrame) -> pl.DataFrame:
    """`weekly`: id, week, ret, mcap (as of the start of the week). `chars`: id, week, mcap,
    mom_4w_skip1, amihud, sector. Returns week, MKT, SMB, MOM, LIQ and one column per sector."""
    ret_next = weekly.select("id", "week", pl.col("ret").alias("ret_next"))
    mkt = (
        weekly.drop_nulls(["ret", "mcap"])
        .group_by("week")
        .agg(((pl.col("ret") * pl.col("mcap")).sum() / pl.col("mcap").sum()).alias("MKT"))
    )
    smb = _tercile_ls(chars, ret_next, "mcap", high_minus_low=False).rename({"f": "SMB"})
    mom = _tercile_ls(chars, ret_next, "mom_4w_skip1").rename({"f": "MOM"})
    liq = _tercile_ls(chars, ret_next, "amihud").rename({"f": "LIQ"})  # illiquid minus liquid
    out = (
        mkt.join(smb, on="week", how="left")
        .join(mom, on="week", how="left")
        .join(liq, on="week", how="left")
    )
    sec = (
        chars.join(ret_next, on=["id", "week"])
        .drop_nulls(["sector", "ret_next"])
        .group_by("week", "sector")
        .agg(pl.col("ret_next").mean().alias("r"))
    )
    sec = sec.join(mkt, on="week").with_columns((pl.col("r") - pl.col("MKT")).alias("f"))
    for (s,), g in sec.group_by("sector", maintain_order=True):
        out = out.join(g.select("week", pl.col("f").alias(f"SEC_{s}")), on="week", how="left")
    return out.sort("week")


def ew_weights(n: int, half_life: float = 8.0) -> np.ndarray:
    lam = 0.5 ** (1.0 / half_life)
    w = lam ** np.arange(n)[::-1]
    return w / w.sum()


def rolling_betas(
    asset_ret: pl.DataFrame,
    factors: pl.DataFrame,
    window: int = 26,
    half_life: float = 8.0,
    min_obs: int = 16,
) -> pl.DataFrame:
    """Weighted least squares of each asset's weekly return on the factors over the trailing
    `window` weeks with exponential weights (half-life 8 weeks). Returns one row per (id, week)
    with beta_<factor> columns and the R²."""
    fcols = [c for c in factors.columns if c != "week"]
    f = factors.sort("week")
    weeks = f["week"].to_list()
    F = f.select(fcols).fill_null(0.0).to_numpy()
    rows = []
    for (i,), g in asset_ret.sort("week").group_by("id", maintain_order=True):
        r = dict(zip(g["week"].to_list(), g["ret"].to_list(), strict=True))
        for t in range(len(weeks)):
            wk = weeks[max(0, t - window + 1) : t + 1]
            idx = [k for k, w_ in enumerate(weeks) if w_ in set(wk)]
            y = np.array([r.get(weeks[k], np.nan) for k in idx])
            ok = np.isfinite(y)
            if ok.sum() < min_obs:
                continue
            X = np.column_stack([np.ones(ok.sum()), F[idx][ok]])
            w = ew_weights(ok.sum(), half_life)
            Wsqrt = np.sqrt(w)[:, None]
            beta, *_ = np.linalg.lstsq(X * Wsqrt, y[ok] * Wsqrt[:, 0], rcond=None)
            resid = y[ok] - X @ beta
            r2 = 1 - float(
                (w * resid**2).sum() / max((w * (y[ok] - (w * y[ok]).sum()) ** 2).sum(), 1e-12)
            )
            rows.append(
                {
                    "id": i,
                    "week": weeks[t],
                    "alpha": beta[0],
                    **{f"beta_{c}": beta[k + 1] for k, c in enumerate(fcols)},
                    "r2": r2,
                    "n": int(ok.sum()),
                }
            )
    return pl.DataFrame(rows) if rows else pl.DataFrame()


def spearman_ic(scores: pl.DataFrame, ret_next: pl.DataFrame, col: str, weeks: int = 52) -> dict:
    """Trailing IC: Spearman ρ between the screen value and the next week's return, per week,
    averaged over the last `weeks`; standard error = std/√n. Reported so nobody mistakes a
    screen for a forecast."""
    j = scores.join(ret_next, on=["id", "week"]).drop_nulls([col, "ret_next"])
    ics = []
    for (_wk,), g in j.sort("week").group_by("week", maintain_order=True):
        if g.height >= 8:
            rho = stats.spearmanr(g[col].to_numpy(), g["ret_next"].to_numpy()).correlation
            if np.isfinite(rho):
                ics.append(float(rho))
    ics = ics[-weeks:]
    n = len(ics)
    if n == 0:
        return {"ic": None, "ic_se": None, "n_weeks": 0}
    return {
        "ic": float(np.mean(ics)),
        "ic_se": float(np.std(ics, ddof=1) / math.sqrt(n)) if n > 1 else None,
        "n_weeks": n,
    }


def momentum_4w_skip1(daily: pl.DataFrame, as_of) -> pl.DataFrame:
    """log(P_{t−7} / P_{t−35}) per id (4 weeks, skipping the most recent week)."""
    from datetime import timedelta

    d = daily.filter(pl.col("date") <= as_of).sort("id", "date")
    p7 = (
        d.filter(pl.col("date") <= as_of - timedelta(days=7))
        .group_by("id")
        .agg(pl.col("close").last().alias("p7"))
    )
    p35 = (
        d.filter(pl.col("date") <= as_of - timedelta(days=35))
        .group_by("id")
        .agg(pl.col("close").last().alias("p35"))
    )
    return (
        p7.join(p35, on="id")
        .with_columns((pl.col("p7") / pl.col("p35")).log().alias("mom_4w_skip1"))
        .select("id", "mom_4w_skip1")
    )
