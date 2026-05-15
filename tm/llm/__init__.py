"""LLM client foundation: narrow Protocol + Anthropic adapter + cost meter."""

from __future__ import annotations

from tm.llm.anthropic_adapter import AnthropicAdapter
from tm.llm.claude_code_adapter import ClaudeCodeAdapter
from tm.llm.client import (
    ChatResponse,
    ExtractResponse,
    LLMClient,
    Message,
    ToolCall,
    ToolCallResponse,
    Usage,
)
from tm.llm.codex_adapter import CodexAdapter
from tm.llm.cost_meter import CostMeter
from tm.llm.errors import CostCapExceeded, LLMClientError
from tm.llm.factory import BACKEND_ENV, VALID_BACKENDS, build_llm_client

__all__ = [
    "BACKEND_ENV",
    "VALID_BACKENDS",
    "AnthropicAdapter",
    "ChatResponse",
    "ClaudeCodeAdapter",
    "CodexAdapter",
    "CostCapExceeded",
    "CostMeter",
    "ExtractResponse",
    "LLMClient",
    "LLMClientError",
    "Message",
    "ToolCall",
    "ToolCallResponse",
    "Usage",
    "build_llm_client",
]
