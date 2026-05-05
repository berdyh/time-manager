"""Shared pytest fixtures.

Minimal in T-FND-02: just a tmp-path SQLite Store fixture. T-FND-05 will
extend this file additively.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.store import Store

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MIGRATIONS_DIR = REPO_ROOT / "migrations"


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    """Filesystem path for an isolated per-test SQLite database."""
    return tmp_path / "tm.db"


@pytest.fixture()
def store(db_path: Path) -> Store:
    """Open a Store on the per-test database with migrations applied."""
    s = Store(db_path, migrations_dir=DEFAULT_MIGRATIONS_DIR)
    s.apply_pending_migrations()
    return s
