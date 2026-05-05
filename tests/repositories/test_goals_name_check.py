"""Regression tests for migration 0008: goals.name CHECK constraint.

Verifies that after applying 0008_goals_name_check.sql:
- The goals table rejects empty and whitespace-only names at the SQL level.
- Valid names are accepted.
- Pre-existing valid data survives the rebuild.
- Pre-existing invalid data (empty name) aborts the migration.
- Both indexes idx_goals_status and idx_goals_created_at are recreated.
- Existing GoalsRepository operations are unaffected.
- schema_migrations records version 8.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tm.models.goals import ulid as make_ulid
from tm.repositories.goals import GoalsRepository
from tm.store import MigrationError, Store

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _apply_all_migrations(db_path: Path) -> Store:
    """Open a Store, apply all migrations from the real migrations dir, return it."""
    s = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    s.apply_pending_migrations()
    return s


def _apply_up_to_0007(db_path: Path) -> Store:
    """Apply only migrations 0001-0007 (exclude 0008) using a filtered tmp dir."""
    import shutil

    # Create a filtered migrations dir with only 0001-0007.
    tmp_mdir = db_path.parent / "migrations_0007"
    tmp_mdir.mkdir(exist_ok=True)
    for f in sorted(MIGRATIONS_DIR.iterdir()):
        if f.is_file() and f.name.endswith(".sql"):
            # Extract version number from filename
            try:
                version = int(f.name.split("_")[0])
            except ValueError:
                continue
            if version <= 7:
                shutil.copy(f, tmp_mdir / f.name)

    s = Store(db_path, migrations_dir=tmp_mdir)
    s.apply_pending_migrations()
    return s


# ---------------------------------------------------------------------------
# test_migration_0008_applies_cleanly_on_top_of_0007
# ---------------------------------------------------------------------------


def test_migration_0008_applies_cleanly_on_top_of_0007(tmp_path: Path) -> None:
    """Fresh tmp DB with all migrations applied; schema_migrations records v8."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations(db)
    applied = s.applied_migrations()
    s.close()
    assert 8 in applied, f"Expected version 8 in applied migrations, got: {applied}"


# ---------------------------------------------------------------------------
# test_goals_name_empty_rejected_post_migration
# ---------------------------------------------------------------------------


def test_goals_name_empty_rejected_post_migration(tmp_path: Path) -> None:
    """After migration 0008, inserting a goal with name='' raises IntegrityError."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (make_ulid(), ""),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# test_goals_name_whitespace_only_rejected_post_migration
# ---------------------------------------------------------------------------


def test_goals_name_whitespace_only_rejected_post_migration(tmp_path: Path) -> None:
    """After migration 0008, inserting a goal with name='   ' raises IntegrityError."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (make_ulid(), "   "),
            )
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# test_goals_name_valid_accepted_post_migration
# ---------------------------------------------------------------------------


def test_goals_name_valid_accepted_post_migration(tmp_path: Path) -> None:
    """After migration 0008, inserting a goal with a valid name succeeds."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        goal_id = make_ulid()
        conn.execute(
            "INSERT INTO goals (goal_id, name, status, created_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (goal_id, "valid name"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT goal_id, name FROM goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        assert row is not None
        assert row[1] == "valid name"
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# test_existing_GoalsRepository_add_still_works
# ---------------------------------------------------------------------------


def test_existing_GoalsRepository_add_still_works(tmp_path: Path) -> None:
    """GoalsRepository.add works correctly after migration 0008 is applied."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations(db)
    s.close()

    repo = GoalsRepository(db)
    goal = repo.add(name="Goal after migration 0008")
    assert goal.goal_id != ""
    assert goal.name == "Goal after migration 0008"
    assert goal.status == "active"

    # Verify it's visible via list
    goals = repo.list()
    goal_ids = {g.goal_id for g in goals}
    assert goal.goal_id in goal_ids


# ---------------------------------------------------------------------------
# test_pre_existing_data_with_valid_names_survives_rebuild
# ---------------------------------------------------------------------------


def test_pre_existing_data_with_valid_names_survives_rebuild(tmp_path: Path) -> None:
    """Valid pre-existing goals survive the 0008 table rebuild."""
    db = tmp_path / "tm.db"

    # Apply 0001-0007 only.
    s = _apply_up_to_0007(db)
    s.close()

    # Insert N goals directly via raw sqlite3 (bypasses GoalsRepository validation).
    conn = sqlite3.connect(str(db))
    inserted_ids = []
    try:
        for i in range(3):
            gid = make_ulid()
            inserted_ids.append(gid)
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (gid, f"Pre-existing goal {i + 1}"),
            )
        conn.commit()
    finally:
        conn.close()

    # Now apply all migrations (including 0008).
    s2 = Store(db, migrations_dir=MIGRATIONS_DIR)
    s2.apply_pending_migrations()
    s2.close()

    # Verify all 3 goals are still present.
    conn2 = sqlite3.connect(str(db))
    try:
        rows = conn2.execute(
            "SELECT goal_id FROM goals WHERE goal_id IN (?, ?, ?)",
            tuple(inserted_ids),
        ).fetchall()
        found_ids = {r[0] for r in rows}
    finally:
        conn2.close()

    assert found_ids == set(inserted_ids), (
        f"Some goals were lost during rebuild. Expected: {set(inserted_ids)}, "
        f"found: {found_ids}"
    )


# ---------------------------------------------------------------------------
# test_pre_existing_data_with_invalid_names_aborts_migration
# ---------------------------------------------------------------------------


def test_pre_existing_data_with_invalid_names_aborts_migration(tmp_path: Path) -> None:
    """A goal with empty name in the DB causes migration 0008 to hard-fail."""
    db = tmp_path / "tm.db"

    # Apply 0001-0007 only.
    s = _apply_up_to_0007(db)
    s.close()

    # Insert a goal with empty name directly (bypasses Python validation).
    conn = sqlite3.connect(str(db))
    try:
        conn.execute(
            "INSERT INTO goals (goal_id, name, status, created_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (make_ulid(), ""),
        )
        conn.commit()
    finally:
        conn.close()

    # Applying migration 0008 must fail. The runner wraps body IntegrityError
    # as MigrationBodyError (which extends MigrationError), so catching
    # MigrationError is sufficient to handle all migration-runner failures.
    s2 = Store(db, migrations_dir=MIGRATIONS_DIR)
    with pytest.raises(MigrationError):
        s2.apply_pending_migrations()
    s2.close()


# ---------------------------------------------------------------------------
# test_indexes_recreated_after_rebuild
# ---------------------------------------------------------------------------


def test_indexes_recreated_after_rebuild(tmp_path: Path) -> None:
    """After migration 0008, idx_goals_status and idx_goals_created_at exist."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master "
            "WHERE type='index' AND tbl_name='goals' AND name IN "
            "('idx_goals_status', 'idx_goals_created_at')"
        ).fetchall()
        index_names = {r[0] for r in rows}
    finally:
        conn.close()

    assert "idx_goals_status" in index_names, (
        "idx_goals_status not found after migration 0008"
    )
    assert "idx_goals_created_at" in index_names, (
        "idx_goals_created_at not found after migration 0008"
    )


# ---------------------------------------------------------------------------
# T-FND-09: migration 0009 tighter whitespace CHECK tests
# ---------------------------------------------------------------------------


def _apply_all_migrations_including_0009(db_path: Path) -> Store:
    """Apply 0001-0009 from the real migrations dir."""
    s = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    s.apply_pending_migrations()
    return s


def test_migration_0009_applies_cleanly(tmp_path: Path) -> None:
    """Fresh DB with all migrations applied; schema_migrations records v9."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations_including_0009(db)
    applied = s.applied_migrations()
    s.close()
    assert 9 in applied, f"Expected version 9 in applied migrations, got: {applied}"


def test_goals_name_tab_rejected_post_migration_0009(tmp_path: Path) -> None:
    """After migration 0009, inserting a goal with name='\\t' raises IntegrityError."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations_including_0009(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (make_ulid(), "\t"),
            )
    finally:
        conn.close()


def test_goals_name_newline_rejected_post_migration_0009(tmp_path: Path) -> None:
    """After migration 0009, inserting a goal with name='\\n' raises IntegrityError."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations_including_0009(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (make_ulid(), "\n"),
            )
    finally:
        conn.close()


def test_goals_name_carriage_return_rejected_post_migration_0009(
    tmp_path: Path,
) -> None:
    """After migration 0009, inserting a goal with name='\\r' raises IntegrityError."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations_including_0009(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (make_ulid(), "\r"),
            )
    finally:
        conn.close()


def test_goals_name_mixed_whitespace_rejected_post_migration_0009(
    tmp_path: Path,
) -> None:
    """After migration 0009, a name of only mixed whitespace raises IntegrityError."""
    db = tmp_path / "tm.db"
    s = _apply_all_migrations_including_0009(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO goals (goal_id, name, status, created_at) "
                "VALUES (?, ?, 'active', datetime('now'))",
                (make_ulid(), " \t\n\r "),
            )
    finally:
        conn.close()


def test_goals_name_with_internal_whitespace_accepted_post_migration_0009(
    tmp_path: Path,
) -> None:
    """After migration 0009, a name with internal tabs/newlines is accepted.

    trim() only strips leading/trailing characters; internal whitespace is
    preserved so 'hello\\tworld' has non-zero trimmed length and passes the CHECK.
    """
    db = tmp_path / "tm.db"
    s = _apply_all_migrations_including_0009(db)
    s.close()

    conn = sqlite3.connect(str(db))
    try:
        goal_id = make_ulid()
        conn.execute(
            "INSERT INTO goals (goal_id, name, status, created_at) "
            "VALUES (?, ?, 'active', datetime('now'))",
            (goal_id, "hello\tworld"),
        )
        conn.commit()
        row = conn.execute(
            "SELECT name FROM goals WHERE goal_id = ?", (goal_id,)
        ).fetchone()
        assert row is not None
        assert row[0] == "hello\tworld"
    finally:
        conn.close()
