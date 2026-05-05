"""tm bottlenecks CLI: performance DFG analysis."""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path
from tm.engines.process_mining import ProcessMiner
from tm.repositories.events import EventsRepository
from tm.stores.sqlite_store import SQLiteStore

bottlenecks_app = typer.Typer(help="Process mining — performance bottleneck analysis.")

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


def _humanize_seconds(s: float | None) -> str:
    """Convert a duration in seconds to a human-readable string.

    Examples::

        None  -> "n/a"
        30.0  -> "30s"
        90.0  -> "1m30s"
        3600  -> "1h0m"
        8100  -> "2h15m"
    """
    if s is None:
        return "n/a"
    total = int(round(s))
    if total < 0:
        total = 0
    hours, remainder = divmod(total, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}h{minutes}m"
    if minutes > 0:
        return f"{minutes}m{seconds}s"
    return f"{seconds}s"


# ---------------------------------------------------------------------------
# bottlenecks (callback so `tm bottlenecks` is the top-level command)
# ---------------------------------------------------------------------------


@bottlenecks_app.callback(invoke_without_command=True)
def bottlenecks(
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
    top_edges: Annotated[
        int,
        typer.Option("--top-edges", help="Maximum number of DFG edges to display."),
    ] = 10,
) -> None:
    """Print per-activity sojourn durations and top DFG edges."""
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

    # Use analyze_variants to get total_cases cheaply (no PM4Py heavy ops).
    variant_analysis = miner.analyze_variants(
        lens=lens,  # type: ignore[arg-type]
        since=since,
        until=until,
    )

    if variant_analysis.total_cases == 0:
        typer.echo(
            f"no events for lens={lens}"
            + (f", since={since}" if since else "")
            + (f", until={until}" if until else "")
        )
        raise typer.Exit(0)

    analysis = miner.analyze_performance(
        lens=lens,  # type: ignore[arg-type]
        since=since,
        until=until,
    )

    typer.echo(f"PERFORMANCE ({lens}, {variant_analysis.total_cases} cases)")
    typer.echo("  Per-activity (sorted by avg duration desc):")

    # Activities are already sorted by avg desc inside ProcessMiner.
    name_w = max((len(m.activity) for m in analysis.activities), default=4)
    for metric in analysis.activities:
        avg_str = _humanize_seconds(metric.avg_duration_seconds)
        med_str = _humanize_seconds(metric.median_duration_seconds)
        typer.echo(
            f"    {metric.activity:<{name_w}}"
            f"  avg={avg_str:<10}"
            f"  median={med_str:<10}"
            f"  count={metric.occurrence_count}"
        )

    if analysis.edges:
        typer.echo(f"  Edges (top {top_edges} by avg throughput):")
        src_w = max((len(e["source"]) for e in analysis.edges), default=4)
        dst_w = max((len(e["target"]) for e in analysis.edges), default=4)
        for edge in analysis.edges[:top_edges]:
            avg_str = _humanize_seconds(edge.get("avg_duration_seconds"))
            occ = edge.get("occurrence_count", 0)
            src = edge["source"]
            dst = edge["target"]
            typer.echo(
                f"    {src:<{src_w}} -> {dst:<{dst_w}}  avg={avg_str:<10}  occ={occ}"
            )
    else:
        typer.echo("  Edges: none (insufficient event data for DFG)")
