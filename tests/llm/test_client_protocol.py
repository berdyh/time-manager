"""Type/protocol-shape tests for tm.llm.client.LLMClient.

The ``runtime_checkable`` Protocol can be checked with ``isinstance`` at
runtime — that's the cheapest way to assert AnthropicAdapter is structurally
compatible without standing up a real Anthropic client.

We construct AnthropicAdapter with an injected mock client so __init__ does
not require ``TM_LLM_API_KEY``.
"""

from __future__ import annotations

import inspect
from unittest.mock import MagicMock

from tm.llm.anthropic_adapter import AnthropicAdapter
from tm.llm.client import (
    ChatResponse,
    LLMClient,
    Message,
    ToolCall,
    ToolCallResponse,
)


def test_anthropic_adapter_is_llmclient_at_runtime() -> None:
    """AnthropicAdapter satisfies the runtime_checkable LLMClient Protocol."""
    adapter = AnthropicAdapter(client=MagicMock())
    assert isinstance(adapter, LLMClient)


def test_protocol_has_three_required_methods() -> None:
    """Protocol surface is exactly chat/extract/tool_call (plus dunders)."""
    public = {name for name in vars(LLMClient) if not name.startswith("_")}
    assert {"chat", "extract", "tool_call"}.issubset(public)


def test_dataclass_shapes_are_immutable() -> None:
    """Frozen dataclasses prevent accidental mutation of transport types."""
    msg = Message(role="user", content="hi")
    chat = ChatResponse(text="hello", model="m", input_tokens=1, output_tokens=2)
    tc = ToolCall(id="x", name="t", input={"a": 1})
    tcr = ToolCallResponse(text="", tool_calls=[tc], model="m")

    for obj, attr in (
        (msg, "role"),
        (chat, "text"),
        (tc, "id"),
        (tcr, "text"),
    ):
        try:
            setattr(obj, attr, "mutated")
        except Exception:  # noqa: BLE001 - dataclasses raises FrozenInstanceError
            continue
        else:
            raise AssertionError(f"{type(obj).__name__}.{attr} should be immutable")


def test_adapter_method_signatures_match_protocol() -> None:
    """Each adapter method has a signature compatible with the Protocol."""
    for name in ("chat", "extract", "tool_call"):
        proto_sig = inspect.signature(getattr(LLMClient, name))
        adapter_sig = inspect.signature(getattr(AnthropicAdapter, name))
        # First positional is `self` for both; ensure matching named params.
        proto_params = set(proto_sig.parameters)
        adapter_params = set(adapter_sig.parameters)
        assert proto_params <= adapter_params, (
            f"{name}: adapter is missing protocol params "
            f"{proto_params - adapter_params}"
        )
