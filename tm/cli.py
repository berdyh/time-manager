from __future__ import annotations

import typer

from tm import __version__

app = typer.Typer(help="tm — behavioral time manager CLI.")


def version_callback(value: bool) -> None:
    if value:
        typer.echo(__version__)
        raise typer.Exit()


@app.callback()
def main(
    version: bool = typer.Option(
        False,
        "--version",
        "-v",
        callback=version_callback,
        is_eager=True,
        help="Show version and exit.",
    ),
) -> None:
    pass


@app.command()
def init() -> None:
    """Initialise a new tm workspace (not yet implemented)."""
    typer.echo("tm init not yet implemented")
