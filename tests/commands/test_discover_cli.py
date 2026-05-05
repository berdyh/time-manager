"""CLI tests for `tm discover`.

All tests use typer's CliRunner against a temporary SQLite database.
typer 0.25.1: use plain CliRunner() (no mix_stderr) and result.output.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.repositories.events import EventsRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tm_test.db"
    store = SQLiteStore(db_path)
    store.apply_pending_migrations()
    store.close()
    return db_path


def _invoke(*args: str, db_path: Path) -> object:
    return runner.invoke(app, ["discover", *args, "--db-path", str(db_path)])


def _seed_events(
    db_path: Path,
    events: list[dict],
) -> None:
    """Seed a list of event dicts into the events table."""
    repo = EventsRepository(db_path)
    for ev in events:
        repo.append_event(**ev)


def _workday_events(
    case_date: str, activities_and_times: list[tuple[str, str]]
) -> list[dict]:
    """Build workday-lens events for a single case_date."""
    rows = []
    for activity, ts in activities_and_times:
        rows.append(
            {
                "event_id": str(uuid.uuid4()),
                "case_id": f"case-{case_date}",
                "activity": activity,
                "timestamp": ts,
                "lifecycle": "complete",
                "case_date": case_date,
            }
        )
    return rows


# ---------------------------------------------------------------------------
# Help / smoke tests
# ---------------------------------------------------------------------------


def test_discover_help_shows_options() -> None:
    result = runner.invoke(app, ["discover", "--help"])
    assert result.exit_code == 0, result.output
    assert "--lens" in result.output
    assert "--since" in result.output
    assert "--until" in result.output
    assert "--db-path" in result.output


# ---------------------------------------------------------------------------
# Empty log
# ---------------------------------------------------------------------------


def test_discover_empty_log_prints_no_events_message_exit_0(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke(db_path=db)
    assert result.exit_code == 0, result.output
    assert "no events" in result.output


# ---------------------------------------------------------------------------
# Invalid lens
# ---------------------------------------------------------------------------


def test_discover_invalid_lens_exits_2(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("--lens", "badlens", db_path=db)
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Seeded events — workday lens
# ---------------------------------------------------------------------------


def test_discover_with_seeded_events_prints_summary(tmp_path: Path) -> None:
    db = _db(tmp_path)
    events = _workday_events(
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
            ("meeting", "2026-01-05T11:00:00Z"),
        ],
    )
    _seed_events(db, events)

    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    assert "DISCOVERED MODEL" in result.output
    assert "workday" in result.output
    assert "1 cases" in result.output
    assert "process_tree:" in result.output
    assert "petri_net:" in result.output
    assert "fitness:" in result.output
    assert "precision:" in result.output


def test_discover_lens_workday(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Two separate workdays = 2 cases
    events = _workday_events(
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
        ],
    ) + _workday_events(
        "2026-01-06",
        [
            ("deep_work", "2026-01-06T09:00:00Z"),
            ("meeting", "2026-01-06T10:00:00Z"),
        ],
    )
    _seed_events(db, events)

    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    assert "DISCOVERED MODEL" in result.output
    assert "2 cases" in result.output


def test_discover_lens_goal_pursuit(tmp_path: Path) -> None:
    """goal_pursuit lens uses case_goal_id; we must create a goal first."""
    db = _db(tmp_path)

    # Create a goal so the FK constraint passes
    from tm.repositories.goals import GoalsRepository

    goals_repo = GoalsRepository(db)
    goal = goals_repo.add(name="test goal")
    goal_id = goal.goal_id

    repo = EventsRepository(db)
    for i, (activity, ts) in enumerate(
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
            ("review", "2026-01-05T11:00:00Z"),
        ]
    ):
        repo.append_event(
            event_id=str(uuid.uuid4()),
            case_id=f"case-gp-{i}",
            activity=activity,
            timestamp=ts,
            lifecycle="complete",
            case_goal_id=goal_id,
        )

    result = _invoke("--lens", "goal_pursuit", db_path=db)
    assert result.exit_code == 0, result.output
    assert "DISCOVERED MODEL" in result.output
    assert "goal_pursuit" in result.output
    assert "1 cases" in result.output


def test_discover_filters_by_time_window(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Events on two different days
    events = _workday_events(
        "2026-01-05",
        [("deep_work", "2026-01-05T09:00:00Z"), ("email", "2026-01-05T10:00:00Z")],
    ) + _workday_events(
        "2026-01-06",
        [("meeting", "2026-01-06T09:00:00Z"), ("review", "2026-01-06T10:00:00Z")],
    )
    _seed_events(db, events)

    # Only include 2026-01-05 events
    result = _invoke(
        "--lens",
        "workday",
        "--since",
        "2026-01-05",
        "--until",
        "2026-01-06",
        db_path=db,
    )
    assert result.exit_code == 0, result.output
    assert "DISCOVERED MODEL" in result.output
    assert "1 cases" in result.output


def test_discover_shows_activity_count(tmp_path: Path) -> None:
    db = _db(tmp_path)
    events = _workday_events(
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
            ("meeting", "2026-01-05T11:00:00Z"),
        ],
    )
    _seed_events(db, events)
    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    # 3 distinct activities
    assert "3 activities" in result.output


def test_discover_fitness_and_precision_numeric(tmp_path: Path) -> None:
    db = _db(tmp_path)
    events = _workday_events(
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
        ],
    )
    _seed_events(db, events)
    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    output = result.output
    # fitness and precision lines must exist
    assert "fitness:" in output
    assert "precision:" in output
