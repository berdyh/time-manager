"""Export and backup commands."""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile
from pathlib import Path
from typing import Annotated, Any

import typer

from tm.commands._shared import DbPathOption, cli_error, prepare_db
from tm.security import connect_sqlite, key_source_for_path

export_app = typer.Typer(help="Export tm data as JSON.")
backup_app = typer.Typer(help="Backup the SQLite database.")

_TABLES = (
    "events",
    "goals",
    "vocabulary",
    "aliases",
    "suggestion_telemetry",
    "transcripts",
    "privacy_actions",
    "cost_ledger",
)
_PRIVATE_FILE_MODE = 0o600


def _nofollow_flag() -> int:
    return int(getattr(os, "O_NOFOLLOW", 0))


def _write_private_text(path: Path, text: str) -> None:
    tmp_path = _create_private_temp_file(path)
    try:
        tmp_path.write_text(text, encoding="utf-8")
        os.chmod(tmp_path, _PRIVATE_FILE_MODE)
        tmp_path.replace(path)
        os.chmod(path, _PRIVATE_FILE_MODE)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise


def _create_private_empty_file(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | _nofollow_flag()
    fd = os.open(path, flags, _PRIVATE_FILE_MODE)
    os.close(fd)
    os.chmod(path, _PRIVATE_FILE_MODE)


def _create_private_temp_file(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw_path = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
    )
    os.close(fd)
    tmp_path = Path(raw_path)
    os.chmod(tmp_path, _PRIVATE_FILE_MODE)
    return tmp_path


def _same_file_path(left: Path, right: Path) -> bool:
    left = left.expanduser()
    right = right.expanduser()
    try:
        if left.exists() and right.exists() and left.samefile(right):
            return True
    except OSError:
        pass
    return left.resolve(strict=False) == right.resolve(strict=False)


def _sqlite_reserved_paths(source: Path) -> tuple[Path, ...]:
    source = source.expanduser().resolve(strict=False)
    return (
        source,
        Path(f"{source}-wal"),
        Path(f"{source}-shm"),
        Path(f"{source}-journal"),
    )


def _reject_source_output(source: Path, output: Path, command_name: str) -> None:
    if any(
        _same_file_path(reserved, output) for reserved in _sqlite_reserved_paths(source)
    ):
        cli_error(
            f"{command_name} output must differ from database path and SQLite sidecars"
        )


def _table_rows(conn: sqlite3.Connection, table: str) -> list[dict[str, Any]]:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (table,),
    ).fetchone()
    if exists is None:
        return []
    rows = conn.execute(f"SELECT * FROM {table}").fetchall()
    return [dict(row) for row in rows]


def _export_payload(db_path: Path) -> dict[str, list[dict[str, Any]]]:
    conn = connect_sqlite(db_path, isolation_level=None, row_factory=True)
    try:
        conn.execute("BEGIN")
        payload = {table: _table_rows(conn, table) for table in _TABLES}
        conn.commit()
        return payload
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


@export_app.callback(invoke_without_command=True)
def export_data(
    db_path: DbPathOption = None,
    output: Annotated[
        Path | None,
        typer.Option("--output", "-o", help="Write JSON export to PATH."),
    ] = None,
) -> None:
    """Export core tm tables as a single JSON object."""
    resolved_db = prepare_db(db_path)
    if output is None:
        payload = _export_payload(resolved_db)
        text = json.dumps(payload, indent=2, sort_keys=True)
        typer.echo(text)
    else:
        _reject_source_output(resolved_db, output, "export")
        payload = _export_payload(resolved_db)
        text = json.dumps(payload, indent=2, sort_keys=True)
        _write_private_text(output, text + "\n")
        rows_exported = sum(len(v) for v in payload.values())
        typer.echo(f"exported {rows_exported} rows to {output}")


@backup_app.callback(invoke_without_command=True)
def backup(
    db_path: DbPathOption = None,
    output: Annotated[
        Path,
        typer.Option("--output", "-o", help="Destination SQLite backup path."),
    ] = Path("tm-backup.db"),
    overwrite: Annotated[
        bool,
        typer.Option("--overwrite", help="Replace an existing backup file."),
    ] = False,
) -> None:
    """Create a SQLite backup using the SQLite online-backup API."""
    resolved_db = prepare_db(db_path)
    _reject_source_output(resolved_db, output, "backup")
    if output.exists() and not overwrite:
        cli_error(f"backup exists: {output}")

    key, _source = key_source_for_path(resolved_db)
    tmp_output = _create_private_temp_file(output)
    src: sqlite3.Connection | None = None
    dest: sqlite3.Connection | None = None
    dest_closed = False
    try:
        src = connect_sqlite(resolved_db)
        dest = connect_sqlite(tmp_output, sqlcipher_key=key)
        src.backup(dest)
        dest.close()
        dest_closed = True
        os.chmod(tmp_output, _PRIVATE_FILE_MODE)
        tmp_output.replace(output)
        os.chmod(output, _PRIVATE_FILE_MODE)
    except Exception:
        tmp_output.unlink(missing_ok=True)
        raise
    finally:
        if dest is not None and not dest_closed:
            dest.close()
        if src is not None:
            src.close()
    typer.echo(f"backup written to {output}")
