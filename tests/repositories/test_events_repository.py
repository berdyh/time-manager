"""Tests for EventsRepository (tm.repositories.events)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _make_db(db_path: Path) -> Path:
    """Apply all migrations and return the db_path."""
    from tm.store import Store

    store = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return db_path


def _make_repos(
    tmp_path: Path,
) -> tuple[EventsRepository, GoalsRepository]:
    db = _make_db(tmp_path / "tm.db")
    return EventsRepository(db), GoalsRepository(db)


# ---------------------------------------------------------------------------
# append_event
# ---------------------------------------------------------------------------


def test_append_event_minimal(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="EVT0001",
        case_id="CASE-A",
        activity="task:do_work",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )
    rows = events_repo.query_events(case_id="CASE-A")
    assert len(rows) == 1
    assert rows[0]["event_id"] == "EVT0001"
    assert rows[0]["case_id"] == "CASE-A"
    assert rows[0]["activity"] == "task:do_work"
    assert rows[0]["lifecycle"] == "complete"
    assert rows[0]["advances_goal"] is None


def test_append_event_with_attributes_round_trips(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    attrs = {"score": 42, "label": "alpha", "nested": {"x": 1}}
    events_repo.append_event(
        event_id="EVT0002",
        case_id="CASE-B",
        activity="task:analyze",
        timestamp="2026-05-05T11:00:00Z",
        lifecycle="complete",
        attributes=attrs,
        extractor_version="v1",
    )
    rows = events_repo.query_events(case_id="CASE-B")
    assert len(rows) == 1
    assert rows[0]["attributes"] == attrs
    assert rows[0]["extractor_version"] == "v1"


def test_append_event_with_advances_goal_validates(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="Valid Goal")
    # Should not raise
    events_repo.append_event(
        event_id="EVT0003",
        case_id="CASE-C",
        activity="task:step",
        timestamp="2026-05-05T12:00:00Z",
        lifecycle="complete",
        advances_goal=goal.goal_id,
    )
    rows = events_repo.query_events(case_id="CASE-C")
    assert len(rows) == 1
    assert rows[0]["advances_goal"] == goal.goal_id


def test_append_event_with_unknown_advances_goal_raises(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    with pytest.raises(ValueError, match="unknown goal"):
        events_repo.append_event(
            event_id="EVT0004",
            case_id="CASE-D",
            activity="task:step",
            timestamp="2026-05-05T13:00:00Z",
            lifecycle="complete",
            advances_goal="NONEXISTENT_GOAL_ID_12345",
        )


def test_append_event_with_advances_goal_none_does_not_validate(
    tmp_path: Path,
) -> None:
    """advances_goal=None must succeed even when no goals exist."""
    events_repo, _ = _make_repos(tmp_path)
    # No goals in the DB — should still succeed
    events_repo.append_event(
        event_id="EVT0005",
        case_id="CASE-E",
        activity="task:step",
        timestamp="2026-05-05T14:00:00Z",
        lifecycle="complete",
        advances_goal=None,
    )
    rows = events_repo.query_events(case_id="CASE-E")
    assert len(rows) == 1
    assert rows[0]["advances_goal"] is None


# ---------------------------------------------------------------------------
# query_events filters
# ---------------------------------------------------------------------------


def _seed_events(events_repo: EventsRepository, goal_id: str | None = None) -> None:
    """Insert three events with different case_ids, timestamps, activities."""
    events_repo.append_event(
        event_id="E001",
        case_id="CASE-1",
        activity="alpha",
        timestamp="2026-01-01T00:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="E002",
        case_id="CASE-2",
        activity="beta",
        timestamp="2026-02-01T00:00:00Z",
        lifecycle="complete",
        advances_goal=goal_id,
    )
    events_repo.append_event(
        event_id="E003",
        case_id="CASE-1",
        activity="gamma",
        timestamp="2026-03-01T00:00:00Z",
        lifecycle="start",
        advances_goal=goal_id,
    )


def test_query_filter_by_case_id(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="G1")
    _seed_events(events_repo, goal.goal_id)

    rows = events_repo.query_events(case_id="CASE-1")
    assert len(rows) == 2
    assert {r["event_id"] for r in rows} == {"E001", "E003"}


def test_query_filter_by_advances_goal(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="G2")
    _seed_events(events_repo, goal.goal_id)

    rows = events_repo.query_events(advances_goal=goal.goal_id)
    assert len(rows) == 2
    assert {r["event_id"] for r in rows} == {"E002", "E003"}


def test_query_filter_by_time_window(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    _seed_events(events_repo)

    # since inclusive, until exclusive
    rows = events_repo.query_events(
        since="2026-01-15T00:00:00Z",
        until="2026-02-15T00:00:00Z",
    )
    assert len(rows) == 1
    assert rows[0]["event_id"] == "E002"


def test_query_limit(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    _seed_events(events_repo)

    rows = events_repo.query_events(limit=2)
    assert len(rows) == 2
    # Results ordered by timestamp ASC — first two should be E001, E002
    assert rows[0]["event_id"] == "E001"
    assert rows[1]["event_id"] == "E002"


# ---------------------------------------------------------------------------
# count_advancing_goal
# ---------------------------------------------------------------------------


def test_count_advancing_goal_zero_for_no_events(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="Empty Goal")
    assert events_repo.count_advancing_goal(goal.goal_id) == 0


def test_count_advancing_goal_counts_correctly(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal_a = goals_repo.add(name="Goal A")
    goal_b = goals_repo.add(name="Goal B")

    events_repo.append_event(
        event_id="X001",
        case_id="CASE-X",
        activity="step",
        timestamp="2026-05-01T00:00:00Z",
        lifecycle="complete",
        advances_goal=goal_a.goal_id,
    )
    events_repo.append_event(
        event_id="X002",
        case_id="CASE-X",
        activity="step",
        timestamp="2026-05-02T00:00:00Z",
        lifecycle="complete",
        advances_goal=goal_a.goal_id,
    )
    events_repo.append_event(
        event_id="X003",
        case_id="CASE-X",
        activity="step",
        timestamp="2026-05-03T00:00:00Z",
        lifecycle="complete",
        advances_goal=goal_b.goal_id,
    )

    assert events_repo.count_advancing_goal(goal_a.goal_id) == 2
    assert events_repo.count_advancing_goal(goal_b.goal_id) == 1
