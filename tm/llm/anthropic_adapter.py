"""Anthropic-direct adapter implementing :class:`tm.llm.client.LLMClient`.

This is the only adapter that ships in v1. It wraps the official
``anthropic`` Python SDK (``anthropic.Anthropic``) and translates between
the SDK's native shapes and our provider-neutral dataclasses in
:mod:`tm.llm.client`.

Key design points:

* The API key is read from the environment variable ``TM_LLM_API_KEY``. If
  the variable is unset, construction raises
  :class:`tm.llm.errors.LLMClientError`. The key is NEVER logged or
  serialized.
* :meth:`AnthropicAdapter.chat` is a straight pass-through to
  ``client.messages.create``.
* :meth:`AnthropicAdapter.extract` uses the SDK's tool-use mode with a
  single synthetic tool whose ``input_schema`` is the caller-provided JSON
  schema. The model is forced to call that tool via ``tool_choice``; we
  parse the resulting ``tool_use`` block back into a plain ``dict``.
* :meth:`AnthropicAdapter.tool_call` exposes the tool list as-is and
  returns parsed :class:`ToolCall` objects alongside any free-form text.

Cost-meter integration is intentionally NOT wired in here yet: the v1 task
scope leaves orchestration (pre-call gate + post-call ledger insert) to the
caller. The adapter does, however, surface ``input_tokens`` and
``output_tokens`` on every response so the caller can drive the meter.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING, Any

from tm.llm.client import (
    ChatResponse,
    ExtractResponse,
    Message,
    ToolCall,
    ToolCallResponse,
    Usage,
)
from tm.llm.errors import LLMClientError

if TYPE_CHECKING:  # pragma: no cover
    from anthropic import Anthropic

__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "DEFAULT_MAX_TOKENS",
    "DEFAULT_MODEL",
    "EXTRACT_TOOL_NAME",
    "AnthropicAdapter",
]

# Canonical env var name for the LLM API key. Other modules import this
# constant rather than redefining the literal — see ``tm.commands._shared``
# and ``tm.daemon``.
ANTHROPIC_API_KEY_ENV = "TM_LLM_API_KEY"

# Default model id for the v1 adapter. Callers may override per-call.
DEFAULT_MODEL = "claude-sonnet-4-6"

# Default max output token budget. Anthropic's API requires this to be set;
# pick a value large enough for medium-length replies but small enough to
# keep the pre-call cost estimate sane.
DEFAULT_MAX_TOKENS = 1024

# The synthetic tool name we use under the hood for ``extract``. The model
# never sees this in the user-facing transcript; it's a routing hint only.
EXTRACT_TOOL_NAME = "emit_extracted_object"


class AnthropicAdapter:
    """Concrete :class:`tm.llm.client.LLMClient` over ``anthropic.Anthropic``.

    Parameters:
        model: default Anthropic model id; can be overridden per call.
        max_tokens: default max output tokens; can be overridden per call.
        client: optional pre-built ``anthropic.Anthropic`` (used in tests to
            inject mocks; in production leave ``None`` to construct one
            from the env-var API key).
    """

    def __init__(
        self,
        *,
        model: str = DEFAULT_MODEL,
        max_tokens: int = DEFAULT_MAX_TOKENS,
        client: Anthropic | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = int(max_tokens)
        if client is not None:
            self._client = client
        else:
            api_key = os.environ.get(ANTHROPIC_API_KEY_ENV)
            if not api_key:
                raise LLMClientError(f"{ANTHROPIC_API_KEY_ENV} not set")
            # Local import so module load doesn't pay for SDK init in tests
            # that inject their own mock client.
            from anthropic import Anthropic

            self._client = Anthropic(api_key=api_key)

    # ------------------------------------------------------------------- API

    def chat(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
    ) -> ChatResponse:
        """Run a vanilla chat completion."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": _to_sdk_messages(messages),
        }
        if system is not None:
            kwargs["system"] = system

        sdk_msg = self._client.messages.create(**kwargs)
        text = _join_text_blocks(_get_content(sdk_msg))
        usage_in, usage_out = _read_usage(sdk_msg)
        return ChatResponse(
            text=text,
            model=_get_attr(sdk_msg, "model", default=self._model),
            input_tokens=usage_in,
            output_tokens=usage_out,
            stop_reason=_get_attr(sdk_msg, "stop_reason", default=None),
        )

    def extract(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
    ) -> ExtractResponse:
        """Force the model to emit a structured object matching ``schema``.

        Implementation: register a synthetic single-tool toolset with the
        caller-provided JSON schema as ``input_schema`` and force the model
        to call it via ``tool_choice``. Parse the resulting ``tool_use`` block
        back into a plain ``dict`` carried on :class:`ExtractResponse`.
        """
        tool_def: dict[str, Any] = {
            "name": EXTRACT_TOOL_NAME,
            "description": (
                "Emit the structured object the caller requested. The "
                "schema is enforced by the surrounding adapter."
            ),
            "input_schema": schema,
        }
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": _to_sdk_messages(messages),
            "tools": [tool_def],
            "tool_choice": {"type": "tool", "name": EXTRACT_TOOL_NAME},
        }
        sdk_msg = self._client.messages.create(**kwargs)
        for block in _get_content(sdk_msg):
            if _block_type(block) == "tool_use":
                inp = _get_attr(block, "input", default=None)
                if not isinstance(inp, dict):
                    raise LLMClientError(
                        "extract: tool_use block had non-dict input "
                        f"(got {type(inp).__name__})"
                    )
                return ExtractResponse(data=inp, usage=_read_optional_usage(sdk_msg))
        raise LLMClientError(
            "extract: model returned no tool_use block; "
            f"stop_reason={_get_attr(sdk_msg, 'stop_reason', default=None)!r}"
        )

    def tool_call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> ToolCallResponse:
        """Run a tool-use round-trip with caller-supplied tool definitions."""
        kwargs: dict[str, Any] = {
            "model": self._model,
            "max_tokens": self._max_tokens,
            "messages": _to_sdk_messages(messages),
            "tools": tools,
        }
        if system is not None:
            kwargs["system"] = system

        sdk_msg = self._client.messages.create(**kwargs)
        text_parts: list[str] = []
        calls: list[ToolCall] = []
        for block in _get_content(sdk_msg):
            btype = _block_type(block)
            if btype == "text":
                text_parts.append(_get_attr(block, "text", default="") or "")
            elif btype == "tool_use":
                inp = _get_attr(block, "input", default={}) or {}
                if not isinstance(inp, dict):
                    raise LLMClientError(
                        "tool_call: tool_use block had non-dict input "
                        f"(got {type(inp).__name__})"
                    )
                calls.append(
                    ToolCall(
                        id=str(_get_attr(block, "id", default="")),
                        name=str(_get_attr(block, "name", default="")),
                        input=inp,
                    )
                )
        usage_in, usage_out = _read_usage(sdk_msg)
        return ToolCallResponse(
            text="".join(text_parts),
            tool_calls=calls,
            model=_get_attr(sdk_msg, "model", default=self._model),
            input_tokens=usage_in,
            output_tokens=usage_out,
            stop_reason=_get_attr(sdk_msg, "stop_reason", default=None),
        )


# ---------------------------------------------------------- internal helpers


def _to_sdk_messages(messages: list[Message]) -> list[dict[str, Any]]:
    """Translate our Message dataclass list into the SDK's message dict shape."""
    out: list[dict[str, Any]] = []
    for m in messages:
        out.append({"role": m.role, "content": m.content})
    return out


def _get_attr(obj: Any, name: str, *, default: Any) -> Any:
    """Attribute lookup that also tolerates dict-shaped mocks/fixtures."""
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _get_content(sdk_msg: Any) -> list[Any]:
    content = _get_attr(sdk_msg, "content", default=[]) or []
    if not isinstance(content, list):
        return []
    return list(content)


def _block_type(block: Any) -> str:
    return str(_get_attr(block, "type", default=""))


def _join_text_blocks(blocks: list[Any]) -> str:
    parts: list[str] = []
    for block in blocks:
        if _block_type(block) == "text":
            parts.append(_get_attr(block, "text", default="") or "")
    return "".join(parts)


def _read_usage(sdk_msg: Any) -> tuple[int, int]:
    usage = _get_attr(sdk_msg, "usage", default=None)
    if usage is None:
        return (0, 0)
    in_t = _get_attr(usage, "input_tokens", default=0) or 0
    out_t = _get_attr(usage, "output_tokens", default=0) or 0
    try:
        return (int(in_t), int(out_t))
    except (TypeError, ValueError):
        return (0, 0)


_MISSING = object()


def _read_optional_usage(sdk_msg: Any) -> Usage | None:
    usage = _get_attr(sdk_msg, "usage", default=None)
    if usage is None:
        return None
    in_t = _get_attr(usage, "input_tokens", default=_MISSING)
    out_t = _get_attr(usage, "output_tokens", default=_MISSING)
    if in_t is _MISSING or out_t is _MISSING:
        return None
    try:
        return Usage(input_tokens=int(in_t), output_tokens=int(out_t))
    except (TypeError, ValueError):
        return None
