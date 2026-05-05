"""Tests for the Goal dataclass and ULID helper (tm.models.goals)."""

from __future__ import annotations

import time

import pytest

from tm.models.goals import Goal, ulid

# ---------------------------------------------------------------------------
# Goal construction
# ---------------------------------------------------------------------------


def test_goal_construction_minimal() -> None:
    g = Goal(goal_id="01TESTID0000000000000000", name="Write tests")
    assert g.name == "Write tests"
    assert g.status == "active"
    assert g.description is None
    assert g.priority is None
    assert g.target_completion_at is None
    assert g.completed_at is None
    assert g.abandoned_at is None
    assert g.abandon_reason is None
    assert g.goal_id == "01TESTID0000000000000000"


def test_goal_construction_full() -> None:
    g = Goal(
        goal_id="01TESTID0000000000000001",
        name="Ship v1",
        description="Get first release out",
        status="completed",
        priority=1,
        target_completion_at="2026-06-01T00:00:00Z",
        created_at="2026-01-01T00:00:00Z",
        completed_at="2026-05-01T12:00:00Z",
        abandoned_at=None,
        abandon_reason=None,
    )
    assert g.name == "Ship v1"
    assert g.status == "completed"
    assert g.priority == 1
    assert g.target_completion_at == "2026-06-01T00:00:00Z"
    assert g.completed_at == "2026-05-01T12:00:00Z"


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_status_validation_rejects_unknown() -> None:
    with pytest.raises(ValueError, match="status must be one of"):
        Goal(goal_id="01TESTID0000000000000002", name="Bad status", status="pending")


def test_priority_validation_rejects_out_of_range() -> None:
    with pytest.raises(ValueError, match="priority must be an integer in"):
        Goal(goal_id="01TESTID0000000000000003", name="Bad priority", priority=5)

    with pytest.raises(ValueError, match="priority must be an integer in"):
        Goal(goal_id="01TESTID0000000000000004", name="Zero priority", priority=0)


def test_goal_name_must_not_be_empty() -> None:
    with pytest.raises(ValueError, match="name must not be empty"):
        Goal(goal_id="01TESTID0000000000000005", name="   ")


# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------


def test_ulid_generator_lengths_consistent() -> None:
    ids = [ulid() for _ in range(20)]
    assert all(len(u) == 26 for u in ids), "All ULIDs must be 26 characters"
    # All characters should be from the Crockford base32 alphabet
    crockford = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    assert all(c in crockford for u in ids for c in u)


def test_ulid_generator_produces_sortable_ids() -> None:
    """Generate 100 ULIDs rapidly and verify they are lexicographically sortable.

    ULIDs generated at the same millisecond may not be strictly ordered by the
    random component, but the sort order should match the creation order for
    IDs generated at different timestamps.  We use a small sleep approach:
    accept the test as passing if at least 95 % of adjacent pairs are in order,
    accounting for same-millisecond collisions.
    """
    ids: list[str] = []
    for _ in range(100):
        ids.append(ulid())
        # tiny sleep to spread timestamps
        time.sleep(0.001)

    out_of_order = sum(1 for a, b in zip(ids, ids[1:], strict=False) if a > b)
    # Allow up to 5 out-of-order pairs as tolerance for same-ms generation
    assert out_of_order <= 5, (
        f"{out_of_order} out-of-order adjacent pairs (expected <=5 for 100 ULIDs)"
    )


def test_ulid_generator_produces_unique_ids() -> None:
    ids = [ulid() for _ in range(200)]
    assert len(set(ids)) == 200, "ULIDs must be unique"


def test_ulid_characters_are_crockford_base32() -> None:
    crockford = set("0123456789ABCDEFGHJKMNPQRSTVWXYZ")
    for _ in range(20):
        u = ulid()
        assert all(c in crockford for c in u), f"Non-Crockford char in ULID: {u!r}"
