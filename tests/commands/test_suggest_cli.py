"""CLI tests for `tm suggest`.

All LLM calls are mocked via unittest.mock.Mock; no Anthropic API calls are
made by these tests.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import Mock, patch

from typer.testing import CliRunner

from tm.cli import app
from tm.llm.client import ExtractResponse, Usage
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tm_test.db"
    store = SQLiteStore(db_path)
    store.apply_pending_migrations()
    store.close()
    return db_path


def _seed_partial_trace(db_path: Path, case_date: str) -> None:
    repo = EventsRepository(db_path)
    repo.append_event(
        event_id=str(uuid.uuid4()),
        case_id=case_date,
        activity="email",
        timestamp=f"{case_date}T09:00:00Z",
        lifecycle="complete",
        case_date=case_date,
    )
    repo.append_event(
        event_id=str(uuid.uuid4()),
        case_id=case_date,
        activity="meeting",
        timestamp=f"{case_date}T10:00:00Z",
        lifecycle="complete",
        case_date=case_date,
    )


def _suggest_extract_response() -> ExtractResponse:
    return ExtractResponse(
        data={
            "recommended_action": "Take a 15min break before diving into task X",
            "predicted_outcome_with": 1.4,
            "predicted_outcome_without": 0.9,
            "predicted_post_suggestion_fitness": 0.88,
            "explanation": "A short reset usually precedes better deep work.",
        },
        usage=Usage(input_tokens=140, output_tokens=45),
    )


def _mock_llm() -> Mock:
    llm = Mock()
    llm.extract.return_value = _suggest_extract_response()
    return llm


def _invoke_suggest(*args: str, db_path: Path):
    return runner.invoke(app, ["suggest", *args, "--db-path", str(db_path)])


def test_suggest_cli_returns_suggestion(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    _seed_partial_trace(db, "2026-05-06")
    llm = _mock_llm()

    from tm.commands import suggest as suggest_cmd

    with patch.object(suggest_cmd, "AnthropicAdapter", return_value=llm):
        result = _invoke_suggest("--case-date", "2026-05-06", db_path=db)

    assert result.exit_code == 0, result.output
    assert "Suggestion ready (suggestion_id=" in result.output
    assert "Recommended action: Take a 15min break" in result.output
    assert "Predicted outcome WITH:    1.40" in result.output
    assert "Predicted outcome WITHOUT: 0.90" in result.output
    assert "Predicted delta:           +0.50" in result.output
    assert "Conformance deviation:     0.12" in result.output
    assert "LLM rationale: A short reset" in result.output


def test_suggest_cli_returns_skip_reason(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    llm = _mock_llm()

    from tm.commands import suggest as suggest_cmd

    with patch.object(suggest_cmd, "AnthropicAdapter", return_value=llm):
        result = _invoke_suggest("--case-date", "2026-05-06", db_path=db)

    assert result.exit_code == 0, result.output
    assert "Suggestion skipped: empty_context" in result.output
    assert "Detail:" in result.output


def test_suggest_cli_missing_api_key(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    db = _db(tmp_path)
    _seed_partial_trace(db, "2026-05-06")

    result = _invoke_suggest("--case-date", "2026-05-06", db_path=db)

    assert result.exit_code == 1, result.output
    assert "TM_LLM_API_KEY" in result.output


def test_suggest_cli_with_goal_id(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("TM_LLM_API_KEY", "test-key")
    db = _db(tmp_path)
    _seed_partial_trace(db, "2026-05-06")
    goal = GoalsRepository(db).add(name="Ship CLI")
    llm = _mock_llm()

    from tm.commands import suggest as suggest_cmd

    with patch.object(suggest_cmd, "AnthropicAdapter", return_value=llm):
        result = _invoke_suggest(
            "--case-date",
            "2026-05-06",
            "--case-goal-id",
            goal.goal_id,
            db_path=db,
        )

    assert result.exit_code == 0, result.output
    rows = [
        row
        for row in SuggestionTelemetryRepository(db).list_recent()
        if row.case_date == "2026-05-06"
    ]
    assert len(rows) == 1
    assert rows[0].case_goal_id == goal.goal_id
