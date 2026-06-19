"""Read-side service functions for the local tm web UI."""

from __future__ import annotations

import importlib.util
import tempfile
from collections import Counter
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import asdict
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any

import typer

from tm._paths import default_db_path, default_socket_path
from tm.commands.capture import capture_calendar, capture_telegram, capture_voice
from tm.commands.export import _export_payload, backup
from tm.commands.privacy import forget, redact
from tm.commands.reextract import reextract
from tm.daemon import DaemonClient
from tm.llm.cost_meter import CostMeter
from tm.local_agents import (
    ROUTEABLE_BACKENDS,
    default_selected_agent,
    load_agent_config,
    probe_agents,
    save_selected_agent,
)
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository
from tm.security import connect_sqlite, encryption_status
from tm.stores.sqlite_store import SQLiteStore

__all__ = [
    "agent_selection",
    "build_capabilities",
    "build_dashboard",
    "build_now",
    "build_status",
    "capture_calendar_text",
    "capture_telegram_json",
    "capture_voice_text",
    "ensure_web_db",
    "export_payload",
    "privacy_forget",
    "privacy_redact",
    "reextract_case",
    "run_backup",
    "selected_agent_params",
]

CAPABILITY_MODULES = {
    "capture": "tm.commands.capture",
    "dashboard_command": "tm.commands.dashboard",
    "export": "tm.commands.export",
    "privacy": "tm.commands.privacy",
    "reextract": "tm.commands.reextract",
    "encryption": "tm.security",
}


def ensure_web_db(db_path: Path | None = None) -> Path:
    """Apply migrations and return the database path the UI should use."""

    resolved = db_path or default_db_path()
    store = SQLiteStore(resolved)
    try:
        store.apply_pending_migrations()
    finally:
        store.close()
    return resolved


def build_status(
    *,
    db_path: Path | None = None,
    socket_path: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Return local runtime status for the cockpit header."""

    resolved_db = db_path or default_db_path()
    resolved_socket = socket_path or default_socket_path()
    daemon = _daemon_status(resolved_socket)
    cost = _cost_status(resolved_db)
    config = load_agent_config(config_path)
    return {
        "db_path": str(resolved_db),
        "socket_path": str(resolved_socket),
        "daemon": daemon,
        "cost": cost,
        "selected_agent": default_selected_agent(config_path),
        "selected_model": config.get("selected_model"),
        "encryption": _encryption_status(resolved_db),
        "generated_at": _now_iso(),
    }


def build_capabilities() -> dict[str, Any]:
    """Report which branch-dependent UI panels can perform real actions."""

    capabilities: dict[str, Any] = {
        name: _module_available(module) for name, module in CAPABILITY_MODULES.items()
    }
    capabilities.update(
        {
            "variants_trend": True,
            "feature_branch_contract": (
                "feature/capture-dashboard-export-privacy-daemon-eval"
            ),
        }
    )
    return capabilities


def agent_selection(
    agent_id: str,
    *,
    model: str | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Persist a selected routeable local agent and return fresh statuses."""

    config = save_selected_agent(agent_id, model=model, config_path=config_path)
    return {
        "selected_agent": config["selected_agent"],
        "selected_model": config.get("selected_model"),
        "agents": probe_agents(config_path),
    }


def selected_agent_params(config_path: Path | None = None) -> dict[str, str | None]:
    """Return daemon params for the currently selected routeable agent."""

    config = load_agent_config(config_path)
    agent_id = default_selected_agent(config_path)
    backend = agent_id if agent_id in ROUTEABLE_BACKENDS else None
    return {
        "agent_id": agent_id,
        "backend": backend,
        "model": _optional_str(config.get("selected_model")),
    }


def build_dashboard(
    *,
    db_path: Path | None = None,
    since: str | None = None,
    until: str | None = None,
) -> dict[str, Any]:
    """Return a compact metrics summary from existing repository APIs."""

    resolved_db = db_path or default_db_path()
    events_repo = EventsRepository(resolved_db)
    goals_repo = GoalsRepository(resolved_db)
    telemetry_repo = SuggestionTelemetryRepository(resolved_db)
    events = events_repo.query_events(since=since, until=until)
    case_dates = events_repo.list_distinct_case_dates(since=since, until=until)
    active_goals = goals_repo.list(status="active")
    suggestions = telemetry_repo.list_recent(limit=25)
    activities = Counter(str(event["activity"]) for event in events)
    outcome = OutcomeAggregator(events_repo)
    outcomes = [
        outcome.for_case_date(case_date).outcome_score for case_date in case_dates
    ]
    avg_outcome = sum(outcomes) / len(outcomes) if outcomes else None
    transcript_count = _table_count(
        resolved_db,
        "transcripts",
        date_column="case_date",
        since=since,
        until=until,
    )
    privacy_actions = _table_count(resolved_db, "privacy_actions")
    return {
        "available": True,
        "source": "feature-integrated",
        "events": len(events),
        "case_dates": case_dates,
        "transcripts": transcript_count,
        "privacy_actions": privacy_actions,
        "active_goals": [_goal_dict(goal) for goal in active_goals],
        "suggestions": [_suggestion_dict(item) for item in suggestions],
        "avg_outcome": avg_outcome,
        "top_activities": [
            {"activity": activity, "count": count}
            for activity, count in activities.most_common(8)
        ],
    }


def build_now(
    *,
    db_path: Path | None = None,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Return the first-screen operating state."""

    resolved_db = db_path or default_db_path()
    events_repo = EventsRepository(resolved_db)
    goals_repo = GoalsRepository(resolved_db)
    telemetry_repo = SuggestionTelemetryRepository(resolved_db)

    active_goals = goals_repo.list(status="active")
    active_goal = _goal_dict(active_goals[0]) if active_goals else None
    recent_suggestions = telemetry_repo.list_recent(limit=1)
    recent_events = events_repo.query_events(limit=8)
    recent_events = list(reversed(recent_events))
    case_dates = events_repo.list_distinct_case_dates()
    latest_case_date = case_dates[-1] if case_dates else None
    outcome = None
    if latest_case_date is not None:
        outcome = asdict(OutcomeAggregator(events_repo).for_case_date(latest_case_date))

    suggestion = _suggestion_dict(recent_suggestions[0]) if recent_suggestions else None
    directive = _directive_text(suggestion=suggestion, active_goal=active_goal)
    agent = selected_agent_params(config_path)
    return {
        "directive": directive,
        "active_goal": active_goal,
        "latest_case_date": latest_case_date,
        "outcome": outcome,
        "suggestion": suggestion,
        "recent_events": recent_events,
        "selected_agent": agent,
        "schedule_delta": _schedule_delta(recent_events),
    }


def capture_telegram_json(
    *,
    db_path: Path | None,
    content: str,
) -> dict[str, Any]:
    return _capture_input_path(
        capture_telegram,
        db_path=db_path,
        content=content,
        suffix=".json",
        response_key="dashboard",
        response_builder=build_dashboard,
    )


def capture_calendar_text(
    *,
    db_path: Path | None,
    content: str,
) -> dict[str, Any]:
    return _capture_input_path(
        capture_calendar,
        db_path=db_path,
        content=content,
        suffix=".ics",
        response_key="dashboard",
        response_builder=build_dashboard,
    )


def capture_voice_text(
    *,
    db_path: Path | None,
    transcript: str,
    case_date: str,
) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    with _temp_text_file(transcript, suffix=".txt") as path:
        _run_typer_command(
            capture_voice,
            transcript_file=path,
            db_path=resolved_db,
            case_date=case_date,
        )
    return {"ok": True, "now": build_now(db_path=resolved_db)}


def export_payload(*, db_path: Path | None) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    payload = _export_payload(resolved_db)
    return {
        "ok": True,
        "tables": payload,
        "rows_exported": sum(len(rows) for rows in payload.values()),
    }


def run_backup(
    *,
    db_path: Path | None,
    output_path: Path,
    overwrite: bool = False,
) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    expanded_output = output_path.expanduser()
    _run_typer_command(
        backup,
        db_path=resolved_db,
        output=expanded_output,
        overwrite=overwrite,
    )
    return {"ok": True, "output_path": str(expanded_output)}


def privacy_redact(
    *,
    db_path: Path | None,
    case_date: str | None,
    event_id: str | None,
    replacement: str = "[redacted]",
) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    _run_typer_command(
        redact,
        db_path=resolved_db,
        case_date=case_date,
        event_id=event_id,
        replacement=replacement,
    )
    return {"ok": True, "dashboard": build_dashboard(db_path=resolved_db)}


def privacy_forget(
    *,
    db_path: Path | None,
    case_date: str | None,
    event_id: str | None,
) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    _run_typer_command(
        forget,
        db_path=resolved_db,
        case_date=case_date,
        event_id=event_id,
    )
    return {"ok": True, "dashboard": build_dashboard(db_path=resolved_db)}


def reextract_case(
    *,
    db_path: Path | None,
    case_date: str,
    transcript: str | None = None,
) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    transcript_file: Path | None = None
    with tempfile.TemporaryDirectory(prefix="tm-web-reextract-") as raw_dir:
        if transcript is not None and transcript.strip():
            transcript_file = Path(raw_dir) / "replacement-transcript.txt"
            transcript_file.write_text(transcript, encoding="utf-8")
        _run_typer_command(
            reextract,
            db_path=resolved_db,
            case_date=case_date,
            transcript_file=transcript_file,
        )
    return {"ok": True, "now": build_now(db_path=resolved_db)}


def _daemon_status(socket_path: Path) -> dict[str, Any]:
    if not socket_path.exists():
        return {"alive": False, "detail": "socket not found"}
    try:
        result = DaemonClient(socket_path, timeout=1.0).call("ping")
    except Exception as exc:  # noqa: BLE001 - status surface must not raise
        return {"alive": False, "detail": str(exc)}
    return {"alive": True, "detail": "pong", "result": result}


def _cost_status(db_path: Path) -> dict[str, Any]:
    try:
        meter = CostMeter(db_path)
        return {
            "monthly_total_usd": meter.monthly_total(),
            "monthly_cap_usd": meter.monthly_cap_usd,
        }
    except Exception as exc:  # noqa: BLE001 - dashboard status only
        return {"monthly_total_usd": None, "monthly_cap_usd": None, "error": str(exc)}


def _encryption_status(db_path: Path) -> dict[str, Any]:
    try:
        status = encryption_status(db_path)
    except Exception as exc:  # noqa: BLE001
        return {"available": True, "status": "error", "detail": str(exc)}
    return {"available": True, "status": asdict(status)}


def _goal_dict(goal: Any) -> dict[str, Any]:
    return asdict(goal)


def _suggestion_dict(record: Any) -> dict[str, Any]:
    return asdict(record)


def _optional_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _capture_input_path(
    command: Any,
    *,
    db_path: Path | None,
    content: str,
    suffix: str,
    response_key: str,
    response_builder: Any,
) -> dict[str, Any]:
    resolved_db = ensure_web_db(db_path)
    with _temp_text_file(content, suffix=suffix) as path:
        _run_typer_command(command, input_path=path, db_path=resolved_db)
    return {"ok": True, response_key: response_builder(db_path=resolved_db)}


def _directive_text(
    *,
    suggestion: dict[str, Any] | None,
    active_goal: dict[str, Any] | None,
) -> str:
    if suggestion is not None:
        return str(suggestion["recommended_action"])
    if active_goal is not None:
        return f"Advance {active_goal['name']}"
    return "Add one active goal"


def _schedule_delta(recent_events: list[dict[str, Any]]) -> dict[str, Any]:
    if not recent_events:
        return {"label": "No recent events", "event_count": 0}
    activities = [str(event["activity"]) for event in recent_events[:3]]
    return {
        "label": "Recent trace updated",
        "event_count": len(recent_events),
        "activities": activities,
    }


def _module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _table_count(
    db_path: Path,
    table: str,
    *,
    date_column: str | None = None,
    since: str | None = None,
    until: str | None = None,
) -> int:
    conn = connect_sqlite(db_path, row_factory=True)
    try:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,),
        ).fetchone()
        if exists is None:
            return 0
        clauses, params = _date_filters(
            date_column=date_column,
            since=since,
            until=until,
        )
        sql = f"SELECT COUNT(*) AS cnt FROM {table}"
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        row = conn.execute(sql, params).fetchone()
        return int(row["cnt"]) if row is not None else 0
    finally:
        conn.close()


def _date_filters(
    *,
    date_column: str | None,
    since: str | None,
    until: str | None,
) -> tuple[list[str], list[Any]]:
    clauses: list[str] = []
    params: list[Any] = []
    if date_column and since:
        clauses.append(f"{date_column} >= ?")
        params.append(since)
    if date_column and until:
        clauses.append(f"{date_column} <= ?")
        params.append(until)
    return clauses, params


def _run_typer_command(fn: Any, **kwargs: Any) -> None:
    stdout = StringIO()
    stderr = StringIO()
    try:
        with redirect_stdout(stdout), redirect_stderr(stderr):
            fn(**kwargs)
    except typer.Exit as exc:
        detail = stderr.getvalue().strip() or stdout.getvalue().strip()
        raise RuntimeError(detail or f"command exited {exc.exit_code}") from exc


class _temp_text_file:
    def __init__(self, content: str, *, suffix: str) -> None:
        self._content = content
        self._suffix = suffix
        self.path: Path | None = None

    def __enter__(self) -> Path:
        fd, raw_path = tempfile.mkstemp(prefix="tm-web-", suffix=self._suffix)
        path = Path(raw_path)
        with open(fd, "w", encoding="utf-8") as handle:
            handle.write(self._content)
        self.path = path
        return path

    def __exit__(self, *_args: object) -> None:
        if self.path is not None:
            self.path.unlink(missing_ok=True)
