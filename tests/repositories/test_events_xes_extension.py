"""Tests for the XES extension columns added by migration 0006.

Covers:
- migration 0006 adds the four expected columns with correct defaults
- ``append_event`` accepts and persists vocab_version / schema_version /
  case_date / case_goal_id (and validates case_goal_id as a soft FK)
- ``case_date`` is derived from ``timestamp`` when not explicitly provided,
  and a malformed timestamp raises ``ValueError`` *before* INSERT
- ``query_events`` filters by all four new columns
- ``list_distinct_case_dates`` excludes empty-string sentinels and respects
  the ``[since, until]`` window
- ``_derive_case_date`` helper unit cases
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from tm.repositories.events import EventsRepository, _derive_case_date
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
# Migration 0006: schema shape
# ---------------------------------------------------------------------------


def test_migration_0006_adds_four_columns(tmp_path: Path) -> None:
    db = _make_db(tmp_path / "tm.db")
    conn = sqlite3.connect(db)
    try:
        rows = conn.execute("PRAGMA table_info(events)").fetchall()
        names = {r[1] for r in rows}
    finally:
        conn.close()
    assert {"vocab_version", "schema_version", "case_date", "case_goal_id"} <= names


def test_existing_events_get_default_schema_version_v1(tmp_path: Path) -> None:
    """A raw insert that omits schema_version falls back to the column default 'v1'."""
    db = _make_db(tmp_path / "tm.db")
    conn = sqlite3.connect(db)
    try:
        # Insert a row directly without schema_version / case_date to confirm
        # the column DEFAULTs apply (mimics any backfill / old-path inserts).
        conn.execute(
            "INSERT INTO events ("
            "event_id, case_id, activity, timestamp, lifecycle, "
            "extractor_version"
            ") VALUES (?, ?, ?, ?, ?, ?)",
            (
                "E_RAW",
                "CASE-RAW",
                "task:legacy",
                "2026-05-05T00:00:00Z",
                "complete",
                "v0",
            ),
        )
        conn.commit()
        row = conn.execute(
            "SELECT schema_version, case_date, vocab_version, case_goal_id "
            "FROM events WHERE event_id = ?",
            ("E_RAW",),
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "v1"
    assert row[1] == ""  # empty-string sentinel
    assert row[2] is None
    assert row[3] is None


# ---------------------------------------------------------------------------
# append_event with the new kwargs
# ---------------------------------------------------------------------------


def test_append_event_with_explicit_schema_version(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E_SV",
        case_id="CASE-SV",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        schema_version="v2",
    )
    rows = events_repo.query_events(case_id="CASE-SV")
    assert len(rows) == 1
    assert rows[0]["schema_version"] == "v2"


def test_append_event_derives_case_date_from_timestamp(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E_CD1",
        case_id="CASE-CD1",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )
    rows = events_repo.query_events(case_id="CASE-CD1")
    assert len(rows) == 1
    assert rows[0]["case_date"] == "2026-05-05"


def test_append_event_explicit_case_date_overrides_derivation(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E_CD2",
        case_id="CASE-CD2",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        case_date="2026-12-31",
    )
    rows = events_repo.query_events(case_id="CASE-CD2")
    assert len(rows) == 1
    assert rows[0]["case_date"] == "2026-12-31"


def test_append_event_with_case_goal_id_validates(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal = goals_repo.add(name="Pursuit Goal")
    events_repo.append_event(
        event_id="E_CGI",
        case_id="CASE-CGI",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        case_goal_id=goal.goal_id,
    )
    rows = events_repo.query_events(case_id="CASE-CGI")
    assert len(rows) == 1
    assert rows[0]["case_goal_id"] == goal.goal_id


def test_append_event_with_unknown_case_goal_id_raises(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    with pytest.raises(ValueError, match="unknown goal"):
        events_repo.append_event(
            event_id="E_BAD",
            case_id="CASE-BAD",
            activity="task:do",
            timestamp="2026-05-05T10:00:00Z",
            lifecycle="complete",
            case_goal_id="nope",
        )


def test_append_event_with_case_goal_id_None_does_not_validate(tmp_path: Path) -> None:
    """case_goal_id=None must succeed even when no goals exist."""
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E_NG",
        case_id="CASE-NG",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        case_goal_id=None,
    )
    rows = events_repo.query_events(case_id="CASE-NG")
    assert len(rows) == 1
    assert rows[0]["case_goal_id"] is None


def test_append_event_advances_goal_and_case_goal_id_can_differ(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    goal_a = goals_repo.add(name="A")
    goal_b = goals_repo.add(name="B")
    events_repo.append_event(
        event_id="E_DIFF",
        case_id="CASE-DIFF",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        advances_goal=goal_a.goal_id,
        case_goal_id=goal_b.goal_id,
    )
    rows = events_repo.query_events(case_id="CASE-DIFF")
    assert len(rows) == 1
    assert rows[0]["advances_goal"] == goal_a.goal_id
    assert rows[0]["case_goal_id"] == goal_b.goal_id


def test_append_event_with_vocab_version_persists(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E_VV",
        case_id="CASE-VV",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        vocab_version="vocab-1.2.3",
    )
    rows = events_repo.query_events(case_id="CASE-VV")
    assert len(rows) == 1
    assert rows[0]["vocab_version"] == "vocab-1.2.3"


def test_append_event_malformed_timestamp_raises_before_insert(tmp_path: Path) -> None:
    """Malformed timestamp must raise ValueError BEFORE INSERT (no '' rows)."""
    events_repo, _ = _make_repos(tmp_path)
    with pytest.raises(ValueError, match="invalid timestamp"):
        events_repo.append_event(
            event_id="E_BADTS",
            case_id="CASE-BADTS",
            activity="task:do",
            timestamp="garbage",
            lifecycle="complete",
        )
    # Confirm no row was written.
    rows = events_repo.query_events(case_id="CASE-BADTS")
    assert rows == []


# ---------------------------------------------------------------------------
# query_events filters on the new columns
# ---------------------------------------------------------------------------


def test_query_filter_by_case_date(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="E2",
        case_id="C2",
        activity="task:do",
        timestamp="2026-05-06T10:00:00Z",
        lifecycle="complete",
    )
    rows = events_repo.query_events(case_date="2026-05-06")
    assert [r["event_id"] for r in rows] == ["E2"]


def test_query_filter_by_case_goal_id(tmp_path: Path) -> None:
    events_repo, goals_repo = _make_repos(tmp_path)
    g = goals_repo.add(name="QG")
    events_repo.append_event(
        event_id="E1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        case_goal_id=g.goal_id,
    )
    events_repo.append_event(
        event_id="E2",
        case_id="C2",
        activity="task:do",
        timestamp="2026-05-05T11:00:00Z",
        lifecycle="complete",
    )
    rows = events_repo.query_events(case_goal_id=g.goal_id)
    assert [r["event_id"] for r in rows] == ["E1"]


def test_query_filter_by_vocab_version(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
        vocab_version="v1.0",
    )
    events_repo.append_event(
        event_id="E2",
        case_id="C2",
        activity="task:do",
        timestamp="2026-05-05T11:00:00Z",
        lifecycle="complete",
        vocab_version="v2.0",
    )
    rows = events_repo.query_events(vocab_version="v2.0")
    assert [r["event_id"] for r in rows] == ["E2"]


def test_query_filter_by_schema_version(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="E1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="E2",
        case_id="C2",
        activity="task:do",
        timestamp="2026-05-05T11:00:00Z",
        lifecycle="complete",
        schema_version="v2",
    )
    rows = events_repo.query_events(schema_version="v1")
    assert [r["event_id"] for r in rows] == ["E1"]
    rows2 = events_repo.query_events(schema_version="v2")
    assert [r["event_id"] for r in rows2] == ["E2"]


# ---------------------------------------------------------------------------
# list_distinct_case_dates
# ---------------------------------------------------------------------------


def _seed_case_dates(events_repo: EventsRepository) -> None:
    events_repo.append_event(
        event_id="D1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="D2",
        case_id="C2",
        activity="task:do",
        timestamp="2026-05-06T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="D3",
        case_id="C3",
        activity="task:do",
        timestamp="2026-05-05T12:00:00Z",
        lifecycle="complete",
    )


def test_list_distinct_case_dates_excludes_empty(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    _seed_case_dates(events_repo)
    # Insert a row directly with empty case_date to confirm it's excluded.
    db = tmp_path / "tm.db"
    conn = sqlite3.connect(db)
    try:
        conn.execute(
            "INSERT INTO events ("
            "event_id, case_id, activity, timestamp, lifecycle, "
            "extractor_version, case_date"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                "D_EMPTY",
                "C_EMPTY",
                "task:do",
                "2026-05-07T10:00:00Z",
                "complete",
                "v0",
                "",
            ),
        )
        conn.commit()
    finally:
        conn.close()

    dates = events_repo.list_distinct_case_dates()
    assert dates == ["2026-05-05", "2026-05-06"]


def test_list_distinct_case_dates_respects_window(tmp_path: Path) -> None:
    events_repo, _ = _make_repos(tmp_path)
    events_repo.append_event(
        event_id="W1",
        case_id="C1",
        activity="task:do",
        timestamp="2026-05-04T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="W2",
        case_id="C2",
        activity="task:do",
        timestamp="2026-05-05T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="W3",
        case_id="C3",
        activity="task:do",
        timestamp="2026-05-06T10:00:00Z",
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="W4",
        case_id="C4",
        activity="task:do",
        timestamp="2026-05-07T10:00:00Z",
        lifecycle="complete",
    )
    dates = events_repo.list_distinct_case_dates(
        since="2026-05-05",
        until="2026-05-06",
    )
    assert dates == ["2026-05-05", "2026-05-06"]


# ---------------------------------------------------------------------------
# _derive_case_date helper unit tests
# ---------------------------------------------------------------------------


def test_derive_case_date_from_iso_t_z() -> None:
    assert _derive_case_date("2026-05-05T10:00:00Z") == "2026-05-05"


def test_derive_case_date_from_bare_date() -> None:
    assert _derive_case_date("2026-05-05") == "2026-05-05"


def test_derive_case_date_invalid_raises() -> None:
    with pytest.raises(ValueError, match="invalid timestamp"):
        _derive_case_date("garbage")


def test_derive_case_date_empty_raises() -> None:
    with pytest.raises(ValueError, match="invalid timestamp"):
        _derive_case_date("")
