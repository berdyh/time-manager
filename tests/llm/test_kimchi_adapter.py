"""Hermetic tests for the Kimchi CLI adapter."""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from tm.llm.client import LLMClient, Message
from tm.llm.errors import LLMClientError
from tm.llm.kimchi_adapter import KimchiAdapter


def _completed(
    payload: dict[str, Any], *, returncode: int = 0
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["kimchi"],
        returncode=returncode,
        stdout=json.dumps(payload),
        stderr="",
    )


def _adapter() -> KimchiAdapter:
    return KimchiAdapter(kimchi_binary="/usr/bin/true")


def test_chat_reads_text_field(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock(
        return_value=_completed(
            {
                "text": "do this now",
                "model": "kimchi-test",
                "usage": {"input_tokens": 9, "output_tokens": 3},
            }
        )
    )
    monkeypatch.setattr("tm.llm.kimchi_adapter.subprocess.run", fake)

    response = _adapter().chat([Message(role="user", content="hi")])

    assert response.text == "do this now"
    assert response.model == "kimchi-test"
    assert response.input_tokens == 9
    assert response.output_tokens == 3
    assert isinstance(_adapter(), LLMClient)


def test_extract_parses_fenced_json_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.kimchi_adapter.subprocess.run",
        MagicMock(return_value=_completed({"result": '```json\n{"ok": true}\n```'})),
    )

    response = _adapter().extract(
        [Message(role="user", content="emit")],
        schema={"type": "object"},
    )

    assert response.data == {"ok": True}
    assert response.usage is not None


def test_non_json_stdout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.kimchi_adapter.subprocess.run",
        MagicMock(
            return_value=subprocess.CompletedProcess(
                args=["kimchi"],
                returncode=0,
                stdout="not json",
                stderr="",
            )
        ),
    )

    with pytest.raises(LLMClientError, match="non-JSON stdout"):
        _adapter().chat([Message(role="user", content="hi")])


def test_build_argv_uses_plan_mode_and_no_session() -> None:
    argv = KimchiAdapter(
        kimchi_binary="/bin/kimchi", model="kimchi-dev/latest"
    )._build_argv("p")
    assert argv[:6] == [
        "/bin/kimchi",
        "--plan",
        "--no-session",
        "--mode",
        "json",
        "--print",
    ]
    assert "--model" in argv
    assert argv[-1] == "p"
