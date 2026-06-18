"""CLI tests for `tm debrief`.

All LLM calls are mocked via unittest.mock.Mock; no Anthropic API calls are
made by these tests.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from typer.testing import CliRunner

from tm.cli import app
from tm.llm.client import ExtractResponse, Usage
from tm.repositories.events import EventsRepository
from tm.repositories.transcripts import TranscriptRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tm_test.db"
    store = SQLiteStore(db_path)
    store.apply_pending_migrations()
    store.close()
    vocab_repo = VocabularyRepository(db_path)
    vocab_repo.seed_starter_vocabulary()
    vocab_repo.seed_starter_aliases()
    return db_path


def _debrief_extract_response(
    *,
    activity: str = "deep_work",
    completed: int = 8,
    total: int = 10,
) -> ExtractResponse:
    return ExtractResponse(
        data={
            "events": [
                {
                    "activity": activity,
                    "timestamp": "2026-05-06T09:00:00Z",
                    "lifecycle": "complete",
                    "advances_goal_id": None,
                    "resource": None,
                    "duration_minutes": 90,
                }
            ],
            "summary": {
                "planned_tasks_completed": completed,
                "planned_tasks_total": total,
            },
        },
        usage=Usage(input_tokens=120, output_tokens=40),
    )


def _mock_llm(response: ExtractResponse | None = None) -> Mock:
    llm = Mock()
    llm.extract.return_value = response or _debrief_extract_response()
    return llm


def _invoke_debrief(*args: str, db_path: Path, input_text: str | None = None):
    return runner.invoke(
        app,
        ["debrief", *args, "--db-path", str(db_path)],
        input=input_text,
    )


def test_debrief_cli_with_transcript_file(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("I completed a deep work block.", encoding="utf-8")
    llm = _mock_llm()

    from tm.commands import debrief as debrief_cmd

    with patch.object(debrief_cmd, "build_llm_client", return_value=llm):
        result = _invoke_debrief(
            "--case-date",
            "2026-05-06",
            "--transcript-file",
            str(transcript),
            db_path=db,
        )

    assert result.exit_code == 0, result.output
    assert "Debrief complete: case_date=2026-05-06" in result.output
    events = EventsRepository(db).query_events(case_date="2026-05-06")
    assert len(events) == 2
    assert {event["activity"] for event in events} == {"deep_work", "debrief_summary"}


def test_debrief_cli_with_stdin(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    llm = _mock_llm()

    from tm.commands import debrief as debrief_cmd

    with patch.object(debrief_cmd, "build_llm_client", return_value=llm):
        result = _invoke_debrief(
            "--case-date",
            "2026-05-06",
            "--from-stdin",
            db_path=db,
            input_text="I finished deep work.\n",
        )

    assert result.exit_code == 0, result.output
    assert "Events persisted: 2" in result.output
    assert len(EventsRepository(db).query_events(case_date="2026-05-06")) == 2


def test_debrief_cli_missing_input_source(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)

    result = _invoke_debrief("--case-date", "2026-05-06", db_path=db)

    assert result.exit_code == 2, result.output
    assert "exactly one input source" in result.output.lower()


def test_debrief_cli_both_input_sources(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("I completed a deep work block.", encoding="utf-8")

    result = _invoke_debrief(
        "--case-date",
        "2026-05-06",
        "--transcript-file",
        str(transcript),
        "--from-stdin",
        db_path=db,
        input_text="stdin transcript",
    )

    assert result.exit_code == 2, result.output
    assert "exactly one input source" in result.output.lower()


def test_debrief_cli_missing_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    db = _db(tmp_path)
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("I completed a deep work block.", encoding="utf-8")

    result = _invoke_debrief(
        "--case-date",
        "2026-05-06",
        "--transcript-file",
        str(transcript),
        db_path=db,
    )

    assert result.exit_code == 1, result.output
    assert "TM_LLM_API_KEY" in result.output


def test_debrief_cli_renders_summary(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("I cooked dinner and wrapped the plan.", encoding="utf-8")
    llm = _mock_llm(_debrief_extract_response(activity="cooking"))
    llm.extract.side_effect = [
        _debrief_extract_response(activity="cooking"),
        {
            "canonical": None,
            "confidence": 0.4,
            "is_novel": True,
            "reason": "not in starter vocabulary",
        },
    ]

    from tm.commands import debrief as debrief_cmd

    with patch.object(debrief_cmd, "build_llm_client", return_value=llm):
        result = _invoke_debrief(
            "--case-date",
            "2026-05-06",
            "--transcript-file",
            str(transcript),
            db_path=db,
        )

    assert result.exit_code == 0, result.output
    assert "Events persisted: 2" in result.output
    assert "Novel labels: ['cooking']" in result.output
    assert "Summary: {planned_tasks_completed: 8, planned_tasks_total: 10}" in (
        result.output
    )
    assert "Cost (estimated): $" in result.output
    assert "Cost (actual): $" in result.output


# ---------------------------------------------------------------------------
# T-PM-DEBRIEF-UNIQUE: friendly CLI rendering on race-induced duplicate
# ---------------------------------------------------------------------------


def test_debrief_cli_renders_friendly_message_on_duplicate_summary(
    tmp_path: Path, monkeypatch
) -> None:
    """The CLI must convert DuplicateSummaryError into a friendly exit-1 message.

    DuplicateSummaryError fires when migration 0010's partial UNIQUE index
    rejects a race-induced second debrief_summary INSERT for the same
    case_date (post-/simplify the daemon's coarse write lock no longer
    serialises LLM-backed handlers, so two concurrent ``run_debrief``
    invocations can both pass the pre-call SELECT and collide at INSERT).

    Operator-facing UX: print "Debrief skipped: a summary already exists
    for case_date=YYYY-MM-DD..." and exit 1 — not a traceback, not exit 0.
    """
    from tm.agents.debrief import DuplicateSummaryError
    from tm.commands import debrief as debrief_cmd

    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    transcript = tmp_path / "transcript.txt"
    transcript.write_text("I finished deep work.", encoding="utf-8")
    TranscriptRepository(db).upsert(
        case_date="2026-05-15",
        transcript_text="old transcript",
        source="debrief",
        extractor_version="debrief-v1",
    )

    raising_agent = Mock()
    raising_agent.extract_and_persist.side_effect = DuplicateSummaryError(
        "2026-05-15",
        detail="UNIQUE constraint failed: events.case_date",
    )

    with (
        patch.object(debrief_cmd, "build_llm_client", return_value=Mock()),
        patch.object(debrief_cmd, "DebriefAgent", return_value=raising_agent),
    ):
        result = _invoke_debrief(
            "--case-date",
            "2026-05-15",
            "--transcript-file",
            str(transcript),
            db_path=db,
        )

    assert result.exit_code == 1, result.output
    # Friendly message mentions the case_date so the operator can act on it.
    assert "Debrief skipped" in result.output
    assert "case_date=2026-05-15" in result.output
    # And does NOT leak a raw traceback / class name.
    assert "Traceback" not in result.output
    assert "DuplicateSummaryError" not in result.output
    retained = TranscriptRepository(db).get("2026-05-15")
    assert retained is not None
    assert retained.transcript_text == "old transcript"


def test_reextract_preserves_existing_debrief_events_when_agent_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    EventsRepository(db).append_event(
        event_id="old-debrief-event",
        case_id="2026-06-05",
        activity="deep_work",
        timestamp="2026-06-05T09:00:00Z",
        lifecycle="complete",
        resource=None,
        attributes={"duration_minutes": 60},
        extractor_version="debrief-v1",
        case_date="2026-06-05",
    )
    TranscriptRepository(db).upsert(
        case_date="2026-06-05",
        transcript_text="old transcript",
        source="debrief",
        extractor_version="debrief-v1",
    )
    new_transcript = tmp_path / "new-transcript.txt"
    new_transcript.write_text("new transcript", encoding="utf-8")

    from tm.commands import reextract as reextract_cmd

    class FailingAgent:
        def extract_and_persist(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("llm failed")

    with patch.object(
        reextract_cmd,
        "build_debrief_agent",
        return_value=FailingAgent(),
    ):
        result = runner.invoke(
            app,
            [
                "reextract",
                "--case-date",
                "2026-06-05",
                "--transcript-file",
                str(new_transcript),
                "--db-path",
                str(db),
            ],
        )

    assert result.exit_code == 1
    events = EventsRepository(db).query_events(case_date="2026-06-05")
    assert [event["event_id"] for event in events] == ["old-debrief-event"]
    transcript = TranscriptRepository(db).get("2026-06-05")
    assert transcript is not None
    assert transcript.transcript_text == "old transcript"


def test_reextract_records_cost_when_production_replacement_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    TranscriptRepository(db).upsert(
        case_date="2026-06-05",
        transcript_text="old transcript",
        source="debrief",
        extractor_version="debrief-v1",
    )

    from tm.commands import reextract as reextract_cmd
    from tm.security import connect_sqlite

    class CostRecordingAgent:
        def __init__(self, db_path: Path) -> None:
            self._db_path = db_path

        def extract_and_persist(self, *args: object, **kwargs: object) -> Mock:
            conn = connect_sqlite(self._db_path)
            try:
                conn.execute(
                    "INSERT INTO cost_ledger "
                    "(ts, model, input_tokens, output_tokens, est_cost_usd, "
                    "request_kind) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        "2026-06-05T12:00:00Z",
                        "claude-test",
                        10,
                        5,
                        0.01,
                        "extract",
                    ),
                )
                conn.commit()
            finally:
                conn.close()
            return Mock()

    def _build_agent(**kwargs: object) -> CostRecordingAgent:
        return CostRecordingAgent(kwargs["db_path"])  # type: ignore[arg-type]

    with (
        patch.object(reextract_cmd, "build_debrief_agent", side_effect=_build_agent),
        patch.object(
            reextract_cmd,
            "_replace_debrief_events",
            side_effect=RuntimeError("replacement failed"),
        ),
    ):
        result = runner.invoke(
            app,
            [
                "reextract",
                "--case-date",
                "2026-06-05",
                "--db-path",
                str(db),
            ],
        )

    assert result.exit_code == 1
    with sqlite3.connect(db) as conn:
        rows = conn.execute(
            "SELECT model, input_tokens, output_tokens, est_cost_usd, request_kind "
            "FROM cost_ledger"
        ).fetchall()
    assert rows == [("claude-test", 10, 5, 0.01, "extract")]


def test_reextract_replacement_aborts_when_privacy_fence_changes(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    EventsRepository(db).append_event(
        event_id="old-debrief-event",
        case_id="2026-06-05",
        activity="deep_work",
        timestamp="2026-06-05T09:00:00Z",
        lifecycle="complete",
        resource=None,
        attributes={"duration_minutes": 60},
        extractor_version="debrief-v1",
        case_date="2026-06-05",
    )

    from tm.commands import reextract as reextract_cmd

    privacy_fence = reextract_cmd._privacy_action_count(db)
    with sqlite3.connect(db) as conn:
        conn.execute(
            "INSERT INTO privacy_actions "
            "(action_id, action_type, selector, affected_events, affected_transcripts) "
            "VALUES (?, ?, ?, ?, ?)",
            ("privacy-1", "forget", "case_date=2026-06-05", 1, 0),
        )

    with pytest.raises(reextract_cmd.PrivacyFenceChangedError):
        reextract_cmd._replace_debrief_events(
            db,
            case_date="2026-06-05",
            events=[],
            cost_rows=[],
            privacy_action_count=privacy_fence,
        )

    events = EventsRepository(db).query_events(case_date="2026-06-05")
    assert [event["event_id"] for event in events] == ["old-debrief-event"]
