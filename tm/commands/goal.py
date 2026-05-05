"""tm goal — goal pursuit management sub-typer.

Each command accepts ``--db-path`` (default: ``~/.local/share/tm/tm.db``,
env-var ``TM_DB``) and instantiates a fresh ``GoalsRepository`` per call.

Migrations are applied at the start of every command via
``SQLiteStore(db_path).apply_pending_migrations()``, which is idempotent and
fast when already up to date.

Note: ``tm goal complete`` / ``tm goal abandon`` / ``tm goal show`` require
the *full* 26-character ULID.  Truncated IDs (e.g. the 12-char display prefix
in ``tm goal list``) are for human readability only and are NOT accepted as
command arguments.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path
from tm.repositories.goals import GoalsRepository
from tm.stores.sqlite_store import SQLiteStore

goal_app = typer.Typer(help="Goal pursuit management.")

# ---------------------------------------------------------------------------
# Shared option type
# ---------------------------------------------------------------------------

_DbPathOption = Annotated[
    Path,
    typer.Option(
        "--db-path",
        envvar="TM_DB",
        help="Path to the tm SQLite database.",
        show_default=False,
    ),
]


def _ensure_migrations(db_path: Path) -> None:
    """Apply any pending migrations against *db_path* (idempotent)."""
    store = SQLiteStore(db_path)
    try:
        store.apply_pending_migrations()
    finally:
        store.close()


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


@goal_app.command()
def add(
    name: Annotated[str, typer.Argument(help="Short name for the goal.")],
    description: Annotated[
        str | None,
        typer.Option("--description", "-d", help="Optional longer description."),
    ] = None,
    priority: Annotated[
        int | None,
        typer.Option("--priority", "-p", help="Priority 1–3 (1 = highest)."),
    ] = None,
    target: Annotated[
        str | None,
        typer.Option(
            "--target",
            "-t",
            help="Target completion date/time in ISO 8601 format (e.g. 2026-12-31).",
        ),
    ] = None,
    db_path: _DbPathOption = None,  # type: ignore[assignment]
) -> None:
    """Add a new goal."""
    if db_path is None:
        db_path = default_db_path()

    # Validate priority before hitting the DB (mirrors Goal model constraint)
    if priority is not None and not (1 <= priority <= 3):
        typer.echo(
            f"error: priority must be an integer in [1, 3] or None, got {priority!r}",
            err=True,
        )
        raise typer.Exit(1)

    # Parse target date if provided
    target_dt: datetime | None = None
    if target is not None:
        try:
            target_dt = datetime.fromisoformat(target)
        except ValueError:
            typer.echo(f"error: invalid ISO 8601 date/time: {target!r}", err=True)
            raise typer.Exit(1) from None

    _ensure_migrations(db_path)
    repo = GoalsRepository(db_path)
    try:
        goal = repo.add(
            name=name,
            description=description,
            priority=priority,
            target_completion_at=target_dt,
        )
    except (ValueError, sqlite3.IntegrityError) as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"created goal {goal.goal_id}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@goal_app.command(name="list")
def list_goals(
    status: Annotated[
        str,
        typer.Option(
            "--status",
            "-s",
            help="Filter by status: active, completed, abandoned, all.",
        ),
    ] = "active",
    db_path: _DbPathOption = None,  # type: ignore[assignment]
) -> None:
    """List goals, filtered by status (default: active)."""
    if db_path is None:
        db_path = default_db_path()

    valid_statuses = {"active", "completed", "abandoned", "all"}
    if status not in valid_statuses:
        typer.echo(f"error: --status must be one of {sorted(valid_statuses)}", err=True)
        raise typer.Exit(1)

    _ensure_migrations(db_path)
    repo = GoalsRepository(db_path)
    goals = repo.list(status=status)  # type: ignore[arg-type]

    if not goals:
        typer.echo("no goals")
        return

    # Column widths
    id_w = 12
    status_w = 9
    priority_w = 8
    name_w = max(4, max(len(g.name) for g in goals))
    target_w = max(6, max(len(g.target_completion_at or "-") for g in goals))

    header = (
        f"{'ID':<{id_w}}  {'STATUS':<{status_w}}  {'NAME':<{name_w}}"
        f"  {'PRIORITY':<{priority_w}}  {'TARGET':<{target_w}}"
    )
    separator = "-" * len(header)
    typer.echo(header)
    typer.echo(separator)

    for g in goals:
        short_id = g.goal_id[:id_w]
        priority_str = str(g.priority) if g.priority is not None else "-"
        target_str = (
            g.target_completion_at if g.target_completion_at is not None else "-"
        )
        typer.echo(
            f"{short_id:<{id_w}}  {g.status:<{status_w}}  {g.name:<{name_w}}"
            f"  {priority_str:<{priority_w}}  {target_str:<{target_w}}"
        )


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


@goal_app.command()
def complete(
    goal_id: Annotated[str, typer.Argument(help="Full 26-character ULID of the goal.")],
    db_path: _DbPathOption = None,  # type: ignore[assignment]
) -> None:
    """Mark a goal as completed."""
    if db_path is None:
        db_path = default_db_path()

    _ensure_migrations(db_path)
    repo = GoalsRepository(db_path)
    try:
        repo.complete(goal_id)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"completed {goal_id}")


# ---------------------------------------------------------------------------
# abandon
# ---------------------------------------------------------------------------


@goal_app.command()
def abandon(
    goal_id: Annotated[str, typer.Argument(help="Full 26-character ULID of the goal.")],
    reason: Annotated[
        str | None,
        typer.Option("--reason", "-r", help="Optional reason for abandonment."),
    ] = None,
    db_path: _DbPathOption = None,  # type: ignore[assignment]
) -> None:
    """Mark a goal as abandoned."""
    if db_path is None:
        db_path = default_db_path()

    _ensure_migrations(db_path)
    repo = GoalsRepository(db_path)
    try:
        repo.abandon(goal_id, reason=reason)
    except ValueError as exc:
        typer.echo(f"error: {exc}", err=True)
        raise typer.Exit(1) from exc

    typer.echo(f"abandoned {goal_id}")


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


@goal_app.command()
def show(
    goal_id: Annotated[str, typer.Argument(help="Full 26-character ULID of the goal.")],
    db_path: _DbPathOption = None,  # type: ignore[assignment]
) -> None:
    """Show details for a single goal."""
    if db_path is None:
        db_path = default_db_path()

    _ensure_migrations(db_path)
    repo = GoalsRepository(db_path)
    goal = repo.get(goal_id)

    if goal is None:
        typer.echo(f"error: unknown goal {goal_id}", err=True)
        raise typer.Exit(1)

    def _v(val: object) -> str:
        return str(val) if val is not None else "-"

    lines = [
        f"ID:            {goal.goal_id}",
        f"NAME:          {goal.name}",
        f"STATUS:        {goal.status}",
        f"DESCRIPTION:   {_v(goal.description)}",
        f"PRIORITY:      {_v(goal.priority)}",
        f"CREATED_AT:    {goal.created_at}",
        f"TARGET:        {_v(goal.target_completion_at)}",
        f"COMPLETED_AT:  {_v(goal.completed_at)}",
        f"ABANDONED_AT:  {_v(goal.abandoned_at)}",
        f"ABANDON_REASON:{_v(goal.abandon_reason)}",
    ]
    typer.echo("\n".join(lines))
