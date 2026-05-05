"""tm variants CLI: variant-frequency listing."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path
from tm.engines.process_mining import ProcessMiner
from tm.repositories.events import EventsRepository
from tm.stores.sqlite_store import SQLiteStore

variants_app = typer.Typer(help="Process mining — variant frequency listing.")

_VALID_LENSES = ("workday", "goal_pursuit")

# ---------------------------------------------------------------------------
# Shared DB-path option
# ---------------------------------------------------------------------------

_DbPathOption = Annotated[
    Path | None,
    typer.Option(
        "--db-path",
        envvar="TM_DB",
        help="Path to the tm SQLite database.",
        show_default=False,
    ),
]


def _ensure_migrations(db_path: Path) -> None:
    store = SQLiteStore(db_path)
    try:
        store.apply_pending_migrations()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# variants (callback so `tm variants` is the top-level command)
# ---------------------------------------------------------------------------


@variants_app.callback(invoke_without_command=True)
def variants(
    db_path: _DbPathOption = None,
    lens: Annotated[
        str,
        typer.Option("--lens", help="Case lens: workday | goal_pursuit"),
    ] = "workday",
    since: Annotated[
        str | None,
        typer.Option("--since", help="ISO date lower bound (inclusive)."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="ISO date upper bound (exclusive)."),
    ] = None,
    top_n: Annotated[
        int | None,
        typer.Option("--top-n", help="Maximum number of variants to display."),
    ] = None,
) -> None:
    """List distinct activity-sequence variants ordered by case frequency."""
    db_path = db_path or default_db_path()

    if lens not in _VALID_LENSES:
        typer.echo(
            f"error: --lens must be one of {_VALID_LENSES!r}",
            err=True,
        )
        raise typer.Exit(2)

    _ensure_migrations(db_path)
    repo = EventsRepository(db_path)
    miner = ProcessMiner(repo)
    analysis = miner.analyze_variants(
        lens=lens,  # type: ignore[arg-type]
        since=since,
        until=until,
        top_n=top_n,
    )

    if analysis.total_cases == 0:
        typer.echo(
            f"no events for lens={lens}"
            + (f", since={since}" if since else "")
            + (f", until={until}" if until else "")
        )
        raise typer.Exit(0)

    header = (
        f"VARIANTS ({lens},"
        f" {analysis.total_cases} cases,"
        f" {analysis.distinct_variants} distinct):"
    )
    typer.echo(header)
    for rank, variant in enumerate(analysis.variants, start=1):
        seq_str = " -> ".join(variant.sequence)
        typer.echo(f"  #{rank:<3} cases={variant.case_count}  {seq_str}")
