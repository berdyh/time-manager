"""Tests for the ``tm init`` bootstrap command."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.repositories.vocabulary import VocabularyRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()


def _applied_migrations(db_path: Path) -> list[int]:
    store = SQLiteStore(db_path)
    try:
        return store.applied_migrations()
    finally:
        store.close()


def _alias_count(db_path: Path) -> int:
    with sqlite3.connect(db_path) as conn:
        row = conn.execute("SELECT COUNT(*) FROM aliases").fetchone()
    assert row is not None
    return int(row[0])


def _assert_bootstrapped(db_path: Path) -> list[int]:
    applied = _applied_migrations(db_path)
    assert applied
    assert len(VocabularyRepository(db_path).list_active()) == 16
    assert _alias_count(db_path) == 5
    return applied


def test_init_first_run_creates_db_and_seeds(tmp_path: Path) -> None:
    db_path = tmp_path / "tm.db"

    result = runner.invoke(app, ["init", "--db-path", str(db_path)])

    assert result.exit_code == 0, result.output
    assert db_path.exists()
    applied = _assert_bootstrapped(db_path)
    assert result.output == (
        f"tm init: db={db_path}\n"
        f"  applied {len(applied)} migrations\n"
        "  seeded 16 starter activities\n"
        "  seeded 5 starter aliases\n"
    )


def test_init_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "tm.db"

    first = runner.invoke(app, ["init", "--db-path", str(db_path)])
    second = runner.invoke(app, ["init", "--db-path", str(db_path)])

    assert first.exit_code == 0, first.output
    assert second.exit_code == 0, second.output
    applied = _assert_bootstrapped(db_path)
    assert f"  applied {len(applied)} migrations\n" in first.output
    assert second.output == (
        f"tm init: db={db_path}\n"
        "  applied 0 migrations\n"
        "  seeded 0 starter activities\n"
        "  seeded 0 starter aliases\n"
    )


def test_init_with_custom_db_path(tmp_path: Path) -> None:
    db_path = tmp_path / "custom.sqlite"

    result = runner.invoke(app, ["init", "--db-path", str(db_path)])

    assert result.exit_code == 0, result.output
    assert db_path.exists()
    _assert_bootstrapped(db_path)
    assert f"tm init: db={db_path}\n" in result.output


def test_init_uses_TM_DB_env(tmp_path: Path) -> None:
    db_path = tmp_path / "env.sqlite"

    result = runner.invoke(app, ["init"], env={"TM_DB": str(db_path)})

    assert result.exit_code == 0, result.output
    assert db_path.exists()
    _assert_bootstrapped(db_path)
    assert f"tm init: db={db_path}\n" in result.output
