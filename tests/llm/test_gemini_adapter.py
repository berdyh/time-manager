"""Hermetic tests for the Gemini CLI adapter."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from tm.llm.client import LLMClient, Message
from tm.llm.errors import LLMClientError
from tm.llm.gemini_adapter import GeminiAdapter


def _completed(
    payload: dict[str, Any], *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["gemini"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


def _adapter() -> GeminiAdapter:
    return GeminiAdapter(gemini_binary="/usr/bin/true")


def test_chat_reads_response_field(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock(
        return_value=_completed(
            {
                "response": "next action",
                "model": "gemini-test",
                "usage": {"input_tokens": 4, "output_tokens": 2},
            }
        )
    )
    monkeypatch.setattr("tm.llm.gemini_adapter.subprocess.run", fake)

    response = _adapter().chat([Message(role="user", content="hi")])

    assert response.text == "next action"
    assert response.model == "gemini-test"
    assert response.input_tokens == 4
    assert response.output_tokens == 2
    assert isinstance(_adapter(), LLMClient)


def test_extract_parses_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.gemini_adapter.subprocess.run",
        MagicMock(return_value=_completed({"text": '{"ok": true}'})),
    )

    response = _adapter().extract(
        [Message(role="user", content="emit")],
        schema={"type": "object"},
    )

    assert response.data == {"ok": True}
    assert response.usage is not None


def test_nonzero_exit_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.gemini_adapter.subprocess.run",
        MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["gemini"],
                returncode=1,
                stdout="",
                stderr="auth failed",
            )
        ),
    )

    with pytest.raises(LLMClientError, match="auth failed"):
        _adapter().chat([Message(role="user", content="hi")])


def test_build_argv_uses_plan_mode_and_prompt() -> None:
    argv = GeminiAdapter(gemini_binary="/bin/gemini", model="latest")._build_argv("p")
    assert argv[:6] == [
        "/bin/gemini",
        "--skip-trust",
        "--approval-mode",
        "plan",
        "--output-format",
        "json",
    ]
    assert "--model" in argv
    assert argv[-2:] == ["--prompt", "p"]
