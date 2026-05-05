"""tm discover CLI: run Inductive Miner against the events log."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path
from tm.engines.process_mining import ProcessMiner
from tm.repositories.events import EventsRepository
from tm.stores.sqlite_store import SQLiteStore

discover_app = typer.Typer(help="Process mining — Inductive Miner discovery.")

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
# discover (callback so `tm discover` is the top-level command)
# ---------------------------------------------------------------------------


@discover_app.callback(invoke_without_command=True)
def discover(
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
    case_id: Annotated[
        str | None,
        typer.Option("--case-id", help="Single case identifier override."),
    ] = None,
) -> None:
    """Run Inductive Miner discovery and print process tree + fitness/precision."""
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
    model = miner.discover_inductive_miner(
        lens=lens,  # type: ignore[arg-type]
        since=since,
        until=until,
        case_id=case_id,
    )

    if model.case_count == 0:
        typer.echo(
            f"no events for lens={lens}"
            + (f", since={since}" if since else "")
            + (f", until={until}" if until else "")
        )
        raise typer.Exit(0)

    fitness_str = f"{model.fitness:.4f}" if model.fitness is not None else "n/a"
    precision_str = f"{model.precision:.4f}" if model.precision is not None else "n/a"
    header = (
        f"DISCOVERED MODEL"
        f" ({lens}, {model.case_count} cases, {model.activity_count} activities)"
    )
    typer.echo(header)
    typer.echo(f"  process_tree: {model.process_tree_repr}")
    typer.echo(f"  petri_net:    {model.petri_net_summary}")
    typer.echo(f"  fitness:      {fitness_str}")
    typer.echo(f"  precision:    {precision_str}")
