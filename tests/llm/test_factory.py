"""Tests for :func:`tm.llm.factory.build_llm_client`.

The factory uses local imports inside each branch so that picking one
backend doesn't pay for any other adapter's import cost. The tests
therefore patch the adapter classes at their *source* module path
(e.g. ``tm.llm.codex_adapter.CodexAdapter``) — the local imports
re-resolve the binding on every call, so patching the source module is
what the factory actually sees.

No live SDK calls or subprocess invocations happen: each test either
patches the adapter class to a sentinel or stubs out the subprocess
resolver (``shutil.which``) and ``TM_LLM_API_KEY``.
"""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from tm.llm.anthropic_adapter import (
    ANTHROPIC_API_KEY_ENV,
    DEFAULT_MAX_TOKENS,
    DEFAULT_MODEL,
    AnthropicAdapter,
)
from tm.llm.claude_code_adapter import ClaudeCodeAdapter
from tm.llm.codex_adapter import CodexAdapter
from tm.llm.errors import LLMClientError
from tm.llm.factory import (
    BACKEND_ENV,
    DEFAULT_BACKEND,
    VALID_BACKENDS,
    build_llm_client,
)

# --------------------------------------------------------------------- fixtures


@pytest.fixture
def _set_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Set TM_LLM_API_KEY so AnthropicAdapter construction succeeds."""
    monkeypatch.setenv(ANTHROPIC_API_KEY_ENV, "test-key-deadbeef")


@pytest.fixture
def _stub_codex_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which("codex")`` return a fake path."""
    monkeypatch.setattr(
        "tm.llm.codex_adapter.shutil.which",
        lambda name: "/usr/bin/true" if name == "codex" else None,
    )


@pytest.fixture
def _stub_claude_binary(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make ``shutil.which("claude")`` return a fake path."""
    monkeypatch.setattr(
        "tm.llm.claude_code_adapter.shutil.which",
        lambda name: "/usr/bin/true" if name == "claude" else None,
    )


# ---------------------------------------------------------------- module shape


def test_module_exports_constants() -> None:
    assert BACKEND_ENV == "TM_LLM_BACKEND"
    assert DEFAULT_BACKEND == "anthropic"
    assert VALID_BACKENDS == frozenset({"anthropic", "codex", "claude-code"})


# ---------------------------------------------------------------- defaulting


def test_default_backend_is_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """No env var, no backend arg -> AnthropicAdapter."""
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    client = build_llm_client()
    assert isinstance(client, AnthropicAdapter)


def test_empty_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """TM_LLM_BACKEND='' -> anthropic (the ``... or DEFAULT_BACKEND`` branch)."""
    monkeypatch.setenv(BACKEND_ENV, "")
    client = build_llm_client()
    assert isinstance(client, AnthropicAdapter)


def test_whitespace_env_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """TM_LLM_BACKEND='   ' (whitespace-only) -> anthropic."""
    monkeypatch.setenv(BACKEND_ENV, "   ")
    client = build_llm_client()
    assert isinstance(client, AnthropicAdapter)


# ------------------------------------------------------------------ selection


def test_env_var_codex_selects_codex(
    monkeypatch: pytest.MonkeyPatch,
    _stub_codex_binary: None,
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "codex")
    client = build_llm_client()
    assert isinstance(client, CodexAdapter)


def test_env_var_claude_code_selects_claude_code(
    monkeypatch: pytest.MonkeyPatch,
    _stub_claude_binary: None,
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "claude-code")
    client = build_llm_client()
    assert isinstance(client, ClaudeCodeAdapter)


def test_explicit_backend_arg_overrides_env(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """``build_llm_client(backend="anthropic")`` wins over env=codex."""
    monkeypatch.setenv(BACKEND_ENV, "codex")
    client = build_llm_client(backend="anthropic")
    assert isinstance(client, AnthropicAdapter)


# ----------------------------------------------------------------- validation


def test_invalid_backend_raises_llm_client_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "grok")
    with pytest.raises(LLMClientError) as exc_info:
        build_llm_client()
    msg = str(exc_info.value)
    assert "invalid" in msg
    assert "grok" in msg
    # Error message lists the valid set in sorted order.
    assert "anthropic" in msg
    assert "claude-code" in msg
    assert "codex" in msg


def test_unknown_env_value_includes_valid_set_in_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(BACKEND_ENV, "ollama")
    with pytest.raises(LLMClientError) as exc_info:
        build_llm_client()
    # The factory formats sorted(VALID_BACKENDS) into the message.
    assert "['anthropic', 'claude-code', 'codex']" in str(exc_info.value)


def test_explicit_invalid_backend_arg_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit invalid arg is validated even when env is valid."""
    monkeypatch.setenv(BACKEND_ENV, "anthropic")
    with pytest.raises(LLMClientError, match="invalid TM_LLM_BACKEND='gemini'"):
        build_llm_client(backend="gemini")


# ----------------------------------------------------------- kwarg forwarding


def test_model_and_max_tokens_forwarded_to_anthropic(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """The anthropic branch forwards both ``model`` and ``max_tokens``."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(name="AnthropicAdapter")

    # Local import inside the factory resolves the binding via the source
    # module, so patching there is what the factory sees.
    monkeypatch.setattr("tm.llm.anthropic_adapter.AnthropicAdapter", _capture)

    build_llm_client(backend="anthropic", model="claude-sonnet-4-6", max_tokens=2048)
    assert captured == {"model": "claude-sonnet-4-6", "max_tokens": 2048}


def test_model_forwarded_to_codex(monkeypatch: pytest.MonkeyPatch) -> None:
    """The codex branch forwards ``model`` and ``max_tokens`` to CodexAdapter."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(name="CodexAdapter")

    monkeypatch.setattr("tm.llm.codex_adapter.CodexAdapter", _capture)

    build_llm_client(backend="codex", model="o4-mini", max_tokens=4096)
    assert captured == {"model": "o4-mini", "max_tokens": 4096}


def test_model_forwarded_to_claude_code(monkeypatch: pytest.MonkeyPatch) -> None:
    """The claude-code branch forwards ``model`` and ``max_tokens``."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(name="ClaudeCodeAdapter")

    monkeypatch.setattr("tm.llm.claude_code_adapter.ClaudeCodeAdapter", _capture)

    build_llm_client(backend="claude-code", model="sonnet", max_tokens=8192)
    assert captured == {"model": "sonnet", "max_tokens": 8192}


def test_max_tokens_none_uses_anthropic_default(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """When max_tokens=None, the anthropic branch substitutes DEFAULT_MAX_TOKENS."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(name="AnthropicAdapter")

    monkeypatch.setattr("tm.llm.anthropic_adapter.AnthropicAdapter", _capture)

    build_llm_client(backend="anthropic")
    assert captured["max_tokens"] == DEFAULT_MAX_TOKENS
    assert captured["model"] == DEFAULT_MODEL


def test_model_none_uses_anthropic_default(
    monkeypatch: pytest.MonkeyPatch,
    _set_api_key: None,
) -> None:
    """When model=None, the anthropic branch substitutes DEFAULT_MODEL."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(name="AnthropicAdapter")

    monkeypatch.setattr("tm.llm.anthropic_adapter.AnthropicAdapter", _capture)

    build_llm_client(backend="anthropic", max_tokens=500)
    assert captured["model"] == DEFAULT_MODEL
    assert captured["max_tokens"] == 500


def test_max_tokens_none_forwarded_as_none_to_codex(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Codex accepts max_tokens=None for API symmetry; factory forwards as-is."""
    captured: dict[str, object] = {}

    def _capture(**kwargs: object) -> object:
        captured.update(kwargs)
        return MagicMock(name="CodexAdapter")

    monkeypatch.setattr("tm.llm.codex_adapter.CodexAdapter", _capture)

    build_llm_client(backend="codex")
    assert captured == {"model": None, "max_tokens": None}


# ----------------------------------------------------------- propagation of init errors


def test_anthropic_missing_api_key_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """AnthropicAdapter raises LLMClientError when API key unset; factory propagates."""
    monkeypatch.delenv(ANTHROPIC_API_KEY_ENV, raising=False)
    monkeypatch.delenv(BACKEND_ENV, raising=False)
    with pytest.raises(LLMClientError, match=ANTHROPIC_API_KEY_ENV):
        build_llm_client()


def test_codex_missing_binary_propagates(monkeypatch: pytest.MonkeyPatch) -> None:
    """CodexAdapter raises LLMClientError when codex isn't on PATH; propagated."""
    monkeypatch.setattr("tm.llm.codex_adapter.shutil.which", lambda _: None)
    with pytest.raises(LLMClientError, match="codex CLI not found"):
        build_llm_client(backend="codex")


def test_claude_code_missing_binary_propagates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ClaudeCodeAdapter raises LLMClientError when claude isn't on PATH."""
    monkeypatch.setattr("tm.llm.claude_code_adapter.shutil.which", lambda _: None)
    with pytest.raises(LLMClientError, match="claude CLI not found"):
        build_llm_client(backend="claude-code")
