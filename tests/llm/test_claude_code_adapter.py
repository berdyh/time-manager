"""Tests for :class:`tm.llm.claude_code_adapter.ClaudeCodeAdapter`.

The claude binary is NEVER actually invoked: every test monkeypatches
:func:`subprocess.run` to return a canned :class:`subprocess.CompletedProcess`
whose ``stdout`` is the single JSON object that claude --output-format json
would emit. This keeps the suite hermetic and CI-safe.
"""

from __future__ import annotations

import json
import subprocess
from typing import Any
from unittest.mock import MagicMock

import pytest

from tm.llm.claude_code_adapter import (
    ANTHROPIC_API_KEY_ENV,
    TM_LLM_API_KEY_ENV,
    ClaudeCodeAdapter,
)
from tm.llm.client import LLMClient, Message
from tm.llm.errors import LLMClientError

# ----------------------------------------------------------- JSON fixtures


def _payload(
    *,
    result: str = "hello",
    model: str = "claude-sonnet-4-6",
    input_tokens: int = 100,
    output_tokens: int = 50,
    is_error: bool = False,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    p: dict[str, Any] = {
        "type": "result",
        "subtype": "success" if not is_error else "error",
        "is_error": is_error,
        "result": result,
        "session_id": "sess-abc",
        "total_cost_usd": 0.0123,
        "model": model,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    if extra:
        p.update(extra)
    return p


def _completed(
    stdout: str,
    *,
    returncode: int = 0,
    stderr: str = "",
) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=["claude"],
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
    )


def _adapter() -> ClaudeCodeAdapter:
    return ClaudeCodeAdapter(claude_binary="/usr/bin/true")


@pytest.fixture(autouse=True)
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """All tests assume TM_LLM_API_KEY is set unless they explicitly unset it."""
    monkeypatch.setenv(TM_LLM_API_KEY_ENV, "test-key-deadbeef")


# --------------------------------------------------------------- chat tests


def test_chat_parses_result_text(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = MagicMock(
        return_value=_completed(json.dumps(_payload(result="hi there from claude")))
    )
    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake)

    resp = _adapter().chat([Message(role="user", content="hello")])
    assert resp.text == "hi there from claude"
    assert resp.stop_reason == "end_turn"
    assert fake.call_count == 1


def test_chat_extracts_token_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(
            return_value=_completed(
                json.dumps(_payload(input_tokens=777, output_tokens=42))
            )
        ),
    )
    resp = _adapter().chat([Message(role="user", content="x")])
    assert resp.input_tokens == 777
    assert resp.output_tokens == 42
    assert resp.model == "claude-sonnet-4-6"


def test_chat_includes_system_prompt_when_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload()))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    _adapter().chat(
        [Message(role="user", content="hi")],
        system="be terse and helpful",
    )

    argv = captured["argv"]
    assert "--append-system-prompt" in argv
    sys_idx = argv.index("--append-system-prompt")
    assert argv[sys_idx + 1] == "be terse and helpful"


def test_chat_omits_system_prompt_when_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload()))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    _adapter().chat([Message(role="user", content="hi")])

    argv = captured["argv"]
    assert "--append-system-prompt" not in argv


# ------------------------------------------------------------ extract tests


def test_extract_parses_inner_json(monkeypatch: pytest.MonkeyPatch) -> None:
    obj = {"foo": 1, "bar": "baz"}
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(
            return_value=_completed(
                json.dumps(
                    _payload(result=json.dumps(obj), input_tokens=8, output_tokens=4)
                )
            )
        ),
    )
    schema = {"type": "object", "properties": {"foo": {"type": "integer"}}}
    resp = _adapter().extract([Message(role="user", content="emit")], schema=schema)
    assert resp.data == obj
    assert resp.usage is not None
    assert resp.usage.input_tokens == 8
    assert resp.usage.output_tokens == 4


def test_extract_includes_json_schema_in_argv(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload(result=json.dumps({"ok": True}))))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    schema = {
        "type": "object",
        "properties": {"ok": {"type": "boolean"}},
        "required": ["ok"],
    }
    _adapter().extract([Message(role="user", content="x")], schema=schema)

    argv = captured["argv"]
    assert "--json-schema" in argv
    schema_idx = argv.index("--json-schema")
    forwarded = argv[schema_idx + 1]
    # Compact JSON, sort_keys=True — assert the schema round-trips.
    assert json.loads(forwarded) == schema
    # Confirm it's compact (no whitespace).
    assert " " not in forwarded


def test_extract_raises_on_non_json_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(
            return_value=_completed(json.dumps(_payload(result="not json at all")))
        ),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().extract(
            [Message(role="user", content="x")], schema={"type": "object"}
        )
    assert "non-JSON result" in str(ei.value)


def test_extract_raises_on_non_dict_inner_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An inner JSON array is valid JSON but Protocol requires a dict."""
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(return_value=_completed(json.dumps(_payload(result="[1, 2]")))),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().extract(
            [Message(role="user", content="x")], schema={"type": "object"}
        )
    assert "non-JSON result" in str(ei.value)


# ---------------------------------------------------------- subprocess errors


def test_subprocess_timeout_raises_llm_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def boom(*args: Any, **kwargs: Any) -> Any:
        raise subprocess.TimeoutExpired(cmd="claude", timeout=1.5)

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", boom)
    adapter = ClaudeCodeAdapter(claude_binary="/usr/bin/true", timeout_s=1.5)
    with pytest.raises(LLMClientError) as ei:
        adapter.chat([Message(role="user", content="x")])
    assert "timed out" in str(ei.value)
    assert "1.5" in str(ei.value)


def test_subprocess_nonzero_exit_raises_llm_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = MagicMock(
        return_value=_completed("", returncode=1, stderr="auth failed: invalid key"),
    )
    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake)
    with pytest.raises(LLMClientError) as ei:
        _adapter().chat([Message(role="user", content="x")])
    msg = str(ei.value)
    assert "auth failed" in msg
    assert "1" in msg


def test_non_json_stdout_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(return_value=_completed("totally not json")),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().chat([Message(role="user", content="x")])
    assert "non-JSON stdout" in str(ei.value)


# ------------------------------------------------------------- construction


def test_claude_binary_not_found_raises_at_construction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("tm.llm.claude_code_adapter.shutil.which", lambda _name: None)
    with pytest.raises(LLMClientError) as ei:
        ClaudeCodeAdapter()
    msg = str(ei.value).lower()
    assert "claude" in msg
    assert "not found" in msg


def test_claude_binary_resolved_from_path(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.shutil.which",
        lambda _name: "/opt/bin/claude",
    )
    adapter = ClaudeCodeAdapter()
    argv = adapter._build_argv("hello", system=None, json_schema=None)
    assert argv[0] == "/opt/bin/claude"


# ------------------------------------------------------------- auth bridge


def test_missing_api_key_raises_at_call_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv(TM_LLM_API_KEY_ENV, raising=False)
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(return_value=_completed(json.dumps(_payload()))),
    )
    adapter = _adapter()
    with pytest.raises(LLMClientError) as ei:
        adapter.chat([Message(role="user", content="x")])
    assert TM_LLM_API_KEY_ENV in str(ei.value)


def test_api_key_bridge_sets_anthropic_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["env"] = kwargs.get("env")
        return _completed(json.dumps(_payload()))

    monkeypatch.setenv(TM_LLM_API_KEY_ENV, "secret-xyz")
    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    _adapter().chat([Message(role="user", content="x")])

    env = captured["env"]
    assert env is not None
    assert env[ANTHROPIC_API_KEY_ENV] == "secret-xyz"
    assert env[TM_LLM_API_KEY_ENV] == "secret-xyz"


# ------------------------------------------------------------- error payload


def test_is_error_response_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(
            return_value=_completed(
                json.dumps(_payload(is_error=True, result="rate limit exceeded"))
            )
        ),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().chat([Message(role="user", content="x")])
    assert "rate limit exceeded" in str(ei.value)
    assert "claude reported error" in str(ei.value)


def test_response_missing_result_field_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(
            return_value=_completed(
                json.dumps({"type": "result", "is_error": False, "model": "x"})
            )
        ),
    )
    with pytest.raises(LLMClientError) as ei:
        _adapter().chat([Message(role="user", content="x")])
    assert "missing 'result'" in str(ei.value)


# --------------------------------------------------------- tool_call stub


def test_tool_call_returns_empty_tool_calls_with_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.subprocess.run",
        MagicMock(
            return_value=_completed(
                json.dumps(
                    _payload(
                        result="would call tool_x but cannot",
                        input_tokens=11,
                        output_tokens=3,
                    )
                )
            )
        ),
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
    assert resp.text == "would call tool_x but cannot"
    assert resp.input_tokens == 11
    assert resp.output_tokens == 3


def test_tool_call_embeds_tools_in_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload()))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    _adapter().tool_call(
        [Message(role="user", content="hi")],
        tools=[{"name": "tool_x", "description": "do it"}],
        system="be helpful",
    )

    argv = captured["argv"]
    assert "--append-system-prompt" in argv
    sys_idx = argv.index("--append-system-prompt")
    sys_value = argv[sys_idx + 1]
    assert "be helpful" in sys_value
    assert "tool_x" in sys_value
    assert "Available tools" in sys_value


# -------------------------------------------- argv + prompt serialization


def test_message_serialization_includes_role_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload()))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    _adapter().chat(
        [
            Message(role="user", content="hi there"),
            Message(role="assistant", content="hello"),
            Message(role="user", content="how are you?"),
        ],
    )

    prompt = captured["argv"][-1]
    assert "[user]" in prompt
    assert "[assistant]" in prompt
    assert "hi there" in prompt
    assert "hello" in prompt
    assert "how are you?" in prompt
    # Order: first user before assistant before second user.
    assert prompt.index("[user]") < prompt.index("[assistant]")
    assert prompt.index("[assistant]") < prompt.rindex("[user]")


def test_argv_includes_required_flags(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload()))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    ClaudeCodeAdapter(claude_binary="/usr/bin/true", model="sonnet").chat(
        [Message(role="user", content="hi")]
    )

    argv = captured["argv"]
    assert "--bare" in argv
    assert "--print" in argv
    assert "--output-format" in argv
    assert argv[argv.index("--output-format") + 1] == "json"
    assert "--allow-dangerously-skip-permissions" in argv
    assert "--model" in argv
    assert argv[argv.index("--model") + 1] == "sonnet"


def test_argv_omits_model_when_none(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured["argv"] = argv
        return _completed(json.dumps(_payload()))

    monkeypatch.setattr("tm.llm.claude_code_adapter.subprocess.run", fake_run)

    _adapter().chat([Message(role="user", content="hi")])

    assert "--model" not in captured["argv"]


# --------------------------------------------------- protocol conformance


def test_adapter_satisfies_llm_client_protocol() -> None:
    adapter = ClaudeCodeAdapter(claude_binary="/usr/bin/true")
    assert isinstance(adapter, LLMClient)
