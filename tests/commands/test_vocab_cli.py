"""CLI tests for `tm vocab` sub-commands.

All tests use typer's CliRunner to invoke commands against a temporary SQLite
database.  The ``--db-path`` option is passed explicitly so the real user
database is never touched.

typer 0.25.1 note: use plain CliRunner() (no mix_stderr) and result.output.
"""

from __future__ import annotations

import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.repositories.vocabulary import VocabularyRepository
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


def _seeded_db(tmp_path: Path) -> tuple[Path, VocabularyRepository]:
    """Return (db_path, repo) with 16 starter activities seeded."""
    db_path = _db(tmp_path)
    repo = VocabularyRepository(db_path)
    repo.seed_starter_vocabulary()
    repo.seed_starter_aliases()
    return db_path, repo


def _insert_event(
    db_path: Path,
    activity: str,
    *,
    ts: str = "2026-05-04T10:00:00Z",
) -> None:
    """Insert a minimal event row for the given activity.

    Default timestamp is recent (within 7 days of the test date 2026-05-05)
    so the default ``--since 7 days ago`` window picks it up.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "INSERT INTO events "
            "(event_id, case_id, activity, timestamp, lifecycle, extractor_version) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), "case-1", activity, ts, "start", "test/0.1"),
        )
        conn.commit()
    finally:
        conn.close()


def _days_ago(days: int) -> str:
    """Return a UTC event timestamp ``days`` days before now."""
    dt = datetime.now(UTC).replace(microsecond=0) - timedelta(days=days)
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def _invoke_review(*args: str, db_path: Path, input: str | None = None) -> object:
    invoke_args = ["vocab", "review", "--db-path", str(db_path), *args]
    return runner.invoke(app, invoke_args, input=input)


def _invoke_list(*args: str, db_path: Path) -> object:
    invoke_args = ["vocab", "list", "--db-path", str(db_path), *args]
    return runner.invoke(app, invoke_args)


def _invoke_drift(*args: str, db_path: Path) -> object:
    invoke_args = ["vocab", "drift", "--db-path", str(db_path), *args]
    return runner.invoke(app, invoke_args)


# ---------------------------------------------------------------------------
# review: no novel labels
# ---------------------------------------------------------------------------


def test_review_no_novel_labels_prints_nothing_to_review(tmp_path: Path) -> None:
    # No events at all → "no novel labels in window"
    db_path, _repo = _seeded_db(tmp_path)
    result = _invoke_review(db_path=db_path)
    assert result.exit_code == 0, result.output
    assert "no novel labels" in result.output


# ---------------------------------------------------------------------------
# review: list novel labels with counts
# ---------------------------------------------------------------------------


def test_review_lists_novel_labels_with_counts(tmp_path: Path) -> None:
    db_path, _repo = _seeded_db(tmp_path)
    # Insert 3 events with unknown activity "yoga"
    for _ in range(3):
        _insert_event(db_path, "yoga")
    # Insert 1 event with another unknown activity "tai_chi"
    _insert_event(db_path, "tai_chi")
    # Insert 1 event with known canonical "exercise" (should NOT appear)
    _insert_event(db_path, "exercise")

    # Ignore both novel labels so we see each one; yoga has highest count
    result = _invoke_review("--limit", "5", db_path=db_path, input="i\ni\n")
    assert result.exit_code == 0, result.output
    assert "yoga" in result.output
    assert "n=3" in result.output
    assert "tai_chi" in result.output
    # Known canonical should NOT appear as a novel label
    assert "exercise (n=" not in result.output


def test_review_limit_respected(tmp_path: Path) -> None:
    db_path, _repo = _seeded_db(tmp_path)
    # Insert events with many distinct unknown activities
    for i in range(5):
        _insert_event(db_path, f"unknown_activity_{i}")

    # Limit to 2 — skip all
    result = _invoke_review("--limit", "2", db_path=db_path, input="i\ni\n")
    assert result.exit_code == 0, result.output
    # Summary should show at most 2 processed
    assert "2 skipped" in result.output


# ---------------------------------------------------------------------------
# review: skip all exits cleanly
# ---------------------------------------------------------------------------


def test_review_skip_all_exits_cleanly(tmp_path: Path) -> None:
    db_path, _repo = _seeded_db(tmp_path)
    _insert_event(db_path, "yoga")

    result = _invoke_review(db_path=db_path, input="s\n")
    assert result.exit_code == 0, result.output
    assert "summary:" in result.output


# ---------------------------------------------------------------------------
# review: merge creates alias
# ---------------------------------------------------------------------------


def test_review_merge_creates_alias(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    _insert_event(db_path, "gym_session")

    # input: choose 'm', then enter canonical 'exercise'
    result = _invoke_review(db_path=db_path, input="m\nexercise\n")
    assert result.exit_code == 0, result.output
    assert "added alias" in result.output
    assert "gym_session" in result.output
    assert "exercise" in result.output

    # Verify alias was actually persisted
    assert repo.resolve("gym_session") == "exercise"


# ---------------------------------------------------------------------------
# review: merge invalid canonical re-prompts
# ---------------------------------------------------------------------------


def test_review_merge_invalid_canonical_reprompts(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    _insert_event(db_path, "gym_session")

    # First try bogus canonical, then provide valid one
    result = _invoke_review(db_path=db_path, input="m\nbogus_nonexistent\nexercise\n")
    assert result.exit_code == 0, result.output
    # Should have reprompted and eventually accepted 'exercise'
    assert "not a known canonical" in result.output
    assert repo.resolve("gym_session") == "exercise"


# ---------------------------------------------------------------------------
# review: create canonical persists new entry
# ---------------------------------------------------------------------------


def test_review_create_canonical_persists_new_entry(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    _insert_event(db_path, "yoga")

    # input: choose 'c', accept suggested canonical name 'yoga'
    result = _invoke_review(db_path=db_path, input="c\nyoga\n")
    assert result.exit_code == 0, result.output
    assert "created canonical" in result.output

    # Verify canonical was persisted
    active_names = {e.activity_name for e in repo.list_active()}
    assert "yoga" in active_names

    # Resolve should work directly (canonical lookup)
    assert repo.resolve("yoga") == "yoga"


# ---------------------------------------------------------------------------
# review: ignore does not persist
# ---------------------------------------------------------------------------


def test_review_ignore_does_not_persist(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    _insert_event(db_path, "yoga")

    initial_active = {e.activity_name for e in repo.list_active()}
    initial_count = len(initial_active)

    # 'i' = ignore, then 's' to skip rest
    result = _invoke_review(db_path=db_path, input="i\n")
    assert result.exit_code == 0, result.output
    assert "skipped" in result.output

    # No new aliases or canonicals
    after_active = {e.activity_name for e in repo.list_active()}
    assert len(after_active) == initial_count
    assert repo.resolve("yoga") is None


# ---------------------------------------------------------------------------
# vocab list: shows starter vocabulary
# ---------------------------------------------------------------------------


def test_vocab_list_shows_starter_vocabulary(tmp_path: Path) -> None:
    db_path, _repo = _seeded_db(tmp_path)

    result = _invoke_list(db_path=db_path)
    assert result.exit_code == 0, result.output

    # All 16 starter activities should appear
    for name in (
        "deep_work",
        "shallow_work",
        "meeting",
        "email",
        "code_review",
        "exercise",
        "meal",
        "commute",
        "errand",
        "family_time",
        "social",
        "solo_recreation",
        "learning",
        "rest",
        "sleep",
        "interruption",
    ):
        assert name in result.output, f"missing starter activity: {name!r}"


# ---------------------------------------------------------------------------
# vocab list: include-archived shows archived
# ---------------------------------------------------------------------------


def test_vocab_list_include_archived_shows_archived(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    repo.archive("sleep")

    # Without flag: 'sleep' should be absent
    result_default = _invoke_list(db_path=db_path)
    assert result_default.exit_code == 0, result_default.output
    # 'sleep' may appear in output as a substring of another word; check
    # carefully that it's not listed as an entry
    lines = result_default.output.splitlines()
    entry_lines = [line for line in lines if line.startswith("sleep")]
    assert not entry_lines, (
        f"archived 'sleep' should not appear in default list, got: {entry_lines}"
    )

    # With flag: 'sleep' should appear with 'archived' status
    result_archived = _invoke_list("--include-archived", db_path=db_path)
    assert result_archived.exit_code == 0, result_archived.output
    assert "sleep" in result_archived.output
    assert "archived" in result_archived.output


# ---------------------------------------------------------------------------
# review: merge abort and create-failure accountability
# ---------------------------------------------------------------------------


def test_review_merge_empty_canonical_returns_to_menu(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    _insert_event(db_path, "gym_session")

    result = _invoke_review(db_path=db_path, input="m\n\ni\n")

    assert result.exit_code == 0, result.output
    assert "merge cancelled" in result.output
    assert "1 skipped" in result.output
    assert repo.resolve("gym_session") is None


def test_review_create_canonical_failure_counts_as_skipped(tmp_path: Path) -> None:
    db_path, repo = _seeded_db(tmp_path)
    _insert_event(db_path, "deep work")

    result = _invoke_review(db_path=db_path, input="c\n\n")

    assert result.exit_code == 0, result.output
    assert "activity already exists: deep_work" in result.output
    assert "1 skipped" in result.output
    assert repo.resolve("deep work") is None


# ---------------------------------------------------------------------------
# vocab list: auto-seeds empty databases
# ---------------------------------------------------------------------------


def test_vocab_list_auto_seeds_empty_database(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    result = _invoke_list(db_path=db_path)

    assert result.exit_code == 0, result.output
    assert "no vocabulary entries" not in result.output
    assert "deep_work" in result.output
    assert "interruption" in result.output

    repo = VocabularyRepository(db_path)
    assert repo.resolve("workout") == "exercise"


# ---------------------------------------------------------------------------
# vocab drift
# ---------------------------------------------------------------------------


def test_drift_command_with_drifted_activities(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    repo = VocabularyRepository(db_path)
    for activity in ("active_now", "bug_triage", "deep_work", "planning_meeting"):
        repo.add_canonical(activity)

    bug_ts = _days_ago(21)
    deep_ts = _days_ago(30)
    _insert_event(db_path, "active_now", ts=_days_ago(2))
    _insert_event(db_path, "bug_triage", ts=bug_ts)
    _insert_event(db_path, "deep_work", ts=deep_ts)

    result = _invoke_drift(db_path=db_path)

    assert result.exit_code == 0, result.output
    assert "Vocabulary drift report" in result.output
    assert "Idle window: 14 days" in result.output
    assert "active_now" not in result.output
    assert "bug_triage" in result.output
    assert f"last seen: {bug_ts[:10]}" in result.output
    assert "deep_work" in result.output
    assert f"last seen: {deep_ts[:10]}" in result.output
    assert "planning_meeting" in result.output
    assert "last seen: never" in result.output
    assert "Novelty rate over [all..now]: 0.000" in result.output


def test_drift_command_no_drift(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    repo = VocabularyRepository(db_path)
    for activity in ("deep_work", "meeting"):
        repo.add_canonical(activity)
        _insert_event(db_path, activity, ts=_days_ago(2))

    result = _invoke_drift(db_path=db_path)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "Vocabulary drift report: clean (no drift, novelty rate 0.000)"
    )


def test_drift_command_empty_db(tmp_path: Path) -> None:
    db_path = _db(tmp_path)

    result = _invoke_drift(db_path=db_path)

    assert result.exit_code == 0, result.output
    assert result.output.strip() == (
        "Vocabulary drift report: clean (no drift, novelty rate 0.000)"
    )


def test_drift_command_idle_days_param(tmp_path: Path) -> None:
    db_path = _db(tmp_path)
    repo = VocabularyRepository(db_path)
    for activity in ("focus_block", "planning"):
        repo.add_canonical(activity)

    _insert_event(db_path, "focus_block", ts=_days_ago(10))
    _insert_event(db_path, "planning", ts=_days_ago(2))

    default_result = _invoke_drift(db_path=db_path)
    short_window_result = _invoke_drift("--idle-days", "7", db_path=db_path)

    assert default_result.exit_code == 0, default_result.output
    assert default_result.output.strip() == (
        "Vocabulary drift report: clean (no drift, novelty rate 0.000)"
    )
    assert short_window_result.exit_code == 0, short_window_result.output
    assert "Idle window: 7 days" in short_window_result.output
    assert "focus_block" in short_window_result.output
    assert "planning" not in short_window_result.output


def test_drift_command_no_llm_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    db_path = _db(tmp_path)
    repo = VocabularyRepository(db_path)
    repo.add_canonical("deep_work")
    _insert_event(db_path, "deep_work", ts=_days_ago(1))

    result = _invoke_drift(db_path=db_path)

    assert result.exit_code == 0, result.output
    assert "clean" in result.output
