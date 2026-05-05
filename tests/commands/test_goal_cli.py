"""CLI tests for `tm goal` sub-commands.

All tests use typer's CliRunner to invoke commands against a temporary SQLite
database.  The ``--db-path`` option is passed explicitly so the real user
database is never touched.
"""

from __future__ import annotations

from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.repositories.goals import GoalsRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _db(tmp_path: Path) -> Path:
    """Return a path to a fresh, migrated database inside tmp_path."""
    db_path = tmp_path / "tm_test.db"
    store = SQLiteStore(db_path)
    store.apply_pending_migrations()
    store.close()
    return db_path


def _invoke(*args: str, db_path: Path) -> object:
    """Invoke the app with ``--db-path`` automatically injected."""
    return runner.invoke(app, ["goal", *args, "--db-path", str(db_path)])


# ---------------------------------------------------------------------------
# add
# ---------------------------------------------------------------------------


def test_add_creates_goal_and_prints_id(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("add", "My first goal", db_path=db)
    assert result.exit_code == 0, result.output
    assert "created goal " in result.output

    # Verify the goal is actually stored
    repo = GoalsRepository(db)
    goals = repo.list(status="all")
    assert len(goals) == 1
    assert goals[0].name == "My first goal"


def test_add_with_description_and_priority_persists(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke(
        "add",
        "Fitness goal",
        "--description",
        "Run 5k every day",
        "--priority",
        "1",
        db_path=db,
    )
    assert result.exit_code == 0, result.output

    repo = GoalsRepository(db)
    goals = repo.list(status="all")
    assert len(goals) == 1
    g = goals[0]
    assert g.name == "Fitness goal"
    assert g.description == "Run 5k every day"
    assert g.priority == 1


def test_add_with_target_date_parses_iso(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("add", "Deadline goal", "--target", "2026-12-31", db_path=db)
    assert result.exit_code == 0, result.output

    repo = GoalsRepository(db)
    goals = repo.list(status="all")
    assert len(goals) == 1
    assert goals[0].target_completion_at is not None
    assert "2026-12-31" in goals[0].target_completion_at


def test_add_invalid_priority_exits_nonzero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # priority must be 1-3; 5 is invalid
    result = _invoke("add", "Bad priority goal", "--priority", "5", db_path=db)
    assert result.exit_code != 0
    # stderr should contain an error message
    assert "error" in result.output.lower()


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_empty_prints_no_goals(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("list", db_path=db)
    assert result.exit_code == 0, result.output
    assert "no goals" in result.output


def test_list_active_only_default(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Add 2 active goals
    _invoke("add", "Active goal A", db_path=db)
    _invoke("add", "Active goal B", db_path=db)

    # Add 1 and immediately complete it
    repo = GoalsRepository(db)
    repo.add(name="Completed goal")
    goals = repo.list(status="all")
    completed_goal = next(g for g in goals if g.name == "Completed goal")
    repo.complete(completed_goal.goal_id)

    # List with default (active) filter
    result = _invoke("list", db_path=db)
    assert result.exit_code == 0, result.output
    assert "Active goal A" in result.output
    assert "Active goal B" in result.output
    assert "Completed goal" not in result.output


def test_list_status_all_shows_all(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Add one active
    _invoke("add", "Still active", db_path=db)

    # Add one completed
    repo = GoalsRepository(db)
    g = repo.add(name="Already done")
    repo.complete(g.goal_id)

    result = _invoke("list", "--status", "all", db_path=db)
    assert result.exit_code == 0, result.output
    assert "Still active" in result.output
    assert "Already done" in result.output


# ---------------------------------------------------------------------------
# complete
# ---------------------------------------------------------------------------


def test_complete_marks_goal(tmp_path: Path) -> None:
    db = _db(tmp_path)
    # Create a goal via the repo
    repo = GoalsRepository(db)
    g = repo.add(name="Goal to complete")

    result = _invoke("complete", g.goal_id, db_path=db)
    assert result.exit_code == 0, result.output
    assert f"completed {g.goal_id}" in result.output

    # Verify via list --status completed
    list_result = _invoke("list", "--status", "completed", db_path=db)
    assert "Goal to complete" in list_result.output


def test_complete_unknown_id_exits_nonzero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("complete", "00000000000000000000000000", db_path=db)
    assert result.exit_code == 1
    assert "error" in result.output.lower()


def test_complete_already_terminal_exits_nonzero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = GoalsRepository(db)
    g = repo.add(name="Goal to complete twice")
    repo.complete(g.goal_id)

    result = _invoke("complete", g.goal_id, db_path=db)
    assert result.exit_code == 1
    assert "error" in result.output.lower()


# ---------------------------------------------------------------------------
# abandon
# ---------------------------------------------------------------------------


def test_abandon_with_reason_persists(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = GoalsRepository(db)
    g = repo.add(name="Goal to abandon")

    result = _invoke(
        "abandon", g.goal_id, "--reason", "Not relevant anymore", db_path=db
    )
    assert result.exit_code == 0, result.output
    assert f"abandoned {g.goal_id}" in result.output

    # Verify via show
    show_result = _invoke("show", g.goal_id, db_path=db)
    assert "Not relevant anymore" in show_result.output


# ---------------------------------------------------------------------------
# show
# ---------------------------------------------------------------------------


def test_show_unknown_id_exits_nonzero(tmp_path: Path) -> None:
    db = _db(tmp_path)
    result = _invoke("show", "00000000000000000000000000", db_path=db)
    assert result.exit_code == 1
    assert "error" in result.output.lower()
    assert "unknown goal" in result.output.lower()


def test_show_renders_all_fields(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = GoalsRepository(db)
    g = repo.add(
        name="Full field goal",
        description="Detailed description here",
        priority=2,
    )

    result = _invoke("show", g.goal_id, db_path=db)
    assert result.exit_code == 0, result.output

    output = result.output
    assert g.goal_id in output
    assert "Full field goal" in output
    assert "active" in output
    assert "Detailed description here" in output
    assert "2" in output  # priority
    # All field labels present
    for label in (
        "ID:",
        "NAME:",
        "STATUS:",
        "DESCRIPTION:",
        "PRIORITY:",
        "CREATED_AT:",
        "TARGET:",
        "COMPLETED_AT:",
        "ABANDONED_AT:",
    ):
        assert label in output
