"""`monitor` command-line interface (typer). Every job in the Makefile and in Actions goes through here."""

from __future__ import annotations

import typer

from monitor import __version__

app = typer.Typer(help="Crypto risk-and-context monitor.", no_args_is_help=True)
site_app = typer.Typer(help="Static site.")
universe_app = typer.Typer(help="Universe and tiers.")
app.add_typer(site_app, name="site")
app.add_typer(universe_app, name="universe")


@app.callback()
def _version_cb() -> None:  # pragma: no cover - trivial
    pass


@app.command()
def version() -> None:
    """Print the package version."""
    typer.echo(__version__)


@site_app.command("render")
def site_render() -> None:
    """Render the static site into ./site."""
    from monitor.site.render import render

    out = render()
    typer.echo(f"rendered {out}")


@universe_app.command("seed-sectors")
def universe_seed_sectors() -> None:
    """Seed sector_map.assignments in config/universe.yaml from CoinGecko categories (then review by hand)."""
    from collections import Counter

    from monitor.jobs_risk import seed_sectors

    out = seed_sectors(write=True)
    typer.echo(str(Counter(out.values())))


@universe_app.command("show")
def universe_show(
    tier: int | None = typer.Option(None, help="only this tier"), excluded: bool = False
) -> None:
    """Print tiers with the metrics that placed each asset (as_of column included)."""
    import polars as pl

    from monitor import archive

    uni = archive.read("universe")
    if uni is None:
        typer.echo("universe not computed yet; run `monitor fetch daily && monitor compute daily`")
        raise typer.Exit(code=2)
    latest = uni.filter(pl.col("as_of") == uni["as_of"].max())
    if tier is not None:
        latest = latest.filter(pl.col("tier") == tier)
    if not excluded:
        latest = latest.filter(pl.col("tier").is_not_null())
    cols = [
        "as_of",
        "tier",
        "symbol",
        "id",
        "rank",
        "perp_venues",
        "oi_median_usd",
        "oi_window_days",
        "depth_status",
        "spot_venues",
        "adv_30d_usd",
        "adv_basis",
        "excluded_reason",
    ]
    with pl.Config(tbl_rows=-1, tbl_cols=-1, tbl_width_chars=200, fmt_str_lengths=40):
        typer.echo(str(latest.sort(["tier", "rank"]).select(cols)))
    counts = latest.group_by("tier").len().sort("tier")
    typer.echo(str(counts))


@app.command()
def fetch(
    job: str = typer.Argument("all", help="all|daily|hourly|weekly"), force: bool = False
) -> None:
    """Fetch raw data for a job (idempotent per bucket; --force refetches)."""
    import logging

    from monitor import jobs

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if job in ("all", "daily"):
        for k, v in jobs.fetch_daily(force=force).items():
            typer.echo(f"{k}: {v}")
    if job in ("all", "daily"):
        from monitor import jobs_hourly

        for k, v in jobs_hourly.fetch_hourly_klines(force=force).items():
            typer.echo(f"{k}: {v}")
    if job in ("all", "hourly"):
        from monitor import jobs_hourly

        for k, v in jobs_hourly.fetch_hourly(force=force).items():
            typer.echo(f"{k}: {v}")
    if job == "weekly":
        typer.echo("weekly: no fetch datasets (weekly recomputes from the archive)")


@app.command()
def compute(
    job: str = typer.Argument("all"),
    rebuild: bool = typer.Option(False, help="replay every raw file"),
) -> None:
    """Recompute processed tables from raw files (no network)."""
    from monitor import jobs

    if job in ("all", "daily"):
        for k, v in jobs.compute_daily(rebuild=rebuild or job == "all").items():
            typer.echo(f"{k}: {v} rows")
    if job in ("all", "hourly", "daily"):
        from monitor import jobs_hourly

        for k, v in jobs_hourly.compute_hourly(rebuild=rebuild or job == "all").items():
            typer.echo(f"{k}: {v} rows")
    if job in ("all", "daily"):
        from monitor import jobs_risk

        out = jobs_risk.compute_all_risk()
        typer.echo(
            f"risk: venues {len(out['venue']['venues'])}, trades {len(out['trades'])}, screens {len(out['screens'])}"
        )
    if job in ("all", "weekly"):
        from monitor import jobs_weekly

        for k, v in jobs_weekly.compute_weekly().items():
            typer.echo(f"{k}: {v}")


@app.command()
def alerts(
    dry_run: bool = typer.Option(False, help="write the table and feed; touch no issues"),
) -> None:
    """Open/close one GitHub issue per active condition and write site/alerts.xml."""
    import logging

    from monitor import alerts as al

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for k, v in al.sync(dry_run=True if dry_run else None).items():
        typer.echo(f"{k}: {v}")


@app.command()
def backfill(
    start: str = typer.Option(..., "--start", help="YYYY-MM-DD"), force: bool = False
) -> None:
    """Backfill history from each source as far back as it allows, then recompute walk-forward."""
    import logging
    from datetime import date

    from monitor.backfill import backfill as run_backfill

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    for k, v in run_backfill(date.fromisoformat(start), force=force).items():
        typer.echo(f"{k}: {v}")


if __name__ == "__main__":  # pragma: no cover
    app()
