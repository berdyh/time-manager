"""Tests for tm.llm.anthropic_adapter.AnthropicAdapter.

All SDK calls are mocked via unittest.mock.MagicMock — no network. The
adapter's job is to translate between the SDK's native shapes and our
provider-neutral dataclasses; these tests pin that translation in place.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from tm.llm.anthropic_adapter import (
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    EXTRACT_TOOL_NAME,
    AnthropicAdapter,
)
from tm.llm.client import ChatResponse, Message, ToolCall, ToolCallResponse
from tm.llm.errors import LLMClientError


def _sdk_message(
    *,
    content: list[Any],
    model: str = DEFAULT_MODEL,
    input_tokens: int = 100,
    output_tokens: int = 50,
    stop_reason: str | None = "end_turn",
) -> Any:
    """Build a SimpleNamespace mimicking ``anthropic.types.Message``."""
    return SimpleNamespace(
        content=content,
        model=model,
        usage=SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens),
        stop_reason=stop_reason,
    )


def _text_block(text: str) -> Any:
    return SimpleNamespace(type="text", text=text)


def _tool_use_block(*, id: str, name: str, input: dict[str, Any]) -> Any:
    return SimpleNamespace(type="tool_use", id=id, name=name, input=input)


# ------------------------------------------------------------ construction


def test_init_requires_api_key_when_no_client_injected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    with pytest.raises(LLMClientError) as ei:
        AnthropicAdapter()
    assert "TM_LLM_API_KEY" in str(ei.value)
    # Ensure the env-var name appears, but the value (if any) is not leaked.
    assert "not set" in str(ei.value).lower()


def test_init_with_injected_client_does_not_need_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)
    fake = MagicMock()
    adapter = AnthropicAdapter(client=fake)
    assert adapter is not None


def test_init_reads_api_key_from_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When env var is set, AnthropicAdapter constructs without raising.

    We patch anthropic.Anthropic itself so the test never hits the network,
    and verify the adapter passes the env key through to the SDK constructor.
    """
    captured: dict[str, Any] = {}

    class FakeAnthropic:
        def __init__(self, *, api_key: str) -> None:
            captured["api_key"] = api_key

    monkeypatch.setenv("TM_LLM_API_KEY", "test-key-secret-value")
    monkeypatch.setattr("anthropic.Anthropic", FakeAnthropic)

    adapter = AnthropicAdapter()
    assert adapter is not None
    assert captured.get("api_key") == "test-key-secret-value"


# ---------------------------------------------------------------- chat


def test_chat_passes_messages_and_returns_chat_response() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(
        content=[_text_block("hello world")],
        model="claude-sonnet-4-6",
        input_tokens=42,
        output_tokens=7,
    )
    adapter = AnthropicAdapter(client=fake, model="claude-sonnet-4-6")

    resp = adapter.chat(
        [Message(role="user", content="hi")],
        system="be terse",
    )

    assert isinstance(resp, ChatResponse)
    assert resp.text == "hello world"
    assert resp.model == "claude-sonnet-4-6"
    assert resp.input_tokens == 42
    assert resp.output_tokens == 7
    assert resp.stop_reason == "end_turn"

    # Verify SDK was called with the right shape.
    fake.messages.create.assert_called_once()
    kwargs = fake.messages.create.call_args.kwargs
    assert kwargs["model"] == "claude-sonnet-4-6"
    assert kwargs["max_tokens"] == DEFAULT_MAX_TOKENS
    assert kwargs["system"] == "be terse"
    assert kwargs["messages"] == [{"role": "user", "content": "hi"}]


def test_chat_omits_system_when_none() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(content=[_text_block("ok")])
    adapter = AnthropicAdapter(client=fake)
    adapter.chat([Message(role="user", content="hi")])
    kwargs = fake.messages.create.call_args.kwargs
    assert "system" not in kwargs


def test_chat_concatenates_multiple_text_blocks() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(
        content=[_text_block("hello "), _text_block("world")]
    )
    adapter = AnthropicAdapter(client=fake)
    resp = adapter.chat([Message(role="user", content="hi")])
    assert resp.text == "hello world"


# --------------------------------------------------------------- extract


def test_extract_forces_synthetic_tool_and_returns_input() -> None:
    payload = {"name": "Sasha", "score": 0.87}
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(
        content=[_tool_use_block(id="t1", name=EXTRACT_TOOL_NAME, input=payload)]
    )
    adapter = AnthropicAdapter(client=fake)
    schema = {
        "type": "object",
        "properties": {
            "name": {"type": "string"},
            "score": {"type": "number"},
        },
        "required": ["name", "score"],
    }
    out = adapter.extract([Message(role="user", content="who?")], schema=schema)
    assert out == payload

    kwargs = fake.messages.create.call_args.kwargs
    assert kwargs["tool_choice"] == {"type": "tool", "name": EXTRACT_TOOL_NAME}
    tools = kwargs["tools"]
    assert isinstance(tools, list) and len(tools) == 1
    assert tools[0]["name"] == EXTRACT_TOOL_NAME
    assert tools[0]["input_schema"] == schema


def test_extract_raises_when_no_tool_use_block() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(content=[_text_block("I refuse")])
    adapter = AnthropicAdapter(client=fake)
    with pytest.raises(LLMClientError):
        adapter.extract(
            [Message(role="user", content="hi")],
            schema={"type": "object"},
        )


# -------------------------------------------------------------- tool_call


def test_tool_call_returns_parsed_tool_use_blocks() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(
        content=[
            _text_block("I'll use a tool. "),
            _tool_use_block(id="abc", name="lookup", input={"q": "weather"}),
            _tool_use_block(id="def", name="lookup", input={"q": "time"}),
        ],
        stop_reason="tool_use",
    )
    adapter = AnthropicAdapter(client=fake)
    tools = [
        {
            "name": "lookup",
            "description": "Look up a value.",
            "input_schema": {
                "type": "object",
                "properties": {"q": {"type": "string"}},
                "required": ["q"],
            },
        }
    ]
    resp = adapter.tool_call(
        [Message(role="user", content="weather and time")],
        tools=tools,
        system="be helpful",
    )

    assert isinstance(resp, ToolCallResponse)
    assert resp.text == "I'll use a tool. "
    assert resp.stop_reason == "tool_use"
    assert len(resp.tool_calls) == 2
    assert resp.tool_calls[0] == ToolCall(
        id="abc", name="lookup", input={"q": "weather"}
    )
    assert resp.tool_calls[1] == ToolCall(id="def", name="lookup", input={"q": "time"})

    kwargs = fake.messages.create.call_args.kwargs
    assert kwargs["tools"] == tools
    assert kwargs["system"] == "be helpful"


def test_tool_call_with_no_tool_use_returns_empty_list() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(
        content=[_text_block("Just talking, no tools.")],
        stop_reason="end_turn",
    )
    adapter = AnthropicAdapter(client=fake)
    resp = adapter.tool_call(
        [Message(role="user", content="hi")],
        tools=[
            {
                "name": "noop",
                "description": "do nothing",
                "input_schema": {"type": "object"},
            }
        ],
    )
    assert resp.tool_calls == []
    assert resp.text == "Just talking, no tools."


# -------------------------------------------------------- usage / robustness


def test_usage_missing_falls_back_to_zero() -> None:
    fake = MagicMock()
    # Build a message with no `usage` attr at all.
    fake.messages.create.return_value = SimpleNamespace(
        content=[_text_block("hi")],
        model=DEFAULT_MODEL,
        stop_reason="end_turn",
        usage=None,
    )
    adapter = AnthropicAdapter(client=fake)
    resp = adapter.chat([Message(role="user", content="hi")])
    assert resp.input_tokens == 0
    assert resp.output_tokens == 0


def test_default_model_is_set() -> None:
    fake = MagicMock()
    fake.messages.create.return_value = _sdk_message(content=[_text_block("ok")])
    adapter = AnthropicAdapter(client=fake)
    adapter.chat([Message(role="user", content="hi")])
    kwargs = fake.messages.create.call_args.kwargs
    assert kwargs["model"] == DEFAULT_MODEL
