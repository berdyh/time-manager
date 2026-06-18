"""tm vocab — vocabulary governance sub-typer.

Each command accepts ``--db-path`` (default: ``~/.local/share/tm/tm.db``,
env-var ``TM_DB``) and instantiates a fresh repository per call.

Migrations are applied at the start of every command via
``SQLiteStore(db_path).apply_pending_migrations()``, which is idempotent and
fast when already up to date.

No LLM calls are made in v1 — the ``review`` command surfaces only
repo-resolution misses (labels from recent events that are neither a known
canonical nor an alias).  LLM alignment can be layered on in a future task.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Annotated

import typer

from tm._paths import default_db_path
from tm.repositories.events import EventsRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.security import connect_sqlite
from tm.stores.sqlite_store import SQLiteStore
from tm.vocab_alignment import VocabAligner

vocab_app = typer.Typer(
    help="Vocabulary governance — review novel labels, list canonical activities."
)

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


def _iso_days_ago(days: int) -> str:
    """Return a UTC ISO-8601 timestamp ``days`` days before now."""
    dt = datetime.now(UTC) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _load_novel_labels(
    db_path: Path,
    vocab_repo: VocabularyRepository,
    since: str,
    limit: int,
) -> list[tuple[str, int]]:
    """Return (label, count) pairs for activity values NOT in the vocabulary.

    A label is "novel" when it is neither a canonical activity nor a known
    alias.  Results are ordered by frequency (descending) and capped at
    ``limit``.
    """
    conn = connect_sqlite(str(db_path), row_factory=True)
    try:
        rows = conn.execute(
            "SELECT activity, COUNT(*) AS cnt FROM events "
            "WHERE timestamp >= ? "
            "GROUP BY activity ORDER BY cnt DESC",
            (since,),
        ).fetchall()
    finally:
        conn.close()

    novel: list[tuple[str, int]] = []
    for row in rows:
        label: str = row["activity"]
        count: int = row["cnt"]
        if vocab_repo.resolve(label) is None:
            novel.append((label, count))
        if len(novel) >= limit:
            break
    return novel


def _parse_date_option(value: str | None, option_name: str) -> date | None:
    """Parse a CLI date option that must be exactly YYYY-MM-DD."""
    if value is None:
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError:
        typer.echo(
            f"error: invalid {option_name}: {value!r} (expected YYYY-MM-DD)",
            err=True,
        )
        raise typer.Exit(1) from None
    if parsed.strftime("%Y-%m-%d") != value:
        typer.echo(
            f"error: invalid {option_name}: {value!r} (expected YYYY-MM-DD)",
            err=True,
        )
        raise typer.Exit(1)
    return parsed


def _date_start_utc(value: date) -> str:
    """Return midnight UTC for a date in the repository timestamp format."""
    return f"{value.isoformat()}T00:00:00Z"


def _last_seen_dates(
    events_repo: EventsRepository,
    activities: list[str],
) -> dict[str, str]:
    """Return YYYY-MM-DD last-seen dates for activity names, or ``never``."""
    last_seen: dict[str, str] = {}
    for activity in activities:
        events = events_repo.query_events(activity=activity)
        timestamps = [
            str(event["timestamp"]) for event in events if event.get("timestamp")
        ]
        if not timestamps:
            last_seen[activity] = "never"
            continue
        last_seen[activity] = max(timestamps)[:10]
    return last_seen


# ---------------------------------------------------------------------------
# review
# ---------------------------------------------------------------------------


@vocab_app.command()
def review(
    db_path: _DbPathOption = None,  # type: ignore[assignment]
    limit: Annotated[
        int,
        typer.Option("--limit", "-n", help="Maximum number of novel labels to review."),
    ] = 10,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help=(
                "ISO 8601 date lower bound (e.g. 2026-01-01).  Defaults to 7 days ago."
            ),
        ),
    ] = None,
    no_llm: Annotated[  # noqa: ARG001 — kept for future use / AC9
        bool,
        typer.Option("--no-llm", help="(No-op in v1) Skip LLM alignment."),
    ] = False,
) -> None:
    """Interactive review of novel activity labels found in recent events."""
    if db_path is None:
        db_path = default_db_path()

    since_ts: str
    if since is not None:
        try:
            since_ts = datetime.fromisoformat(since).strftime("%Y-%m-%dT%H:%M:%SZ")
        except ValueError:
            typer.echo(f"error: invalid ISO 8601 date: {since!r}", err=True)
            raise typer.Exit(1) from None
    else:
        since_ts = _iso_days_ago(7)

    _ensure_migrations(db_path)
    vocab_repo = VocabularyRepository(db_path)

    novel_labels = _load_novel_labels(db_path, vocab_repo, since_ts, limit)

    if not novel_labels:
        typer.echo("no novel labels in window")
        return

    aliases_added = 0
    canonicals_created = 0
    skipped = 0
    remaining = len(novel_labels)

    for label, count in novel_labels:
        typer.echo(f"\n{label} (n={count})")
        typer.echo(
            "  [m]erge into existing canonical"
            "  [c]reate new canonical"
            "  [i]gnore  [s]kip rest"
        )

        while True:
            choice = typer.prompt("choice", default="i").strip().lower()

            if choice == "s":
                # skip rest — count current label as skipped, compute remaining
                typer.echo("skipping remaining labels")
                skipped += 1
                remaining = len(novel_labels) - (
                    aliases_added + canonicals_created + skipped
                )
                typer.echo(
                    f"\nsummary: {aliases_added} aliases added, "
                    f"{canonicals_created} canonicals created, "
                    f"{skipped} skipped, "
                    f"{remaining} remaining unprocessed"
                )
                return

            elif choice == "m":
                # Merge: prompt for canonical name, validate, add alias
                active_names = {e.activity_name for e in vocab_repo.list_active()}
                merge_cancelled = False
                while True:
                    canonical_name = (
                        typer.prompt(
                            f"  merge '{label}' into canonical (one of: "
                            f"{', '.join(sorted(active_names)[:5])}...)",
                            default="",
                            show_default=False,
                        )
                        .strip()
                        .lower()
                    )
                    if canonical_name == "":
                        typer.echo("  merge cancelled")
                        merge_cancelled = True
                        break
                    if canonical_name in active_names:
                        break
                    typer.echo(
                        f"  '{canonical_name}' is not a known canonical. "
                        "Please enter a valid canonical name."
                    )
                if merge_cancelled:
                    continue
                vocab_repo.add_alias(label, canonical_name)
                typer.echo(f"added alias `{label}` → `{canonical_name}`")
                aliases_added += 1
                break

            elif choice == "c":
                # Create new canonical
                suggested = label.strip().lower().replace(" ", "_").replace("-", "_")
                canonical_name = (
                    typer.prompt("  new canonical name", default=suggested)
                    .strip()
                    .lower()
                )
                try:
                    vocab_repo.add_canonical(canonical_name)
                    # Also add the original label as an alias to the new canonical
                    # (if it differs from the canonical name)
                    if label.lower() != canonical_name:
                        try:
                            vocab_repo.add_alias(label, canonical_name)
                        except ValueError:
                            pass  # alias already exists; not a problem
                    typer.echo(f"created canonical `{canonical_name}`")
                    canonicals_created += 1
                except ValueError as exc:
                    typer.echo(f"  error: {exc}")
                    skipped += 1
                break

            elif choice == "i":
                typer.echo("skipped")
                skipped += 1
                break

            else:
                typer.echo("  please enter m, c, i, or s")

    processed = aliases_added + canonicals_created + skipped
    remaining = len(novel_labels) - processed
    typer.echo(
        f"\nsummary: {aliases_added} aliases added, "
        f"{canonicals_created} canonicals created, "
        f"{skipped} skipped, "
        f"{remaining} remaining unprocessed"
    )


# ---------------------------------------------------------------------------
# drift
# ---------------------------------------------------------------------------


@vocab_app.command()
def drift(
    db_path: _DbPathOption = None,  # type: ignore[assignment]
    idle_days: Annotated[
        int,
        typer.Option(
            "--idle-days",
            min=0,
            help="Number of idle days before an active canonical is drifted.",
        ),
    ] = 14,
    since: Annotated[
        str | None,
        typer.Option(
            "--since",
            help="YYYY-MM-DD lower bound for novelty-rate events.",
        ),
    ] = None,
    until: Annotated[
        str | None,
        typer.Option(
            "--until",
            help="YYYY-MM-DD inclusive upper bound for novelty-rate events.",
        ),
    ] = None,
) -> None:
    """Report inactive canonical vocabulary entries and novel-label rate."""
    if db_path is None:
        db_path = default_db_path()

    since_date = _parse_date_option(since, "--since")
    until_date = _parse_date_option(until, "--until")
    since_ts = _date_start_utc(since_date) if since_date is not None else None
    until_ts = (
        _date_start_utc(until_date + timedelta(days=1))
        if until_date is not None
        else None
    )

    _ensure_migrations(db_path)
    vocab_repo = VocabularyRepository(db_path)
    events_repo = EventsRepository(db_path)
    aligner = VocabAligner(vocab_repo)

    drifted = sorted(aligner.find_drifted_activities(idle_days=idle_days))
    novelty_rate = aligner.compute_novelty_rate(since=since_ts, until=until_ts)
    total_events = len(events_repo.query_events(since=since_ts, until=until_ts))
    novel_events = round(novelty_rate * total_events)

    if not drifted and novelty_rate == 0.0:
        typer.echo("Vocabulary drift report: clean (no drift, novelty rate 0.000)")
        return

    typer.echo("Vocabulary drift report")
    typer.echo("=======================")
    typer.echo(f"Idle window: {idle_days} days")
    typer.echo("Drifted canonicals (no events in window):")

    if drifted:
        last_seen = _last_seen_dates(events_repo, drifted)
        name_width = max(len(name) for name in drifted)
        for activity in drifted:
            typer.echo(
                f"  - {activity:<{name_width}} (last seen: {last_seen[activity]})"
            )
    else:
        typer.echo("  (none)")

    since_label = since if since is not None else "all"
    until_label = until if until is not None else "now"
    typer.echo(
        f"Novelty rate over [{since_label}..{until_label}]: "
        f"{novelty_rate:.3f}  "
        f"({novel_events} / {total_events} events used novel labels)"
    )


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


@vocab_app.command(name="list")
def list_vocab(
    include_archived: Annotated[
        bool,
        typer.Option(
            "--include-archived",
            help="Include archived entries in the listing.",
        ),
    ] = False,
    db_path: _DbPathOption = None,  # type: ignore[assignment]
) -> None:
    """List canonical vocabulary entries (active only by default)."""
    if db_path is None:
        db_path = default_db_path()

    _ensure_migrations(db_path)
    vocab_repo = VocabularyRepository(db_path)

    if not vocab_repo.list_all():
        vocab_repo.seed_starter_vocabulary()
        vocab_repo.seed_starter_aliases()

    if include_archived:
        entries = vocab_repo.list_all()
    else:
        entries = vocab_repo.list_active()

    if not entries:
        typer.echo("no vocabulary entries")
        return

    name_w = max(len(e.activity_name) for e in entries)
    status_w = max(len(e.status) for e in entries)

    header = f"{'ACTIVITY':<{name_w}}  {'STATUS':<{status_w}}  DESCRIPTION"
    separator = "-" * (name_w + status_w + 16)
    typer.echo(header)
    typer.echo(separator)

    for entry in entries:
        desc = entry.description or "-"
        typer.echo(
            f"{entry.activity_name:<{name_w}}  {entry.status:<{status_w}}  {desc}"
        )
