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


@universe_app.command("show")
def universe_show() -> None:
    """Print tiers with the metrics that placed each asset (phase 2)."""
    typer.echo("universe not computed yet (phase 2)")
    raise typer.Exit(code=2)


@app.command()
def fetch(job: str = typer.Argument("all", help="all|hourly|daily|weekly")) -> None:
    """Fetch raw data for a job (phase 2+)."""
    typer.echo(f"fetch {job}: no adapters yet (phase 2)")


@app.command()
def compute(job: str = typer.Argument("all")) -> None:
    """Recompute processed tables from raw files (phase 2+)."""
    typer.echo(f"compute {job}: nothing to compute yet (phase 2)")


@app.command()
def backfill(start: str = typer.Option(..., "--start", help="YYYY-MM-DD")) -> None:
    """Backfill history from each source as far back as it allows (phase 6)."""
    typer.echo(f"backfill from {start}: not implemented (phase 6)")
    raise typer.Exit(code=2)


if __name__ == "__main__":  # pragma: no cover
    app()
