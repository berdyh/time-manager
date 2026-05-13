"""CLI-internal shared helpers for tm command modules."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer

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


def utc_today() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%d")


def require_api_key(command_name: str) -> None:
    if not os.environ.get(API_KEY_ENV):
        typer.echo(
            f"error: {API_KEY_ENV} is not set; set it before running {command_name}.",
            err=True,
        )
        raise typer.Exit(1)
