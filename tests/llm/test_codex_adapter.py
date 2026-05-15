"""Tests for :class:`tm.llm.codex_adapter.CodexAdapter`.

The codex binary is NEVER actually invoked: every test monkeypatches
:func:`subprocess.run` to return a canned :class:`subprocess.CompletedProcess`
whose ``stdout`` is a synthetic JSONL stream mimicking what codex would
emit. This keeps the suite hermetic and CI-safe.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from tm.llm.client import LLMClient, Message
from tm.llm.codex_adapter import CODEX_MODEL_LABEL, CodexAdapter
from tm.llm.errors import LLMClientError

# ----------------------------------------------------------- JSONL fixtures


def _agent_message_event(text: str) -> dict[str, Any]:
    return {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": text},
    }


def _reasoning_event(text: str = "thinking...") -> dict[str, Any]:
    return {
        "type": "item.completed",
        "item": {"type": "reasoning", "text": text},
    }


def _turn_completed(
    *, input_tokens: int = 123, output_tokens: int = 45
) -> dict[str, Any]:
    return {
        "type": "turn.completed",
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }


def _thread_started() -> dict[str, Any]:
    return {"type": "thread.started", "thread_id": "abc"}


def _stdout(events: list[dict[str, Any]]) -> str:
    return "\n".join(json.dumps(e) for e in events) + "\n"


def _completed(
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["codex"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


# Helper that builds an adapter without requiring codex on PATH.
def _adapter() -> CodexAdapter:
    return CodexAdapter(codex_binary="/usr/bin/true")


# --------------------------------------------------------------- chat tests


def test_chat_parses_last_agent_message(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [
        _thread_started(),
        _agent_message_event("hello from codex"),
        _turn_completed(input_tokens=10, output_tokens=5),
    ]
    fake = MagicMock(return_value=_completed(_stdout(events)))
    monkeypatch.setattr("tm.llm.codex_adapter.subprocess.run", fake)

    adapter = _adapter()
    resp = adapter.chat([Message(role="user", content="hi")])

    assert resp.text == "hello from codex"
    assert resp.model == CODEX_MODEL_LABEL
    assert resp.stop_reason == "end_turn"
    assert fake.call_count == 1


def test_chat_extracts_token_usage_from_turn_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _agent_message_event("ok"),
        _turn_completed(input_tokens=999, output_tokens=42),
    ]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    resp = _adapter().chat([Message(role="user", content="hi")])
    assert resp.input_tokens == 999
    assert resp.output_tokens == 42


def test_chat_picks_last_agent_message_when_multiple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _agent_message_event("first"),
        _reasoning_event(),
        _agent_message_event("second"),
        _turn_completed(),
    ]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    resp = _adapter().chat([Message(role="user", content="hi")])
    assert resp.text == "second"


def test_chat_no_agent_message_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    events = [_thread_started(), _reasoning_event(), _turn_completed()]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().chat([Message(role="user", content="hi")])
    assert "no assistant message" in str(ei.value)


# ------------------------------------------------------------ extract tests


def test_extract_parses_json_response(monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {"name": "alice", "age": 30}
    events = [
        _agent_message_event(json.dumps(payload)),
        _turn_completed(input_tokens=7, output_tokens=3),
    ]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    schema: dict[str, Any] = {
        "type": "object",
        "properties": {"name": {"type": "string"}, "age": {"type": "integer"}},
    }
    resp = _adapter().extract([Message(role="user", content="emit")], schema=schema)
    assert resp.data == payload
    assert resp.usage is not None
    assert resp.usage.input_tokens == 7
    assert resp.usage.output_tokens == 3


def test_extract_raises_on_non_json_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _agent_message_event("sorry, I can't do that"),
        _turn_completed(),
    ]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().extract(
            [Message(role="user", content="x")], schema={"type": "object"}
        )
    assert "non-JSON" in str(ei.value)


def test_extract_raises_on_non_dict_json(monkeypatch: pytest.MonkeyPatch) -> None:
    """A JSON array is valid JSON but the protocol requires a dict."""
    events = [_agent_message_event("[1, 2, 3]"), _turn_completed()]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().extract(
            [Message(role="user", content="x")], schema={"type": "object"}
        )
    assert "non-JSON" in str(ei.value)


# ---------------------------------------------------------- subprocess errors


def test_subprocess_timeout_raises_llm_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="codex", timeout=1.0)

    monkeypatch.setattr("tm.llm.codex_adapter.subprocess.run", boom)
    adapter = CodexAdapter(codex_binary="/usr/bin/true", timeout_s=1.0)
    with pytest.raises(LLMClientError) as ei:
        adapter.chat([Message(role="user", content="x")])
    assert "timed out" in str(ei.value)
    assert "1.0" in str(ei.value)


def test_subprocess_nonzero_exit_raises_llm_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        return_value=_completed("", returncode=1, stderr="auth failed: no token"),
    )
    monkeypatch.setattr("tm.llm.codex_adapter.subprocess.run", fake)
    with pytest.raises(LLMClientError) as ei:
        _adapter().chat([Message(role="user", content="x")])
    msg = str(ei.value)
    assert "auth failed" in msg
    assert "1" in msg


# ------------------------------------------------------------- construction


def test_codex_binary_not_found_raises_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tm.llm.codex_adapter.shutil.which", lambda _name: None)
    with pytest.raises(LLMClientError) as ei:
        CodexAdapter()
    assert "codex" in str(ei.value).lower()
    assert "not found" in str(ei.value).lower()


def test_invalid_reasoning_effort_raises_at_construction() -> None:
    """Reasoning-effort allowlist hardens the ``-c`` interpolation surface."""
    with pytest.raises(LLMClientError) as ei:
        CodexAdapter(codex_binary="/usr/bin/true", reasoning_effort="ultra")
    msg = str(ei.value)
    assert "reasoning_effort" in msg
    assert "ultra" in msg


def test_codex_binary_resolved_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.codex_adapter.shutil.which",
        lambda _name: "/opt/bin/codex",
    )
    adapter = CodexAdapter()
    # Sanity: the resolved path is used when building argv.
    argv = adapter._build_argv("hello")
    assert argv[0] == "/opt/bin/codex"


def test_adapter_satisfies_llm_client_protocol() -> None:
    adapter = _adapter()
    assert isinstance(adapter, LLMClient)


# --------------------------------------------- argv + prompt serialization


def test_message_serialization_includes_role_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        events = [_agent_message_event("ok"), _turn_completed()]
        return _completed(_stdout(events))

    monkeypatch.setattr("tm.llm.codex_adapter.subprocess.run", fake_run)

    _adapter().chat(
        [
            Message(role="user", content="hi there"),
            Message(role="assistant", content="hello"),
            Message(role="user", content="how are you?"),
        ],
        system="you are helpful",
    )

    # The final positional argument is the prompt.
    prompt = captured["argv"][-1]
    assert "[user]" in prompt
    assert "[assistant]" in prompt
    assert "[system]" in prompt
    assert "you are helpful" in prompt
    assert "hi there" in prompt
    assert "hello" in prompt
    assert "how are you?" in prompt
    # Order: system block first, user/assistant/user follow.
    assert prompt.index("[system]") < prompt.index("[user]")
    assert prompt.index("[assistant]") < prompt.rindex("[user]")


def test_argv_includes_required_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        events = [_agent_message_event("ok"), _turn_completed()]
        return _completed(_stdout(events))

    monkeypatch.setattr("tm.llm.codex_adapter.subprocess.run", fake_run)

    CodexAdapter(
        codex_binary="/usr/bin/true",
        reasoning_effort="high",
        model="gpt-5-codex",
    ).chat([Message(role="user", content="hi")])

    argv = captured["argv"]
    assert "exec" in argv
    assert "--json" in argv
    assert "--skip-git-repo-check" in argv
    assert "-s" in argv
    # read-only sandbox follows -s
    assert argv[argv.index("-s") + 1] == "read-only"
    # -c with reasoning effort override
    assert "-c" in argv
    c_idx = argv.index("-c")
    assert "model_reasoning_effort" in argv[c_idx + 1]
    assert "high" in argv[c_idx + 1]
    # explicit model passthrough
    assert "-m" in argv
    assert argv[argv.index("-m") + 1] == "gpt-5-codex"


# --------------------------------------------------------- tool_call stub


def test_tool_call_returns_empty_tool_calls_with_message_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = [
        _agent_message_event("I would call tool_x but cannot."),
        _turn_completed(input_tokens=5, output_tokens=2),
    ]
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(_stdout(events))),
    )
    tools = [
        {
            "name": "tool_x",
            "description": "Do something",
            "input_schema": {"type": "object"},
        }
    ]
    resp = _adapter().tool_call(
        [Message(role="user", content="please use a tool")],
        tools=tools,
        system="be terse",
    )
    assert resp.tool_calls == []
    assert resp.text == "I would call tool_x but cannot."
    assert resp.model == CODEX_MODEL_LABEL
    assert resp.input_tokens == 5
    assert resp.output_tokens == 2


def test_tool_call_embeds_tools_in_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        events = [_agent_message_event("ok"), _turn_completed()]
        return _completed(_stdout(events))

    monkeypatch.setattr("tm.llm.codex_adapter.subprocess.run", fake_run)

    _adapter().tool_call(
        [Message(role="user", content="hi")],
        tools=[{"name": "tool_x", "description": "do it"}],
    )

    prompt = captured["argv"][-1]
    assert "tool_x" in prompt
    assert "Available tools" in prompt


# -------------------------------------------- robustness: noisy JSONL stdout


def test_invoke_codex_skips_non_json_lines(monkeypatch: pytest.MonkeyPatch) -> None:
    """Codex sometimes prefixes the stream with a human banner; we skip it."""
    stdout_lines = [
        "Codex CLI version 1.0",
        "",
        json.dumps(_agent_message_event("clean")),
        "not-json-tail",
        json.dumps(_turn_completed(input_tokens=1, output_tokens=2)),
    ]
    stdout = "\n".join(stdout_lines) + "\n"
    monkeypatch.setattr(
        "tm.llm.codex_adapter.subprocess.run",
        MagicMock(return_value=_completed(stdout)),
    )
    resp = _adapter().chat([Message(role="user", content="x")])
    assert resp.text == "clean"
    assert resp.input_tokens == 1
    assert resp.output_tokens == 2
