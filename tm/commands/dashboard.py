"""Small operational dashboard for local tm data."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

import typer

from tm.commands._shared import DbPathOption, prepare_db
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.security import connect_sqlite

dashboard_app = typer.Typer(help="Dashboard — local metrics summary.")


def _scalar(db_path: Path, sql: str, params: tuple[Any, ...] = ()) -> int:
    conn = connect_sqlite(db_path, row_factory=True)
    try:
        row = conn.execute(sql, params).fetchone()
    finally:
        conn.close()
    return int(row[0]) if row is not None else 0


@dashboard_app.callback(invoke_without_command=True)
def dashboard(
    db_path: DbPathOption = None,
    since: Annotated[
        str | None,
        typer.Option("--since", help="YYYY-MM-DD lower bound for case dates."),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option("--until", help="YYYY-MM-DD upper bound for case dates."),
    ] = None,
) -> None:
    """Print compact counts, outcomes, suggestions, and top activities."""
    resolved_db = prepare_db(db_path)

    event_where = []
    params: list[Any] = []
    if since:
        event_where.append("case_date >= ?")
        params.append(since)
    if until:
        event_where.append("case_date <= ?")
        params.append(until)
    suffix = " WHERE " + " AND ".join(event_where) if event_where else ""

    event_count = _scalar(
        resolved_db, f"SELECT COUNT(*) FROM events{suffix}", tuple(params)
    )
    case_count = _scalar(
        resolved_db,
        f"SELECT COUNT(DISTINCT case_date) FROM events{suffix}"
        + (" AND case_date <> ''" if suffix else " WHERE case_date <> ''"),
        tuple(params),
    )
    active_goals = _scalar(
        resolved_db,
        "SELECT COUNT(*) FROM goals WHERE status = 'active'",
    )
    suggestions = _scalar(
        resolved_db,
        f"SELECT COUNT(*) FROM suggestion_telemetry{suffix}",
        tuple(params),
    )
    transcripts = _scalar(
        resolved_db,
        f"SELECT COUNT(*) FROM transcripts{suffix}",
        tuple(params),
    )

    events_repo = EventsRepository(resolved_db)
    outcomes = OutcomeAggregator(events_repo).for_date_range(
        since=since or "0001-01-01",
        until=until,
    )
    avg_outcome = (
        sum(v.outcome_score for v in outcomes.values()) / len(outcomes)
        if outcomes
        else None
    )

    conn = connect_sqlite(resolved_db, row_factory=True)
    try:
        top_rows = conn.execute(
            f"SELECT activity, COUNT(*) AS cnt FROM events{suffix} "
            "GROUP BY activity ORDER BY cnt DESC, activity ASC LIMIT 5",
            tuple(params),
        ).fetchall()
    finally:
        conn.close()

    typer.echo("DASHBOARD")
    typer.echo(f"events: {event_count}")
    typer.echo(f"case_dates: {case_count}")
    typer.echo(f"active_goals: {active_goals}")
    typer.echo(f"suggestions: {suggestions}")
    typer.echo(f"transcripts: {transcripts}")
    if avg_outcome is None:
        typer.echo("avg_outcome: n/a")
    else:
        typer.echo(f"avg_outcome: {avg_outcome:.2f}")
    typer.echo("top_activities:")
    if not top_rows:
        typer.echo("  none")
    for row in top_rows:
        typer.echo(f"  {row['activity']}: {row['cnt']}")
