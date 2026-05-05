"""Local fixtures for tests/stores — stub migration directories."""

from __future__ import annotations

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
