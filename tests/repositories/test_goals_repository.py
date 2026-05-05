"""Tests for GoalsRepository (tm.repositories.goals)."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path

import pytest

from tm.repositories.goals import GoalsRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _make_repo(db_path: Path) -> GoalsRepository:
    """Apply migrations and return a GoalsRepository for the given db_path."""
    # Apply migrations via the Store so the goals table is created.
    from tm.store import Store

    store = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return GoalsRepository(db_path)


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_returns_goal_with_id_and_default_status_active(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Learn SQLite")
    assert goal.goal_id != ""
    assert len(goal.goal_id) == 26
    assert goal.name == "Learn SQLite"
    assert goal.status == "active"
    assert goal.completed_at is None
    assert goal.abandoned_at is None
    assert goal.created_at is not None


def test_add_with_all_optional_fields(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    target = datetime(2026, 12, 31, 23, 59, 59)
    goal = repo.add(
        name="Full goal",
        description="Detailed description",
        priority=2,
        target_completion_at=target,
    )
    assert goal.description == "Detailed description"
    assert goal.priority == 2
    assert goal.target_completion_at is not None
    assert "2026-12-31" in goal.target_completion_at


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_none_for_unknown_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    result = repo.get("NONEXISTENT_GOAL_ID_000000")
    assert result is None


def test_get_returns_goal_for_known_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    created = repo.add(name="Findable goal")
    found = repo.get(created.goal_id)
    assert found is not None
    assert found.goal_id == created.goal_id
    assert found.name == "Findable goal"


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_filters_by_status(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    active1 = repo.add(name="Active goal 1")
    active2 = repo.add(name="Active goal 2")
    completed_goal = repo.add(name="To be completed")
    repo.complete(completed_goal.goal_id)

    active_list = repo.list(status="active")
    completed_list = repo.list(status="completed")
    abandoned_list = repo.list(status="abandoned")
    all_list = repo.list(status="all")

    active_ids = {g.goal_id for g in active_list}
    assert active1.goal_id in active_ids
    assert active2.goal_id in active_ids
    assert completed_goal.goal_id not in active_ids

    assert len(completed_list) == 1
    assert completed_list[0].goal_id == completed_goal.goal_id

    assert len(abandoned_list) == 0
    assert len(all_list) == 3


def test_list_returns_sorted_by_created_at_desc(tmp_path: Path) -> None:
    """Verify sort order using explicit created_at values via direct SQL insert.

    SQLite datetime('now') has 1-second granularity, so rapid inserts may land
    in the same second.  We bypass the repository for this test and insert
    rows with well-known timestamps to exercise the ORDER BY guarantee.
    """
    db = tmp_path / "tm.db"
    repo = _make_repo(db)

    conn = sqlite3.connect(str(db))
    try:
        from tm.models.goals import ulid as make_ulid

        id1 = make_ulid()
        id2 = make_ulid()
        id3 = make_ulid()
        conn.execute(
            "INSERT INTO goals (goal_id, name, created_at) VALUES (?,?,?)",
            (id1, "Goal 1", "2026-01-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO goals (goal_id, name, created_at) VALUES (?,?,?)",
            (id2, "Goal 2", "2026-02-01T00:00:00Z"),
        )
        conn.execute(
            "INSERT INTO goals (goal_id, name, created_at) VALUES (?,?,?)",
            (id3, "Goal 3", "2026-03-01T00:00:00Z"),
        )
        conn.commit()
    finally:
        conn.close()

    goals = repo.list()
    # Filter to just our inserted goals (there may be none from _make_repo)
    our_ids = {id1, id2, id3}
    relevant = [g for g in goals if g.goal_id in our_ids]
    assert [g.goal_id for g in relevant] == [id3, id2, id1], (
        "Expected newest first: Goal 3, Goal 2, Goal 1"
    )


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def test_complete_sets_status_and_timestamp(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Goal to complete")
    assert goal.status == "active"

    completed = repo.complete(goal.goal_id)
    assert completed.status == "completed"
    assert completed.completed_at is not None
    # Verify it persists via get
    fetched = repo.get(goal.goal_id)
    assert fetched is not None
    assert fetched.status == "completed"
    assert fetched.completed_at == completed.completed_at


def test_complete_raises_on_already_terminal(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Terminal goal")
    repo.complete(goal.goal_id)

    with pytest.raises(ValueError, match="terminal state"):
        repo.complete(goal.goal_id)


def test_complete_raises_on_unknown_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    with pytest.raises(ValueError, match="Unknown goal_id"):
        repo.complete("NO_SUCH_ID_000000000000000")


# ---------------------------------------------------------------------------
# abandon
# ---------------------------------------------------------------------------


def test_abandon_sets_status_and_reason(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Goal to abandon")

    abandoned = repo.abandon(goal.goal_id, reason="Not relevant anymore")
    assert abandoned.status == "abandoned"
    assert abandoned.abandoned_at is not None
    assert abandoned.abandon_reason == "Not relevant anymore"

    fetched = repo.get(goal.goal_id)
    assert fetched is not None
    assert fetched.status == "abandoned"
    assert fetched.abandon_reason == "Not relevant anymore"


def test_abandon_without_reason(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Reasonless abandon")
    abandoned = repo.abandon(goal.goal_id)
    assert abandoned.status == "abandoned"
    assert abandoned.abandon_reason is None


def test_abandon_raises_on_unknown_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    with pytest.raises(ValueError, match="Unknown goal_id"):
        repo.abandon("NO_SUCH_ID_000000000000000")


def test_abandon_raises_on_already_terminal(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Already done")
    repo.complete(goal.goal_id)
    with pytest.raises(ValueError, match="terminal state"):
        repo.abandon(goal.goal_id)


# ---------------------------------------------------------------------------
# priority + target_completion_at persistence
# ---------------------------------------------------------------------------


def test_priority_persists(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    goal = repo.add(name="Priority goal", priority=3)
    fetched = repo.get(goal.goal_id)
    assert fetched is not None
    assert fetched.priority == 3


def test_target_completion_at_persists_and_round_trips_iso(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    target = datetime(2027, 3, 15, 9, 0, 0)
    goal = repo.add(name="Deadline goal", target_completion_at=target)
    fetched = repo.get(goal.goal_id)
    assert fetched is not None
    assert fetched.target_completion_at is not None
    # Should round-trip as ISO 8601 with date portion preserved
    assert "2027-03-15" in fetched.target_completion_at
