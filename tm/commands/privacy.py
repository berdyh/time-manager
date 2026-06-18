"""Privacy commands: redact and forget."""

from __future__ import annotations

import json
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any

import typer

from tm._paths import default_db_path, default_kuzu_path, kuzu_projection_marker_path
from tm.commands._shared import DbPathOption, ensure_migrations, validate_case_date
from tm.models.goals import ulid
from tm.security import connect_sqlite

privacy_app = typer.Typer(help="Privacy — redact text or forget events.")

KuzuPathOption = Annotated[
    Path | None,
    typer.Option(
        "--kuzu-db-path",
        envvar="TM_KUZU",
        help="Path to the local Kuzu projection to clear after privacy changes.",
        show_default=False,
    ),
]


def _selector(
    case_date: str | None, event_id: str | None
) -> tuple[str, str, tuple[str, ...]]:
    if bool(case_date) == bool(event_id):
        typer.echo("error: provide exactly one of --case-date or --event-id", err=True)
        raise typer.Exit(2)
    if case_date:
        valid_case_date = validate_case_date(case_date)
        next_case_date = (
            datetime.strptime(valid_case_date, "%Y-%m-%d").date() + timedelta(days=1)
        ).isoformat()
        return (
            "case_date",
            "case_date = ? OR (case_date = '' AND timestamp >= ? AND timestamp < ?)",
            (valid_case_date, valid_case_date, next_case_date),
        )
    return "event_id", "event_id = ?", (event_id or "",)


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _redact_attributes(raw: str, replacement: str) -> str:
    try:
        attrs = json.loads(raw or "{}")
    except json.JSONDecodeError:
        attrs = {}
    if not isinstance(attrs, dict):
        attrs = {}
    redacted = {key: _redact_value(value, replacement) for key, value in attrs.items()}
    redacted["redacted"] = True
    redacted["redacted_at"] = _now_iso()
    return json.dumps(redacted, sort_keys=True)


def _redact_value(value: Any, replacement: str) -> Any:
    if isinstance(value, str):
        return replacement
    if isinstance(value, list):
        return [_redact_value(item, replacement) for item in value]
    if isinstance(value, dict):
        return {key: _redact_value(item, replacement) for key, item in value.items()}
    return value


def _case_date_from_timestamp(timestamp: Any) -> str | None:
    raw = str(timestamp or "").strip()
    for separator in ("T", " "):
        if separator in raw:
            raw = raw.split(separator, 1)[0]
            break
    try:
        datetime.strptime(raw, "%Y-%m-%d")
    except ValueError:
        return None
    return raw


def _case_date_for_event(row: Any) -> str | None:
    if row is None:
        return None
    if row["case_date"]:
        return str(row["case_date"])
    return _case_date_from_timestamp(row["timestamp"])


def _log_action(
    conn: Any,
    *,
    action_type: str,
    selector: str,
    affected_events: int,
    affected_transcripts: int,
) -> None:
    conn.execute(
        "INSERT INTO privacy_actions "
        "(action_id, action_type, selector, affected_events, affected_transcripts) "
        "VALUES (?, ?, ?, ?, ?)",
        (ulid(), action_type, selector, affected_events, affected_transcripts),
    )


def _projection_path(kuzu_db_path: Path | None) -> Path:
    return (kuzu_db_path or default_kuzu_path()).expanduser()


def _is_default_projection(path: Path) -> bool:
    default_projection = default_kuzu_path().expanduser()
    return path.absolute() == default_projection.absolute()


def _has_projection_marker(path: Path) -> bool:
    marker = kuzu_projection_marker_path(path)
    return marker.exists() and marker.is_file() and not marker.is_symlink()


def _clear_kuzu_projection(kuzu_db_path: Path | None) -> bool:
    projection = _projection_path(kuzu_db_path)
    if not projection.exists():
        return False
    if projection.is_symlink():
        typer.echo(
            f"warning: not clearing symlinked Kuzu projection path: {projection}",
            err=True,
        )
        return False
    marker = kuzu_projection_marker_path(projection)
    if not _is_default_projection(projection):
        if not _has_projection_marker(projection):
            typer.echo(
                f"warning: not clearing unmarked Kuzu projection path: {projection}",
                err=True,
            )
            return False
    if projection.is_dir():
        shutil.rmtree(projection)
    else:
        projection.unlink()
        marker.unlink(missing_ok=True)
    return True


def _ensure_kuzu_projection_clearable(kuzu_db_path: Path | None) -> None:
    projection = _projection_path(kuzu_db_path)
    if not projection.exists() and not projection.is_symlink():
        return
    if projection.is_symlink():
        typer.echo(
            f"error: refusing to clear symlinked Kuzu projection path: {projection}",
            err=True,
        )
        raise typer.Exit(1)
    if not _is_default_projection(projection) and not _has_projection_marker(
        projection
    ):
        typer.echo(
            f"error: refusing to clear unmarked Kuzu projection path: {projection}",
            err=True,
        )
        raise typer.Exit(1)


def _harden_sqlite_deletes(conn: Any) -> None:
    conn.execute("PRAGMA secure_delete=ON")


def _purge_sqlite_deleted_content(conn: Any) -> None:
    row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if row is not None and int(row[0]) != 0:
        raise RuntimeError("WAL checkpoint busy; deleted content may remain in WAL")
    conn.execute("VACUUM")


def _best_effort_purge_sqlite_deleted_content(conn: Any) -> None:
    try:
        _purge_sqlite_deleted_content(conn)
    except Exception as exc:  # pragma: no cover - exercised via monkeypatch
        typer.echo(f"warning: SQLite purge skipped: {exc}", err=True)


@privacy_app.command("redact")
def redact(
    db_path: DbPathOption = None,
    kuzu_db_path: KuzuPathOption = None,
    case_date: Annotated[
        str | None,
        typer.Option("--case-date", help="Redact all events for this case date."),
    ] = None,
    event_id: Annotated[
        str | None,
        typer.Option("--event-id", help="Redact one event by id."),
    ] = None,
    replacement: Annotated[
        str,
        typer.Option("--replacement", help="Replacement text for string attributes."),
    ] = "[redacted]",
) -> None:
    """Redact string attributes and resources while preserving event shape."""
    resolved_db = db_path or default_db_path()
    ensure_migrations(resolved_db)
    _ensure_kuzu_projection_clearable(kuzu_db_path)
    selector_name, where_sql, params = _selector(case_date, event_id)
    conn = connect_sqlite(resolved_db, isolation_level=None, row_factory=True)
    kuzu_cleared = False
    try:
        _harden_sqlite_deletes(conn)
        conn.execute("BEGIN IMMEDIATE")
        rows = conn.execute(
            "SELECT event_id, activity, attributes_json, case_date, timestamp "
            f"FROM events WHERE {where_sql}",
            params,
        ).fetchall()
        transcript_case_dates: set[str] = set()
        for row in rows:
            transcript_case_date = _case_date_for_event(row)
            if transcript_case_date:
                transcript_case_dates.add(transcript_case_date)
            conn.execute(
                "UPDATE events SET activity=?, resource=NULL, attributes_json=? "
                "WHERE event_id=?",
                (
                    (
                        "debrief_summary"
                        if row["activity"] == "debrief_summary"
                        else replacement
                    ),
                    _redact_attributes(row["attributes_json"], replacement),
                    row["event_id"],
                ),
            )
        transcript_count = 0
        target_case_dates = [case_date] if case_date else sorted(transcript_case_dates)
        for transcript_case_date in target_case_dates:
            cur = conn.execute(
                "UPDATE transcripts SET transcript_text=? WHERE case_date=?",
                (replacement, transcript_case_date),
            )
            transcript_count += int(cur.rowcount)
            conn.execute(
                "UPDATE suggestion_telemetry "
                "SET recommended_action=?, llm_explanation_text=? "
                "WHERE case_date=?",
                (replacement, replacement, transcript_case_date),
            )
        _log_action(
            conn,
            action_type="redact",
            selector=f"{selector_name}={case_date or event_id}",
            affected_events=len(rows),
            affected_transcripts=transcript_count,
        )
        kuzu_cleared = _clear_kuzu_projection(kuzu_db_path)
        conn.commit()
        _best_effort_purge_sqlite_deleted_content(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    show_transcript_count = bool(case_date) or bool(transcript_case_dates)
    typer.echo(
        f"redacted {len(rows)} events"
        + (f" and {transcript_count} transcripts" if show_transcript_count else "")
    )
    if kuzu_cleared:
        typer.echo("cleared Kuzu projection")


@privacy_app.command("forget")
def forget(
    db_path: DbPathOption = None,
    kuzu_db_path: KuzuPathOption = None,
    case_date: Annotated[
        str | None,
        typer.Option("--case-date", help="Delete all data for this case date."),
    ] = None,
    event_id: Annotated[
        str | None,
        typer.Option("--event-id", help="Delete one event by id."),
    ] = None,
) -> None:
    """Delete selected events and associated transcript/suggestion data."""
    resolved_db = db_path or default_db_path()
    ensure_migrations(resolved_db)
    _ensure_kuzu_projection_clearable(kuzu_db_path)
    selector_name, where_sql, params = _selector(case_date, event_id)
    conn = connect_sqlite(resolved_db, isolation_level=None, row_factory=True)
    kuzu_cleared = False
    try:
        _harden_sqlite_deletes(conn)
        conn.execute("BEGIN IMMEDIATE")
        transcript_case_date: str | None = None
        if event_id:
            event_row = conn.execute(
                "SELECT case_date, timestamp FROM events WHERE event_id=?", (event_id,)
            ).fetchone()
            transcript_case_date = _case_date_for_event(event_row)
        row = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {where_sql}", params
        ).fetchone()
        affected_events = int(row[0]) if row else 0
        conn.execute(f"DELETE FROM events WHERE {where_sql}", params)
        transcript_count = 0
        if case_date:
            cur = conn.execute(
                "DELETE FROM transcripts WHERE case_date=?", (case_date,)
            )
            transcript_count = int(cur.rowcount)
            conn.execute(
                "DELETE FROM suggestion_telemetry WHERE case_date=?", (case_date,)
            )
        elif transcript_case_date:
            cur = conn.execute(
                "DELETE FROM transcripts WHERE case_date=?", (transcript_case_date,)
            )
            transcript_count = int(cur.rowcount)
            conn.execute(
                "DELETE FROM suggestion_telemetry WHERE case_date=?",
                (transcript_case_date,),
            )
        _log_action(
            conn,
            action_type="forget",
            selector=f"{selector_name}={case_date or event_id}",
            affected_events=affected_events,
            affected_transcripts=transcript_count,
        )
        kuzu_cleared = _clear_kuzu_projection(kuzu_db_path)
        conn.commit()
        _best_effort_purge_sqlite_deleted_content(conn)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    show_transcript_count = bool(case_date) or bool(transcript_case_date)
    typer.echo(
        f"forgot {affected_events} events"
        + (f" and {transcript_count} transcripts" if show_transcript_count else "")
    )
    if kuzu_cleared:
        typer.echo("cleared Kuzu projection")
