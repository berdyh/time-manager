"""CLI tests for `tm variants`.

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
    return runner.invoke(app, ["variants", *args, "--db-path", str(db_path)])


def _seed_case(
    db_path: Path,
    case_date: str,
    activities_and_times: list[tuple[str, str]],
) -> None:
    repo = EventsRepository(db_path)
    for activity, ts in activities_and_times:
        repo.append_event(
            event_id=str(uuid.uuid4()),
            case_id=f"case-{case_date}-{uuid.uuid4().hex[:6]}",
            activity=activity,
            timestamp=ts,
            lifecycle="complete",
            case_date=case_date,
        )


# ---------------------------------------------------------------------------
# Help / smoke tests
# ---------------------------------------------------------------------------


def test_variants_help() -> None:
    result = runner.invoke(app, ["variants", "--help"])
    assert result.exit_code == 0, result.output
    assert "--lens" in result.output
    assert "--since" in result.output
    assert "--until" in result.output
    assert "--top-n" in result.output
    assert "--trend" in result.output
    assert "--db-path" in result.output


# ---------------------------------------------------------------------------
# Empty log
# ---------------------------------------------------------------------------


def test_variants_empty_log(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke(db_path=db)
    assert result.exit_code == 0, result.output
    assert "no events" in result.output


# ---------------------------------------------------------------------------
# Invalid lens
# ---------------------------------------------------------------------------


def test_variants_invalid_lens_exits_2(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("--lens", "bogus", db_path=db)
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Seeded events
# ---------------------------------------------------------------------------


def test_variants_with_seeded_events_lists_distinct_sequences(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Variant A: deep_work -> email (2 cases on different dates)
    _seed_case(
        db,
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
        ],
    )
    _seed_case(
        db,
        "2026-01-06",
        [
            ("deep_work", "2026-01-06T09:00:00Z"),
            ("email", "2026-01-06T10:00:00Z"),
        ],
    )
    # Variant B: meeting -> review (1 case)
    _seed_case(
        db,
        "2026-01-07",
        [
            ("meeting", "2026-01-07T09:00:00Z"),
            ("review", "2026-01-07T10:00:00Z"),
        ],
    )

    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    output = result.output
    assert "VARIANTS" in output
    assert "workday" in output
    assert "3 cases" in output
    assert "2 distinct" in output
    assert "deep_work -> email" in output
    assert "meeting -> review" in output


def test_variants_orders_by_frequency_desc(tmp_path: Path) -> None:
    """The variant with more cases should appear first (#1)."""
    db = _db(tmp_path)
    # Variant A: deep_work -> email (appears twice)
    _seed_case(
        db,
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
        ],
    )
    _seed_case(
        db,
        "2026-01-06",
        [
            ("deep_work", "2026-01-06T09:00:00Z"),
            ("email", "2026-01-06T10:00:00Z"),
        ],
    )
    # Variant B: meeting (appears once)
    _seed_case(
        db,
        "2026-01-07",
        [
            ("meeting", "2026-01-07T09:00:00Z"),
        ],
    )

    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    output = result.output
    # #1 should be deep_work -> email (higher frequency)
    lines = [line for line in output.splitlines() if line.strip().startswith("#")]
    assert len(lines) >= 2
    assert "deep_work -> email" in lines[0]
    assert "meeting" in lines[1]


def test_variants_top_n_truncation(tmp_path: Path) -> None:
    """--top-n 1 should return only 1 variant."""
    db = _db(tmp_path)
    _seed_case(
        db,
        "2026-01-05",
        [
            ("deep_work", "2026-01-05T09:00:00Z"),
            ("email", "2026-01-05T10:00:00Z"),
        ],
    )
    _seed_case(
        db,
        "2026-01-06",
        [
            ("meeting", "2026-01-06T09:00:00Z"),
            ("review", "2026-01-06T10:00:00Z"),
        ],
    )
    _seed_case(
        db,
        "2026-01-07",
        [
            ("planning", "2026-01-07T09:00:00Z"),
        ],
    )

    result = _invoke("--lens", "workday", "--top-n", "1", db_path=db)
    assert result.exit_code == 0, result.output
    output = result.output
    # Only one #N line
    rank_lines = [line for line in output.splitlines() if line.strip().startswith("#")]
    assert len(rank_lines) == 1
    assert "#1" in rank_lines[0]


def test_variants_shows_case_count_per_variant(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Same variant on 3 different days = 3 cases for one variant
    for date in ("2026-01-05", "2026-01-06", "2026-01-07"):
        _seed_case(
            db,
            date,
            [
                ("deep_work", f"{date}T09:00:00Z"),
                ("email", f"{date}T10:00:00Z"),
            ],
        )

    result = _invoke("--lens", "workday", db_path=db)
    assert result.exit_code == 0, result.output
    assert "cases=3" in result.output


def test_variants_trend_compares_previous_equal_window(tmp_path: Path) -> None:
    db = _db(tmp_path)
    _seed_case(
        db,
        "2026-01-01",
        [("email", "2026-01-01T09:00:00Z")],
    )
    _seed_case(
        db,
        "2026-01-08",
        [("email", "2026-01-08T09:00:00Z")],
    )
    _seed_case(
        db,
        "2026-01-09",
        [("email", "2026-01-09T09:00:00Z")],
    )

    result = _invoke(
        "--lens",
        "workday",
        "--since",
        "2026-01-08",
        "--until",
        "2026-01-15",
        "--trend",
        db_path=db,
    )

    assert result.exit_code == 0, result.output
    assert "trend=rising" in result.output
    assert "prev_cases=1" in result.output
