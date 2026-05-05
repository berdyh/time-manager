"""Migration runner tests."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tm.store import MigrationIntegrityError, Store

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def test_applies_pending_on_empty_db(tmp_path: Path, stub_migrations_dir: Path) -> None:
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    applied = s.apply_pending_migrations()
    assert applied == [1]
    assert s.applied_migrations() == [1]
    s.close()


def test_idempotent_second_apply(tmp_path: Path, stub_migrations_dir: Path) -> None:
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    assert s.apply_pending_migrations() == [1]
    assert s.apply_pending_migrations() == []
    assert s.applied_migrations() == [1]
    s.close()


def test_records_checksum_in_schema_migrations(
    tmp_path: Path, stub_migrations_dir: Path
) -> None:
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    s.apply_pending_migrations()
    cur = s._conn.cursor()  # type: ignore[attr-defined]  # test internal access
    cur.execute(
        "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
    )
    rows = cur.fetchall()
    cur.close()
    assert len(rows) == 1
    version, applied_at, checksum = rows[0]
    assert version == 1
    assert isinstance(applied_at, str) and applied_at.endswith("Z")
    assert SHA256_RE.match(checksum), f"checksum not sha256-shaped: {checksum!r}"
    s.close()


def test_detects_checksum_drift(tmp_path: Path, stub_migrations_dir: Path) -> None:
    """Apply stub migration, then mutate the file → drift error."""
    db = tmp_path / "tm.db"

    s = Store(db, migrations_dir=stub_migrations_dir)
    assert s.apply_pending_migrations() == [1]
    s.close()

    # Tamper: append a comment (changes checksum, keeps SQL valid).
    target = stub_migrations_dir / "0001_stub.sql"
    target.write_text(target.read_text() + "\n-- drift\n")

    s2 = Store(db, migrations_dir=stub_migrations_dir)
    with pytest.raises(MigrationIntegrityError) as ei:
        s2.apply_pending_migrations()
    err = ei.value
    assert err.version == 1
    assert err.recorded_checksum != err.file_checksum
    assert SHA256_RE.match(err.recorded_checksum)
    assert SHA256_RE.match(err.file_checksum)
    s2.close()


def test_applies_in_version_order(tmp_path: Path) -> None:
    """Two stub migrations: 0001 and 0002 must apply in numeric order."""
    fixture_dir = tmp_path / "migrations"
    fixture_dir.mkdir()
    (fixture_dir / "0002_second.sql").write_text(
        "CREATE TABLE second (id INTEGER PRIMARY KEY);\n"
    )
    (fixture_dir / "0001_first.sql").write_text(
        "CREATE TABLE first (id INTEGER PRIMARY KEY);\n"
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=fixture_dir)
    applied = s.apply_pending_migrations()
    assert applied == [1, 2]
    assert s.applied_migrations() == [1, 2]

    # Verify both tables exist.
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('first','second')"
    )
    names = sorted(r[0] for r in cur.fetchall())
    cur.close()
    assert names == ["first", "second"]
    s.close()
