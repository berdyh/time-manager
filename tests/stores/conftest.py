"""Local fixtures for tests/stores — stub migration directories."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest


@pytest.fixture()
def stub_migrations_dir(tmp_path: Path) -> Path:
    """Return a tmp migrations dir containing exactly one valid stub migration.

    The stub creates a single harmless table so the SQL splitter is satisfied.
    Tests that need a controlled, isolated migration set should use this fixture
    instead of the live ``migrations/`` directory, ensuring assertions about
    applied version lists remain stable as real migrations are added.
    """
    mdir = tmp_path / "stub_migrations"
    mdir.mkdir()
    (mdir / "0001_stub.sql").write_text(
        "CREATE TABLE stub_fixture (id INTEGER PRIMARY KEY);\n"
    )
    return mdir


@pytest.fixture()
def write_migration(tmp_path: Path) -> Callable[..., Path]:
    """Factory fixture for migrations with arbitrary header + body.

    Returns a callable ``write_migration(filename, body)`` that writes the
    file inside a fresh ``tmp_path / "pragma_migrations"`` directory and
    returns the directory path. Subsequent calls in the same test reuse the
    directory, so multi-migration suites can be assembled.
    """
    mdir = tmp_path / "pragma_migrations"
    mdir.mkdir(exist_ok=True)

    def _write(filename: str, body: str) -> Path:
        (mdir / filename).write_text(body)
        return mdir

    return _write
