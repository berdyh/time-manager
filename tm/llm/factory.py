"""Build an LLMClient for the configured backend.

Reads ``TM_LLM_BACKEND`` env var to pick between adapters; defaults to
``"anthropic"``. Backends: ``"anthropic"`` (direct API),
``"codex"`` (subprocess to OpenAI Codex CLI), ``"claude-code"``
(subprocess to Claude Code CLI in --bare --print mode).

Future backends (OpenAI direct, Ollama, OpenCode, Gemini) are tracked in
TODOS.md as v1.1+ work.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from tm.llm.errors import LLMClientError

if TYPE_CHECKING:
    from tm.llm.client import LLMClient

__all__ = ["BACKEND_ENV", "DEFAULT_BACKEND", "VALID_BACKENDS", "build_llm_client"]

BACKEND_ENV = "TM_LLM_BACKEND"
DEFAULT_BACKEND = "anthropic"
VALID_BACKENDS: frozenset[str] = frozenset({"anthropic", "codex", "claude-code"})


def build_llm_client(
    *,
    backend: str | None = None,
    model: str | None = None,
    max_tokens: int | None = None,
) -> LLMClient:
    """Construct an :class:`LLMClient` for the selected backend.

    Parameters:
        backend: explicit backend name; if ``None``, read
            ``TM_LLM_BACKEND`` env var; if that's unset, use
            :data:`DEFAULT_BACKEND` ("anthropic").
        model: optional model override. Forwarded to all adapters that
            accept a ``model`` kwarg (all of them).
        max_tokens: optional max-tokens override. Forwarded to all
            adapters (AnthropicAdapter uses it; Codex/ClaudeCode accept
            it for API symmetry but don't forward to their CLIs).

    Raises:
        LLMClientError: if ``backend`` is not in :data:`VALID_BACKENDS`,
            or if the chosen adapter cannot be constructed (e.g. CLI
            binary missing, API key not set).
    """
    if backend is None:
        backend = (
            os.environ.get(BACKEND_ENV, DEFAULT_BACKEND).strip() or DEFAULT_BACKEND
        )
    if backend not in VALID_BACKENDS:
        raise LLMClientError(
            f"invalid {BACKEND_ENV}={backend!r}; "
            f"expected one of {sorted(VALID_BACKENDS)}"
        )

    # Local imports so the factory module load doesn't pay for any one
    # adapter's import cost unless that backend is selected.
    if backend == "anthropic":
        from tm.llm.anthropic_adapter import (  # noqa: I001 — grouped local import
            DEFAULT_MAX_TOKENS as ANTHROPIC_DEFAULT_MAX,
            DEFAULT_MODEL as ANTHROPIC_DEFAULT_MODEL,
            AnthropicAdapter,
        )

        return AnthropicAdapter(
            model=model or ANTHROPIC_DEFAULT_MODEL,
            max_tokens=max_tokens if max_tokens is not None else ANTHROPIC_DEFAULT_MAX,
        )
    if backend == "codex":
        from tm.llm.codex_adapter import CodexAdapter

        return CodexAdapter(model=model, max_tokens=max_tokens)
    # backend == "claude-code"
    from tm.llm.claude_code_adapter import ClaudeCodeAdapter

    return ClaudeCodeAdapter(model=model, max_tokens=max_tokens)
