"""Tests for tm.models.outcome (T-OUT-01).

Two test groups:

1. Pure-function tests for ``compute_outcome_breakdown`` /
   ``compute_outcome_score`` — no database, just dict-shaped events.
2. Integration tests for ``OutcomeAggregator`` against a real
   :class:`EventsRepository` backed by a tmp_path SQLite file.
"""

from __future__ import annotations

import dataclasses
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from tm.models.outcome import (
    DEFAULT_TASK_COMPLETION_THRESHOLD,
    OutcomeAggregator,
    OutcomeBreakdown,
    compute_outcome_breakdown,
    compute_outcome_score,
)
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_event(
    *,
    advances_goal: str | None = None,
    planned_tasks_completed: Any = None,
    planned_tasks_total: Any = None,
    extra_attributes: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an event dict in the shape produced by EventsRepository._row_to_event."""
    attrs: dict[str, Any] = {}
    if planned_tasks_completed is not None:
        attrs["planned_tasks_completed"] = planned_tasks_completed
    if planned_tasks_total is not None:
        attrs["planned_tasks_total"] = planned_tasks_total
    if extra_attributes:
        attrs.update(extra_attributes)
    return {
        "event_id": "EVT",
        "case_id": "CASE",
        "activity": "task:work",
        "timestamp": "2026-05-05T10:00:00Z",
        "lifecycle": "complete",
        "resource": None,
        "extractor_version": "v1",
        "created_at": "2026-05-05T10:00:00Z",
        "advances_goal": advances_goal,
        "vocab_version": None,
        "schema_version": "v1",
        "case_date": "2026-05-05",
        "case_goal_id": None,
        "attributes": attrs,
    }


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


# ===========================================================================
# Pure-function tests (no DB)
# ===========================================================================


def test_compute_outcome_zero_events_returns_zero() -> None:
    breakdown = compute_outcome_breakdown([])
    assert breakdown.outcome_score == 0
    assert breakdown.did_complete_tasks is False
    assert breakdown.did_advance_goal is False
    assert breakdown.planned_tasks_completed == 0
    assert breakdown.planned_tasks_total == 0
    assert breakdown.advancing_goal_event_count == 0
    assert breakdown.case_event_count == 0


def test_compute_outcome_did_complete_only_returns_one() -> None:
    """8/10 tasks completed (>= 0.80), but no event tags advances_goal."""
    events = [_make_event(planned_tasks_completed=8, planned_tasks_total=10)]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.did_complete_tasks is True
    assert breakdown.did_advance_goal is False
    assert breakdown.outcome_score == 1


def test_compute_outcome_did_advance_only_returns_one() -> None:
    """advances_goal set, but no planned_tasks attributes anywhere."""
    events = [
        _make_event(advances_goal="GOAL_X"),
        _make_event(advances_goal=None),
    ]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.did_complete_tasks is False
    assert breakdown.did_advance_goal is True
    assert breakdown.outcome_score == 1
    assert breakdown.advancing_goal_event_count == 1


def test_compute_outcome_did_advance_only_with_subthreshold_completion() -> None:
    """advances_goal set, but completion ratio is below threshold."""
    events = [
        _make_event(
            advances_goal="GOAL_X",
            planned_tasks_completed=5,
            planned_tasks_total=10,
        )
    ]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.did_complete_tasks is False
    assert breakdown.did_advance_goal is True
    assert breakdown.outcome_score == 1


def test_compute_outcome_both_returns_two() -> None:
    events = [
        _make_event(
            advances_goal="GOAL_X",
            planned_tasks_completed=9,
            planned_tasks_total=10,
        )
    ]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.did_complete_tasks is True
    assert breakdown.did_advance_goal is True
    assert breakdown.outcome_score == 2


def test_compute_outcome_threshold_boundary() -> None:
    """8/10 = 0.80 exactly is >= threshold so did_complete_tasks is True."""
    events = [_make_event(planned_tasks_completed=8, planned_tasks_total=10)]
    assert compute_outcome_score(events) == 1
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.did_complete_tasks is True


def test_compute_outcome_threshold_just_below() -> None:
    """7/10 = 0.70 with default 0.80 threshold is False."""
    events = [_make_event(planned_tasks_completed=7, planned_tasks_total=10)]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.did_complete_tasks is False
    assert breakdown.outcome_score == 0


def test_compute_outcome_custom_threshold() -> None:
    """With threshold=0.5, 6/10 = 0.60 should trip did_complete=True."""
    events = [_make_event(planned_tasks_completed=6, planned_tasks_total=10)]
    breakdown = compute_outcome_breakdown(events, threshold=0.5)
    assert breakdown.did_complete_tasks is True
    assert breakdown.outcome_score == 1


def test_compute_outcome_picks_max_planned_values_across_events() -> None:
    """When multiple events have planned_tasks attributes, take the MAX of each.

    Mid-day debrief might have completed=3/total=5; end-of-day refresh might
    have completed=4/total=5.  We want the latter's view of the world, modeled
    as the per-attribute MAX across events.
    """
    events = [
        _make_event(planned_tasks_completed=3, planned_tasks_total=5),
        _make_event(planned_tasks_completed=4, planned_tasks_total=5),
    ]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.planned_tasks_completed == 4
    assert breakdown.planned_tasks_total == 5
    # 4/5 = 0.80 -> True
    assert breakdown.did_complete_tasks is True


def test_compute_outcome_advances_goal_counted() -> None:
    """5 events, 2 with advances_goal set; advancing count = 2; flag True."""
    events = [
        _make_event(advances_goal=None),
        _make_event(advances_goal="GOAL_A"),
        _make_event(advances_goal=None),
        _make_event(advances_goal="GOAL_B"),
        _make_event(advances_goal=None),
    ]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.advancing_goal_event_count == 2
    assert breakdown.case_event_count == 5
    assert breakdown.did_advance_goal is True


def test_compute_outcome_total_zero_blocks_did_complete() -> None:
    """planned_tasks_completed=5, planned_tasks_total=0 -> did_complete=False."""
    events = [_make_event(planned_tasks_completed=5, planned_tasks_total=0)]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.planned_tasks_total == 0
    assert breakdown.did_complete_tasks is False
    assert breakdown.outcome_score == 0


def test_compute_outcome_only_completed_attribute_no_total() -> None:
    """Only planned_tasks_completed present (no total) -> did_complete=False."""
    events = [_make_event(planned_tasks_completed=10)]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.planned_tasks_total == 0
    assert breakdown.did_complete_tasks is False


def test_compute_outcome_string_attribute_values_coerced() -> None:
    """Defensive coercion: LLM-extracted attrs may surface as JSON strings."""
    events = [_make_event(planned_tasks_completed="8", planned_tasks_total="10")]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.planned_tasks_completed == 8
    assert breakdown.planned_tasks_total == 10
    assert breakdown.did_complete_tasks is True


def test_compute_outcome_garbage_attribute_values_treated_as_zero() -> None:
    """Non-coercible values fall back to 0 (no exception)."""
    events = [
        _make_event(
            planned_tasks_completed="not-a-number",
            planned_tasks_total={"oops": "dict"},
        )
    ]
    breakdown = compute_outcome_breakdown(events)
    assert breakdown.planned_tasks_completed == 0
    assert breakdown.planned_tasks_total == 0
    assert breakdown.did_complete_tasks is False


def test_compute_outcome_score_wrapper_returns_int() -> None:
    events = [
        _make_event(
            advances_goal="GOAL_X",
            planned_tasks_completed=10,
            planned_tasks_total=10,
        )
    ]
    score = compute_outcome_score(events)
    assert isinstance(score, int)
    assert score == 2


def test_compute_outcome_invalid_threshold_raises() -> None:
    with pytest.raises(ValueError, match="threshold"):
        compute_outcome_breakdown([], threshold=0.0)
    with pytest.raises(ValueError, match="threshold"):
        compute_outcome_breakdown([], threshold=1.5)
    with pytest.raises(ValueError, match="threshold"):
        compute_outcome_breakdown([], threshold=-0.1)


def test_outcome_breakdown_dataclass_frozen() -> None:
    breakdown = compute_outcome_breakdown([])
    # Frozen dataclasses raise dataclasses.FrozenInstanceError (a TypeError)
    # when an attribute is assigned.
    with pytest.raises(dataclasses.FrozenInstanceError):
        breakdown.outcome_score = 99  # type: ignore[misc]


def test_outcome_breakdown_default_threshold_constant() -> None:
    """Spot-check the public threshold constant matches the locked plan."""
    assert DEFAULT_TASK_COMPLETION_THRESHOLD == 0.80


def test_outcome_breakdown_is_returned_type() -> None:
    """compute_outcome_breakdown returns the public OutcomeBreakdown type."""
    breakdown = compute_outcome_breakdown([])
    assert isinstance(breakdown, OutcomeBreakdown)


# ===========================================================================
# Aggregator integration tests (use EventsRepository against tmp_path SQLite)
# ===========================================================================


def test_aggregator_for_case_date_with_no_events(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    agg = OutcomeAggregator(events_repo)
    breakdown = agg.for_case_date("2026-05-05")
    assert breakdown.outcome_score == 0
    assert breakdown.case_event_count == 0


def test_aggregator_for_case_date_returns_breakdown(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="Daily Goal")

    # Three events on 2026-05-05 — one tags advances_goal, and one carries
    # the planned-tasks attributes (90% completion).
    events_repo.append_event(
        event_id="E1",
        case_id="CASE-D1",
        activity="meeting",
        timestamp="2026-05-05T09:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="E2",
        case_id="CASE-D1",
        activity="task:focus",
        timestamp="2026-05-05T11:00:00Z",
        lifecycle="complete",
        advances_goal=goal.goal_id,
    )
    events_repo.append_event(
        event_id="E3",
        case_id="CASE-D1",
        activity="day:debrief",
        timestamp="2026-05-05T18:00:00Z",
        lifecycle="complete",
        attributes={"planned_tasks_completed": 9, "planned_tasks_total": 10},
    )

    agg = OutcomeAggregator(events_repo)
    breakdown = agg.for_case_date("2026-05-05")
    assert breakdown.case_event_count == 3
    assert breakdown.did_complete_tasks is True
    assert breakdown.did_advance_goal is True
    assert breakdown.outcome_score == 2
    assert breakdown.planned_tasks_completed == 9
    assert breakdown.planned_tasks_total == 10
    assert breakdown.advancing_goal_event_count == 1


def test_aggregator_for_case_date_isolates_by_date(tmp_path: Path) -> None:
    """Events from a different case_date must not bleed into the breakdown."""
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E1",
        case_id="C",
        activity="meeting",
        timestamp="2026-05-04T09:00:00Z",
        lifecycle="complete",
        attributes={"planned_tasks_completed": 10, "planned_tasks_total": 10},
    )
    events_repo.append_event(
        event_id="E2",
        case_id="C",
        activity="meeting",
        timestamp="2026-05-05T09:00:00Z",
        lifecycle="complete",
    )
    agg = OutcomeAggregator(events_repo)
    # The 2026-05-05 day should NOT see the 5-04 attrs.
    breakdown = agg.for_case_date("2026-05-05")
    assert breakdown.case_event_count == 1
    assert breakdown.planned_tasks_total == 0
    assert breakdown.outcome_score == 0


def test_aggregator_for_case_date_empty_string_raises(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    agg = OutcomeAggregator(events_repo)
    with pytest.raises(ValueError, match="case_date"):
        agg.for_case_date("")


def test_aggregator_for_case_goal_id(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="Pursued Goal")

    # Two events tagged with case_goal_id, one of which advances the goal,
    # plus an unrelated event NOT tagged with case_goal_id.
    events_repo.append_event(
        event_id="G1",
        case_id="CASE-G",
        activity="task:research",
        timestamp="2026-05-05T09:00:00Z",
        lifecycle="complete",
        advances_goal=goal.goal_id,
        case_goal_id=goal.goal_id,
    )
    events_repo.append_event(
        event_id="G2",
        case_id="CASE-G",
        activity="task:write",
        timestamp="2026-05-05T11:00:00Z",
        lifecycle="complete",
        attributes={"planned_tasks_completed": 4, "planned_tasks_total": 5},
        case_goal_id=goal.goal_id,
    )
    events_repo.append_event(
        event_id="G3",
        case_id="CASE-OTHER",
        activity="meeting",
        timestamp="2026-05-05T15:00:00Z",
        lifecycle="complete",
    )

    agg = OutcomeAggregator(events_repo)
    breakdown = agg.for_case_goal_id(goal.goal_id)
    assert breakdown.case_event_count == 2  # only the case_goal_id-tagged ones
    # 4/5 = 0.80 -> True; one event advances_goal -> True.
    assert breakdown.did_complete_tasks is True
    assert breakdown.did_advance_goal is True
    assert breakdown.outcome_score == 2


def test_aggregator_for_case_goal_id_empty_string_raises(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    agg = OutcomeAggregator(events_repo)
    with pytest.raises(ValueError, match="case_goal_id"):
        agg.for_case_goal_id("")


def test_aggregator_for_date_range_returns_dict(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="Range Goal")

    # Three distinct case_dates, each with its own outcome shape.
    # 2026-05-03: only advances_goal -> score 1
    events_repo.append_event(
        event_id="R1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-03T10:00:00Z",
        lifecycle="complete",
        advances_goal=goal.goal_id,
    )
    # 2026-05-04: only task completion -> score 1
    events_repo.append_event(
        event_id="R2",
        case_id="C2",
        activity="day:debrief",
        timestamp="2026-05-04T18:00:00Z",
        lifecycle="complete",
        attributes={"planned_tasks_completed": 8, "planned_tasks_total": 10},
    )
    # 2026-05-05: both -> score 2
    events_repo.append_event(
        event_id="R3",
        case_id="C3",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        advances_goal=goal.goal_id,
        attributes={"planned_tasks_completed": 10, "planned_tasks_total": 10},
    )

    agg = OutcomeAggregator(events_repo)
    result = agg.for_date_range(since="2026-05-03", until="2026-05-05")
    assert set(result.keys()) == {"2026-05-03", "2026-05-04", "2026-05-05"}
    assert result["2026-05-03"].outcome_score == 1
    assert result["2026-05-03"].did_advance_goal is True
    assert result["2026-05-03"].did_complete_tasks is False
    assert result["2026-05-04"].outcome_score == 1
    assert result["2026-05-04"].did_complete_tasks is True
    assert result["2026-05-04"].did_advance_goal is False
    assert result["2026-05-05"].outcome_score == 2


def test_aggregator_for_date_range_excludes_empty_case_date(tmp_path: Path) -> None:
    """An event with case_date='' (raw insert path) must not appear in results."""
    events_repo, _ = _make_repos(tmp_path)

    # Insert a "real" event normally so we have at least one valid date.
    events_repo.append_event(
        event_id="OK",
        case_id="C-OK",
        activity="meeting",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )

    # Insert an event with case_date='' via raw sqlite, bypassing the
    # validating append_event path.  This mirrors the legacy pre-T-PM-01 row
    # shape that list_distinct_case_dates already filters.
    db_path = tmp_path / "tm.db"
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            "INSERT INTO events ("
            "event_id, case_id, activity, timestamp, lifecycle, "
            "extractor_version, schema_version, case_date"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "RAW",
                "C-RAW",
                "task:legacy",
                "2026-05-05T11:00:00Z",
                "complete",
                "v0",
                "v1",
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    agg = OutcomeAggregator(events_repo)
    result = agg.for_date_range(since="2026-05-01", until="2026-05-31")
    # Only the "real" 2026-05-05 entry should be present; the empty-string
    # row must NOT surface as its own bucket nor join the 2026-05-05 bucket.
    assert set(result.keys()) == {"2026-05-05"}
    assert result["2026-05-05"].case_event_count == 1


def test_aggregator_for_date_range_threshold_propagates(tmp_path: Path) -> None:
    """The threshold kwarg flows through to per-date breakdowns."""
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="T1",
        case_id="C",
        activity="day:debrief",
        timestamp="2026-05-05T18:00:00Z",
        lifecycle="complete",
        attributes={"planned_tasks_completed": 6, "planned_tasks_total": 10},
    )
    agg = OutcomeAggregator(events_repo)
    # Default 0.80 threshold -> 6/10 = 0.6 -> False
    default_result = agg.for_date_range(since="2026-05-05", until="2026-05-05")
    assert default_result["2026-05-05"].did_complete_tasks is False
    # Custom 0.5 threshold -> 6/10 = 0.6 -> True
    custom_result = agg.for_date_range(
        since="2026-05-05", until="2026-05-05", threshold=0.5
    )
    assert custom_result["2026-05-05"].did_complete_tasks is True
