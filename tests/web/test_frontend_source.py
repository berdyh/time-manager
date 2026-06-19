"""Static contract tests for frontend action wiring."""

from __future__ import annotations

from pathlib import Path


def test_debrief_run_button_posts_to_debrief_api() -> None:
    source = Path("frontend/src/main.tsx").read_text(encoding="utf-8")

    assert 'debrief: "/api/debrief"' in source
    assert "onDebrief" in source
    assert "onClick={() => onDebrief(caseDate, replacementTranscript)}" in source


def test_private_frontend_reads_send_status_token() -> None:
    source = Path("frontend/src/main.tsx").read_text(encoding="utf-8")

    assert "const status = await getJson<StatusPayload>(API.status);" in source
    assert "const apiToken = status.api_token;" in source
    assert "getJson<{ agents: Agent[] }>(API.agents, apiToken)" in source
    assert "getJson<ExportPayload>(API.export, apiToken)" in source


def test_directive_panel_actions_are_not_inert_buttons() -> None:
    source = Path("frontend/src/main.tsx").read_text(encoding="utf-8")

    assert (
        '<a className="primary-action" href="#debrief" title="Open debrief">' in source
    )
    assert '<a className="secondary-action" href="#plan" title="Open plan">' in source
    assert 'title="Start directive"' not in source
    assert 'title="Mark done"' not in source
