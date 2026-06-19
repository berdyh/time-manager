"""Static contract tests for frontend action wiring."""

from __future__ import annotations

from pathlib import Path


def test_debrief_run_button_posts_to_debrief_api() -> None:
    source = Path("frontend/src/main.tsx").read_text(encoding="utf-8")

    assert 'debrief: "/api/debrief"' in source
    assert "onDebrief" in source
    assert "onClick={() => onDebrief(caseDate, replacementTranscript)}" in source
