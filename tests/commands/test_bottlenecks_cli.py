"""CLI tests for `tm bottlenecks`.

All tests use typer's CliRunner against a temporary SQLite database.
typer 0.25.1: use plain CliRunner() (no mix_stderr) and result.output.
"""

from __future__ import annotations

import uuid
from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.commands.bottlenecks import _humanize_seconds
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
    return runner.invoke(app, ["bottlenecks", *args, "--db-path", str(db_path)])


def _seed_workday_events(
    db_path: Path,
    case_date: str,
    activities_and_times: list[tuple[str, str]],
) -> None:
    repo = EventsRepository(db_path)
    for activity, ts in activities_and_times:
        repo.append_event(
            event_id=str(uuid.uuid4()),
            case_id=f"case-{case_date}",
            activity=activity,
            timestamp=ts,
            lifecycle="complete",
            case_date=case_date,
        )


# ---------------------------------------------------------------------------
# Humanize helper unit tests
# ---------------------------------------------------------------------------


def test_humanize_none_returns_na() -> None:
    assert _humanize_seconds(None) == "n/a"


def test_humanize_seconds_only() -> None:
    assert _humanize_seconds(30.0) == "30s"


def test_humanize_minutes_and_seconds() -> None:
    assert _humanize_seconds(90.0) == "1m30s"


def test_humanize_hours_and_minutes() -> None:
    # 2h15m = 8100 seconds
    assert _humanize_seconds(8100.0) == "2h15m"


def test_humanize_exact_hour() -> None:
    assert _humanize_seconds(3600.0) == "1h0m"


def test_humanize_zero() -> None:
    assert _humanize_seconds(0.0) == "0s"


# ---------------------------------------------------------------------------
# Help / smoke tests
# ---------------------------------------------------------------------------


def test_bottlenecks_help() -> None:
    result = runner.invoke(app, ["bottlenecks", "--help"])
    assert result.exit_code == 0, result.output
    assert "--lens" in result.output
    assert "--since" in result.output
    assert "--until" in result.output
    assert "--db-path" in result.output


# ---------------------------------------------------------------------------
# Empty log
# ---------------------------------------------------------------------------


def test_bottlenecks_empty_log_friendly_message(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke(db_path=db)
    assert result.exit_code == 0, result.output
    assert "no events" in result.output


# ---------------------------------------------------------------------------
# Invalid lens
# ---------------------------------------------------------------------------


def test_bottlenecks_invalid_lens_exits_2(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("--lens", "notlens", db_path=db)
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Seeded events
# ---------------------------------------------------------------------------


def test_bottlenecks_with_events_prints_per_activity_durations(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # deep_work 9:00 -> email 11:15 (duration for deep_work = 2h15m = 8100s)
    # email 11:15 -> meeting 12:00 (duration for email = 45m = 2700s)
    _seed_workday_events(
        db,
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T11:15:00Z"),
            ("meeting", "2026-01-05T12:00:00Z"),
        ],
    )

    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    assert "PERFORMANCE" in result.output
    assert "workday" in result.output
    assert "Per-activity" in result.output
    assert "deep_work" in result.output
    assert "avg=" in result.output
    assert "count=" in result.output


def test_bottlenecks_humanizes_duration(tmp_path: Path) -> None:
    """deep_work with 2h15m duration should appear as 2h15m in output."""
    db = _db(tmp_path)
    _seed_workday_events(
        db,
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T11:15:00Z"),  # 2h15m later
        ],
    )
    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    assert "2h15m" in result.output


def test_bottlenecks_orders_activities_by_avg_duration_desc(tmp_path: Path) -> None:
    """The activity with the longest average duration should appear first."""
    db = _db(tmp_path)
    # deep_work: 2h duration (7200s), email: 30m (1800s)
    _seed_workday_events(
        db,
        "2026-01-05",
        [
            ("email", "2026-01-05T08:00:00Z"),
            ("deep_work", "2026-01-05T08:30:00Z"),  # email->deep_work: 30m
            ("done", "2026-01-05T10:30:00Z"),  # deep_work->done: 2h
        ],
    )
    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    output = result.output
    # deep_work should appear before email in the output
    assert output.index("deep_work") < output.index("email")


def test_bottlenecks_case_count_in_header(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_workday_events(
        db,
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
        ],
    )
    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    assert "1 cases" in result.output
