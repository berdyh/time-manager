"""CLI tests for `tm debrief`.

All LLM calls are mocked via unittest.mock.Mock; no Anthropic API calls are
made by these tests.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from tm.cli import app
from tm.llm.client import ExtractResponse, Usage
from tm.repositories.events import EventsRepository
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

    with patch.object(debrief_cmd, "AnthropicAdapter", return_value=llm):
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

    with patch.object(debrief_cmd, "AnthropicAdapter", return_value=llm):
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

    with patch.object(debrief_cmd, "AnthropicAdapter", return_value=llm):
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
