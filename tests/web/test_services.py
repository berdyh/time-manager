"""Tests for local web UI service helpers."""

from __future__ import annotations

from pathlib import Path

from tm.local_agents import save_selected_agent
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository
from tm.stores.sqlite_store import SQLiteStore
from tm.web.services import (
    build_capabilities,
    build_dashboard,
    build_now,
    selected_agent_params,
)


def _prepare_db(path: Path) -> None:
    store = SQLiteStore(path)
    try:
        store.apply_pending_migrations()
    finally:
        store.close()


def test_dashboard_and_now_use_existing_repositories(tmp_path: Path) -> None:
    db = tmp_path / "tm.db"
    _prepare_db(db)
    goals = GoalsRepository(db)
    goal = goals.add("Ship UI layer", priority=1)
    EventsRepository(db).append_event(
        event_id="evt-1",
        case_id="case-1",
        activity="deep_work",
        timestamp="2026-06-18T10:00:00Z",
        lifecycle="complete",
        advances_goal=goal.goal_id,
        attributes={"planned_tasks_completed": 1, "planned_tasks_total": 1},
    )
    SuggestionTelemetryRepository(db).log_suggestion(
        suggestion_id="sug-1",
        case_date="2026-06-18",
        recommended_action="Write the cockpit shell",
        predicted_outcome_with=2.0,
        predicted_outcome_without=1.0,
    )

    dashboard = build_dashboard(db_path=db)
    now = build_now(db_path=db)

    assert dashboard["events"] == 1
    assert dashboard["avg_outcome"] == 2
    assert dashboard["top_activities"][0] == {"activity": "deep_work", "count": 1}
    assert now["directive"] == "Write the cockpit shell"
    assert now["active_goal"]["name"] == "Ship UI layer"
    assert now["outcome"]["outcome_score"] == 2


def test_selected_agent_params_uses_web_config(tmp_path: Path) -> None:
    config = tmp_path / "web-config.json"
    save_selected_agent("kimchi", model="kimchi-dev/minimax-m3", config_path=config)

    params = selected_agent_params(config)

    assert params == {
        "agent_id": "kimchi",
        "backend": "kimchi",
        "model": "kimchi-dev/minimax-m3",
    }


def test_capabilities_show_feature_branch_placeholders() -> None:
    capabilities = build_capabilities()

    assert capabilities["feature_branch_contract"] == (
        "feature/capture-dashboard-export-privacy-daemon-eval"
    )
    assert "capture" in capabilities
