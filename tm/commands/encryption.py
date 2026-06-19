"""Encryption operator commands."""

from __future__ import annotations

from pathlib import Path

import typer

from tm._paths import default_db_path
from tm.commands._shared import DbPathOption, cli_error
from tm.security import (
    ENV_SQLCIPHER_KEY,
    ENV_SQLCIPHER_KEYRING,
    connect_sqlite,
    encryption_status,
    store_key_in_keyring,
)

encryption_app = typer.Typer(help="Encryption — SQLCipher/keyring status.")


def _resolve_db_path(db_path: Path | None) -> Path:
    return (db_path or default_db_path()).expanduser().resolve(strict=False)


@encryption_app.command("status")
def status(db_path: DbPathOption = None) -> None:
    """Report SQLCipher driver support and configured key source."""
    resolved_db = _resolve_db_path(db_path)
    state = encryption_status(resolved_db)
    typer.echo(f"db: {resolved_db}")
    typer.echo(f"sqlcipher_available: {'yes' if state.sqlcipher_available else 'no'}")
    typer.echo(f"key_source: {state.key_source or 'none'}")
    typer.echo(f"keyring_available: {'yes' if state.keyring_available else 'no'}")
    if state.key_source and not state.sqlcipher_available:
        typer.echo(
            f"warning: {ENV_SQLCIPHER_KEY} is configured but this sqlite3 driver "
            "does not expose SQLCipher",
            err=True,
        )


@encryption_app.command("set-key")
def set_key(db_path: DbPathOption = None) -> None:
    """Store a SQLCipher key in keyring for the selected database path."""
    resolved_db = _resolve_db_path(db_path)
    state = encryption_status(resolved_db)
    if not state.sqlcipher_available:
        cli_error("this sqlite3 driver does not expose SQLCipher; not storing key")
    if resolved_db.exists() and resolved_db.stat().st_size > 0:
        cli_error(
            "refusing to store a new key for an existing database; "
            "create or migrate an encrypted database first"
        )
    key = typer.prompt(
        "SQLCipher key",
        hide_input=True,
        confirmation_prompt=True,
    )
    if not key:
        cli_error("key must not be empty")
    try:
        store_key_in_keyring(resolved_db, key)
    except RuntimeError as exc:
        cli_error(str(exc), cause=exc)
    conn = connect_sqlite(resolved_db, sqlcipher_key=key)
    conn.close()
    typer.echo(f"stored key in keyring for {resolved_db}")
    typer.echo(f"enable lookup with {ENV_SQLCIPHER_KEYRING}=1")
