"""Replay a retained transcript through the current debrief extractor."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer

from tm.agents.debrief import DuplicateSummaryError
from tm.commands._shared import (
    DbPathOption,
    cli_error,
    prepare_db,
    read_required_text_file,
    require_api_key,
    validate_case_date,
)
from tm.commands.debrief import (
    DEFAULT_DEBRIEF_COST_CAP_USD,
    build_debrief_agent,
    render_debrief_result,
)
from tm.llm.anthropic_adapter import DEFAULT_MAX_TOKENS, DEFAULT_MODEL
from tm.repositories.transcripts import TranscriptRepository
from tm.security import (
    connect_sqlite,
    enable_wal_mode,
    harden_sqlite_file_permissions,
    key_source_for_path,
    process_key_override,
)

reextract_app = typer.Typer(help="Re-extract retained transcripts.")

_EVENT_COLUMNS = (
    "event_id",
    "case_id",
    "activity",
    "timestamp",
    "lifecycle",
    "resource",
    "attributes_json",
    "extractor_version",
    "created_at",
    "advances_goal",
    "vocab_version",
    "schema_version",
    "case_date",
    "case_goal_id",
)
_COST_COLUMNS = (
    "ts",
    "model",
    "input_tokens",
    "output_tokens",
    "est_cost_usd",
    "request_kind",
)
_DEBRIEF_EVENT_FILTER = (
    "case_date=? AND (extractor_version LIKE 'debrief-%' OR activity='debrief_summary')"
)


class PrivacyFenceChangedError(RuntimeError):
    """Raised when privacy actions commit while a reextract is staged."""


def _privacy_action_count_for_conn(conn: Any) -> int:
    row = conn.execute("SELECT COUNT(*) FROM privacy_actions").fetchone()
    return int(row[0]) if row else 0


def _privacy_action_count(db_path: Path) -> int:
    conn = connect_sqlite(db_path)
    try:
        return _privacy_action_count_for_conn(conn)
    finally:
        conn.close()


def _delete_debrief_events(db_path: Path, case_date: str) -> int:
    conn = connect_sqlite(db_path, row_factory=True)
    try:
        enable_wal_mode(conn, db_path)
        row = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {_DEBRIEF_EVENT_FILTER}",
            (case_date,),
        ).fetchone()
        count = int(row[0]) if row else 0
        conn.execute(
            f"DELETE FROM events WHERE {_DEBRIEF_EVENT_FILTER}",
            (case_date,),
        )
        conn.commit()
    finally:
        harden_sqlite_file_permissions(db_path)
        conn.close()
    return count


def _copy_database(src_path: Path, dest_path: Path) -> None:
    key, _source = key_source_for_path(src_path)
    src = connect_sqlite(src_path)
    dest = connect_sqlite(dest_path, sqlcipher_key=key)
    try:
        src.backup(dest)
    finally:
        dest.close()
        src.close()


def _max_cost_id(db_path: Path) -> int:
    conn = connect_sqlite(db_path)
    try:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) FROM cost_ledger").fetchone()
    finally:
        conn.close()
    return int(row[0]) if row else 0


def _fetch_debrief_events(db_path: Path, case_date: str) -> list[dict[str, Any]]:
    column_sql = ", ".join(_EVENT_COLUMNS)
    conn = connect_sqlite(db_path, row_factory=True)
    try:
        rows = conn.execute(
            f"SELECT {column_sql} FROM events WHERE {_DEBRIEF_EVENT_FILTER} "
            "ORDER BY timestamp ASC, event_id ASC",
            (case_date,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _fetch_cost_rows_after(db_path: Path, cost_id: int) -> list[dict[str, Any]]:
    column_sql = ", ".join(_COST_COLUMNS)
    conn = connect_sqlite(db_path, row_factory=True)
    try:
        rows = conn.execute(
            f"SELECT {column_sql} FROM cost_ledger WHERE id > ? ORDER BY id ASC",
            (cost_id,),
        ).fetchall()
    finally:
        conn.close()
    return [dict(row) for row in rows]


def _append_cost_rows(db_path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    column_sql = ", ".join(_COST_COLUMNS)
    placeholder_sql = ", ".join("?" for _ in _COST_COLUMNS)
    conn = connect_sqlite(db_path)
    try:
        enable_wal_mode(conn, db_path)
        with conn:
            conn.executemany(
                f"INSERT INTO cost_ledger ({column_sql}) VALUES ({placeholder_sql})",
                ([row[column] for column in _COST_COLUMNS] for row in rows),
            )
    finally:
        harden_sqlite_file_permissions(db_path)
        conn.close()


def _replace_debrief_events(
    db_path: Path,
    *,
    case_date: str,
    events: list[dict[str, Any]],
    cost_rows: list[dict[str, Any]],
    privacy_action_count: int,
    replacement_transcript: str | None = None,
) -> int:
    event_column_sql = ", ".join(_EVENT_COLUMNS)
    event_placeholder_sql = ", ".join("?" for _ in _EVENT_COLUMNS)
    cost_column_sql = ", ".join(_COST_COLUMNS)
    cost_placeholder_sql = ", ".join("?" for _ in _COST_COLUMNS)
    conn = connect_sqlite(db_path, isolation_level=None, row_factory=True)
    try:
        enable_wal_mode(conn, db_path)
        conn.execute("BEGIN IMMEDIATE")
        if _privacy_action_count_for_conn(conn) != privacy_action_count:
            raise PrivacyFenceChangedError(
                "privacy actions changed during reextract; stale replacement aborted"
            )
        row = conn.execute(
            f"SELECT COUNT(*) FROM events WHERE {_DEBRIEF_EVENT_FILTER}",
            (case_date,),
        ).fetchone()
        deleted = int(row[0]) if row else 0
        conn.execute(
            f"DELETE FROM events WHERE {_DEBRIEF_EVENT_FILTER}",
            (case_date,),
        )
        conn.executemany(
            f"INSERT INTO events ({event_column_sql}) VALUES ({event_placeholder_sql})",
            ([event[column] for column in _EVENT_COLUMNS] for event in events),
        )
        conn.executemany(
            f"INSERT INTO cost_ledger ({cost_column_sql}) "
            f"VALUES ({cost_placeholder_sql})",
            ([row[column] for column in _COST_COLUMNS] for row in cost_rows),
        )
        if replacement_transcript is not None:
            conn.execute(
                "INSERT INTO transcripts "
                "(case_date, transcript_text, source, extractor_version) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(case_date) DO UPDATE SET "
                "transcript_text=excluded.transcript_text, "
                "source=excluded.source, "
                "extractor_version=excluded.extractor_version, "
                "recorded_at=datetime('now')",
                (
                    case_date,
                    replacement_transcript,
                    "reextract",
                    "debrief-v1",
                ),
            )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        harden_sqlite_file_permissions(db_path)
        conn.close()
    return deleted


@reextract_app.callback(invoke_without_command=True)
def reextract(
    db_path: DbPathOption = None,
    case_date: Annotated[
        str,
        typer.Option("--case-date", help="Case date to re-extract."),
    ] = "",
    transcript_file: Annotated[
        Path | None,
        typer.Option("--transcript-file", help="Replace retained transcript first."),
    ] = None,
    model: Annotated[
        str, typer.Option("--model", help="Anthropic model name.")
    ] = DEFAULT_MODEL,
    max_tokens: Annotated[
        int,
        typer.Option("--max-tokens", help="Maximum output tokens for extraction."),
    ] = DEFAULT_MAX_TOKENS,
    monthly_cap_usd: Annotated[
        float,
        typer.Option("--monthly-cap-usd", help="Monthly cost cap in USD."),
    ] = DEFAULT_DEBRIEF_COST_CAP_USD,
) -> None:
    """Replay a transcript in a staging DB, then replace prior debrief events."""
    if not case_date:
        cli_error("--case-date is required", code=2)
    resolved_case_date = validate_case_date(case_date)
    require_api_key("tm reextract")
    resolved_db = prepare_db(db_path)
    privacy_fence = _privacy_action_count(resolved_db)

    transcripts = TranscriptRepository(resolved_db)
    replacement_transcript: str | None = None
    if transcript_file is not None:
        replacement_transcript = read_required_text_file(
            transcript_file,
            read_description="transcript file",
            empty_description="transcript",
        )
    record = transcripts.get(resolved_case_date)
    if replacement_transcript is None and record is None:
        cli_error(f"no retained transcript for case_date={resolved_case_date}")
    transcript_text = (
        replacement_transcript
        if replacement_transcript is not None
        else record.transcript_text
        if record is not None
        else ""
    )

    with tempfile.TemporaryDirectory(prefix="tm-reextract-") as tmpdir:
        staging_db = Path(tmpdir) / "staging.db"
        staging_key, _source = key_source_for_path(resolved_db)
        with process_key_override(staging_db, staging_key):
            _copy_database(resolved_db, staging_db)
            cost_floor = _max_cost_id(staging_db)
            _delete_debrief_events(staging_db, resolved_case_date)
            agent = build_debrief_agent(
                db_path=staging_db,
                model=model,
                max_tokens=max_tokens,
                monthly_cap_usd=monthly_cap_usd,
            )
            try:
                result = agent.extract_and_persist(
                    transcript_text,
                    case_date=resolved_case_date,
                )
            except DuplicateSummaryError as exc:
                _append_cost_rows(
                    resolved_db, _fetch_cost_rows_after(staging_db, cost_floor)
                )
                cli_error(f"duplicate summary after cleanup: {exc}", cause=exc)
            except Exception:
                _append_cost_rows(
                    resolved_db, _fetch_cost_rows_after(staging_db, cost_floor)
                )
                raise

            replacement_events = _fetch_debrief_events(staging_db, resolved_case_date)
            cost_rows = _fetch_cost_rows_after(staging_db, cost_floor)
            try:
                deleted = _replace_debrief_events(
                    resolved_db,
                    case_date=resolved_case_date,
                    events=replacement_events,
                    cost_rows=cost_rows,
                    privacy_action_count=privacy_fence,
                    replacement_transcript=replacement_transcript,
                )
            except PrivacyFenceChangedError as exc:
                _append_cost_rows(resolved_db, cost_rows)
                cli_error(str(exc), cause=exc)
            except Exception:
                _append_cost_rows(resolved_db, cost_rows)
                raise
    typer.echo(f"reextract replaced {deleted} prior debrief events")
    render_debrief_result(result)
