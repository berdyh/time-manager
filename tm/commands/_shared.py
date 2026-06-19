"""CLI-internal shared helpers for tm command modules."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from tm._paths import default_db_path
from tm.llm.anthropic_adapter import ANTHROPIC_API_KEY_ENV as API_KEY_ENV
from tm.stores.sqlite_store import SQLiteStore

DbPathOption = Annotated[
    Path | None,
    typer.Option(
        "--db-path",
        envvar="TM_DB",
        help="Path to the tm SQLite database.",
        show_default=False,
    ),
]


def ensure_migrations(db_path: Path) -> None:
    store = SQLiteStore(db_path)
    try:
        store.apply_pending_migrations()
    finally:
        store.close()


def prepare_db(db_path: Path | None) -> Path:
    resolved_db = db_path or default_db_path()
    ensure_migrations(resolved_db)
    return resolved_db


def cli_error(
    message: str,
    *,
    code: int = 1,
    cause: BaseException | None = None,
) -> NoReturn:
    typer.echo(f"error: {message}", err=True)
    if cause is None:
        raise typer.Exit(code)
    raise typer.Exit(code) from cause


def read_text_file(path: Path, description: str) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError as exc:
        cli_error(f"could not read {description}: {exc}", cause=exc)


def read_required_text_file(
    path: Path,
    *,
    read_description: str,
    empty_description: str,
) -> str:
    text = read_text_file(path, read_description)
    if not text.strip():
        cli_error(f"{empty_description} is empty")
    return text


def utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def validate_case_date(value: str, option_name: str = "--case-date") -> str:
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        typer.echo(
            f"error: invalid {option_name}: {value!r} (expected YYYY-MM-DD)",
            err=True,
        )
        raise typer.Exit(2) from None
    if parsed.strftime("%Y-%m-%d") != value:
        typer.echo(
            f"error: invalid {option_name}: {value!r} (expected YYYY-MM-DD)",
            err=True,
        )
        raise typer.Exit(2)
    return value


def require_api_key(command_name: str) -> None:
    if not os.environ.get(API_KEY_ENV):
        cli_error(f"{API_KEY_ENV} is not set; set it before running {command_name}.")
