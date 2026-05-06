"""LLMClient Protocol + transport dataclasses.

The :class:`LLMClient` Protocol is the narrow surface every model adapter
must satisfy. v1 ships exactly one adapter (Anthropic, see
:mod:`tm.llm.anthropic_adapter`); other providers are deferred.

Three operations are exposed:

* :meth:`LLMClient.chat` — free-form chat completion. Returns a
  :class:`ChatResponse` carrying the assistant text plus token usage so the
  cost meter can record the call.
* :meth:`LLMClient.extract` — structured extraction against a JSON schema
  (provider-side tool-use under the hood). Returns the parsed dict plus
  token usage when the adapter can compute it.
* :meth:`LLMClient.tool_call` — generic tool-use round-trip. Returns a
  :class:`ToolCallResponse` with zero or more parsed
  :class:`ToolCall` blocks plus any free-form text.

Inputs use plain :mod:`dataclasses`; we deliberately avoid pulling Pydantic
into the runtime dep set just for transport types. The shapes are
provider-neutral so future adapters can map them onto their own SDKs.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

__all__ = [
    "ChatResponse",
    "ExtractResponse",
    "LLMClient",
    "Message",
    "ToolCall",
    "ToolCallResponse",
    "Usage",
]


# --------------------------------------------------------------------- inputs


@dataclass(frozen=True)
class Message:
    """One turn in a chat transcript.

    ``role`` is one of ``"user"`` or ``"assistant"`` — system prompts are
    passed separately to :meth:`LLMClient.chat` because the Anthropic SDK
    accepts them as a top-level parameter rather than a transcript role.
    """

    role: str
    content: str


# -------------------------------------------------------------------- outputs


@dataclass(frozen=True)
class ChatResponse:
    """Result of a :meth:`LLMClient.chat` call.

    Attributes:
        text: concatenated assistant text content.
        model: model id reported by the provider (echoed for logging).
        input_tokens: prompt-side token count reported by the provider.
        output_tokens: completion-side token count reported by the provider.
        stop_reason: provider stop reason (e.g. ``"end_turn"``), if any.
    """

    text: str
    model: str
    input_tokens: int
    output_tokens: int
    stop_reason: str | None = None


@dataclass(frozen=True)
class Usage:
    """Token usage reported for an LLM call."""

    input_tokens: int
    output_tokens: int


@dataclass(frozen=True)
class ExtractResponse:
    """Result of a :meth:`LLMClient.extract` call.

    ``data`` is the structured object parsed from provider-side tool-use.
    ``usage`` is ``None`` when the adapter cannot determine token counts.
    """

    data: dict[str, Any]
    usage: Usage | None


@dataclass(frozen=True)
class ToolCall:
    """One tool_use block parsed out of a tool-call response."""

    id: str
    name: str
    input: dict[str, Any]


@dataclass(frozen=True)
class ToolCallResponse:
    """Result of a :meth:`LLMClient.tool_call` call.

    ``tool_calls`` may be empty if the model elected to answer directly; in
    that case ``text`` holds the assistant's free-form reply.
    """

    text: str
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    stop_reason: str | None = None


# ------------------------------------------------------------------- protocol


@runtime_checkable
class LLMClient(Protocol):
    """Narrow surface every LLM adapter must satisfy.

    Implementations live under :mod:`tm.llm`; v1 ships only
    :class:`tm.llm.anthropic_adapter.AnthropicAdapter`.
    """

    def chat(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
    ) -> ChatResponse:
        """Run a chat completion. ``system`` is passed via the SDK's dedicated
        system-prompt parameter rather than encoded in the transcript.
        """
        ...

    def extract(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
    ) -> ExtractResponse:
        """Run structured extraction. ``schema`` is a JSON schema describing
        the expected output object; the adapter is responsible for routing
        this through provider-side tool-use and parsing the result back into
        an :class:`ExtractResponse`.
        """
        ...

    def tool_call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> ToolCallResponse:
        """Run a tool-use round trip. ``tools`` is a list of tool definitions
        in the provider's native shape (Anthropic-style ``{name,
        description, input_schema}`` dicts for the v1 adapter).
        """
        ...
