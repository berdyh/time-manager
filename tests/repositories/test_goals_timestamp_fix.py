"""Regression test: GoalsRepository.add must persist created_at in project
ISO-T-Z format ('%Y-%m-%dT%H:%M:%SZ'), not the SQLite default space-separator
format ('%Y-%m-%d %H:%M:%S').
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from tm.repositories.goals import GoalsRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"

_ISO_TZ_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")


def _make_repo(db_path: Path) -> GoalsRepository:
    from tm.store import Store

    store = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return GoalsRepository(db_path)


def test_add_persists_created_at_in_iso_tz_format(tmp_path: Path) -> None:
    """created_at stored by GoalsRepository.add must match '%Y-%m-%dT%H:%M:%SZ'.

    Reads the raw value directly from sqlite3 (bypassing _row_to_goal) to
    ensure the DB value itself is correct, not a reformatted representation.
    """
    db_path = tmp_path / "tm.db"
    repo = _make_repo(db_path)
    goal = repo.add(name="Timestamp test goal")

    # Read raw created_at from the database — do NOT go through GoalsRepository
    # to avoid any reformatting that might mask the bug.
    conn = sqlite3.connect(str(db_path))
    try:
        row = conn.execute(
            "SELECT created_at FROM goals WHERE goal_id = ?", (goal.goal_id,)
        ).fetchone()
    finally:
        conn.close()

    assert row is not None, "Goal row should exist after insert"
    raw_created_at: str = row[0]

    assert _ISO_TZ_RE.match(raw_created_at), (
        f"created_at in DB must match '%Y-%m-%dT%H:%M:%SZ' (ISO-T-Z), "
        f"got: {raw_created_at!r}. "
        f"SQLite's datetime('now') produces space-separated format without Z — "
        f"GoalsRepository.add must pass created_at explicitly."
    )
