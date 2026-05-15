"""Subprocess-based :class:`tm.llm.client.LLMClient` over the Claude Code CLI.

This adapter routes calls through the system ``claude`` binary (``claude
--bare --print --output-format json``) rather than an in-process SDK. It
is the second subprocess adapter in the multi-backend LLM routing wave
described in ``docs/designs/temporal-knowledge-graph.md`` and ships
alongside :class:`tm.llm.codex_adapter.CodexAdapter`.

Approach
--------

* The claude binary is resolved at construction time via
  :func:`shutil.which` (or via an explicit ``claude_binary`` kwarg for
  tests). A missing binary raises :class:`tm.llm.errors.LLMClientError`
  up-front so callers fail fast.
* Each call invokes ``claude --bare --print --output-format json
  --allow-dangerously-skip-permissions`` with optional ``--model``,
  ``--append-system-prompt``, and ``--json-schema`` flags. We use
  :func:`subprocess.run` with ``capture_output=True`` and ``text=True``.
  Unlike the codex CLI, the claude CLI emits a *single* JSON object on
  stdout when ``--output-format json`` is set (not a streaming JSONL
  event log), so parsing is one :func:`json.loads` call.
* The ``--bare`` flag is REQUIRED: it disables hooks, plugins, auto-memory,
  ``CLAUDE.md`` auto-discovery, OAuth/keychain reads, and other per-user
  side effects that would otherwise leak into the subprocess. With
  ``--bare`` set, authentication is strictly via the ``ANTHROPIC_API_KEY``
  environment variable.
* The wire timeout (``timeout_s``) bounds the subprocess; on
  :class:`subprocess.TimeoutExpired` the adapter raises
  :class:`LLMClientError` with a descriptive message.

Authentication bridge
---------------------

The rest of ``tm`` reads the API key from ``TM_LLM_API_KEY`` (canonical
name; see :data:`tm.llm.anthropic_adapter.ANTHROPIC_API_KEY_ENV`), but
``claude --bare`` only reads ``ANTHROPIC_API_KEY``. To keep configuration
single-sourced, the adapter builds the subprocess env by copying the
current ``os.environ`` and **mirroring** ``TM_LLM_API_KEY`` into
``ANTHROPIC_API_KEY`` for the child only. Callers therefore set only
``TM_LLM_API_KEY``; the bridge is transparent.

If ``TM_LLM_API_KEY`` is not set at call time we raise
:class:`LLMClientError`. We do this at call time rather than construction
because the env can change between construction and use (e.g. tests that
unset the variable after wiring up an adapter).

Prompt serialization
--------------------

Claude --print accepts a single positional prompt argument. We serialize
:class:`tm.llm.client.Message` lists into one string using ``[user]`` /
``[assistant]`` role markers — the same scheme as
:class:`tm.llm.codex_adapter.CodexAdapter` — so downstream agents see a
uniform on-the-wire shape across subprocess backends. The ``system``
parameter (when given) is passed via ``--append-system-prompt`` rather
than baked into the positional prompt, which lets claude apply it to its
own default system-prompt scaffold.

Output shape
------------

With ``--output-format json``, claude emits a single JSON object on
stdout. The adapter reads:

* ``result`` — the assistant's response text (or a JSON-encoded object
  when ``--json-schema`` is supplied for :meth:`extract`).
* ``model`` — the underlying model id, echoed into ``ChatResponse.model``.
* ``usage.input_tokens`` and ``usage.output_tokens`` — token counts.
* ``is_error`` — when ``true`` we raise :class:`LLMClientError` with the
  ``subtype``/``result`` detail.

Extra fields (``session_id``, ``total_cost_usd``, etc.) are ignored.

Tool-call limitation
--------------------

The claude ``--print --output-format json`` surface returns only the
final assistant text — it does not expose intermediate ``tool_use``
content blocks the way the Anthropic SDK does.
:meth:`ClaudeCodeAdapter.tool_call` is therefore implemented as a stub
that embeds the tool definitions in the prompt for documentation
purposes and always returns ``tool_calls=[]`` (same shape as
:class:`CodexAdapter.tool_call`). No current ``tm.agents.*`` caller
exercises ``tool_call`` on claude, so this is forward-looking and
explicitly documented as a limitation here.

The ``max_tokens`` constructor parameter is accepted for API symmetry
with :class:`AnthropicAdapter` but is NOT forwarded to claude (the CLI
exposes ``--max-budget-usd`` but no token-level cap). The parameter is
retained so callers can treat all adapters uniformly.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from typing import Any

from tm.llm.client import (
    ChatResponse,
    ExtractResponse,
    Message,
    ToolCallResponse,
    Usage,
)
from tm.llm.errors import LLMClientError

__all__ = [
    "ANTHROPIC_API_KEY_ENV",
    "DEFAULT_TIMEOUT_S",
    "TM_LLM_API_KEY_ENV",
    "ClaudeCodeAdapter",
]

# Canonical env var holding the API key. Mirrors
# :data:`tm.llm.anthropic_adapter.ANTHROPIC_API_KEY_ENV` — we do NOT import
# from that module to keep this adapter independent of the SDK-backed one.
TM_LLM_API_KEY_ENV = "TM_LLM_API_KEY"

# The env var that ``claude --bare`` reads for auth. Set by the adapter
# from ``TM_LLM_API_KEY`` for the child subprocess only.
ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# Default wall-clock timeout for a single claude invocation, in seconds.
DEFAULT_TIMEOUT_S = 300.0


class ClaudeCodeAdapter:
    """Subprocess-based :class:`LLMClient` wrapping ``claude --bare --print``.

    Parameters:
        model: optional claude model alias or full name passed via
            ``--model`` (e.g. ``"sonnet"``, ``"opus"``,
            ``"claude-sonnet-4-6"``). When ``None``, claude picks its
            configured default.
        max_tokens: accepted for API symmetry with
            :class:`AnthropicAdapter` but NOT forwarded to claude (no
            token-level cap on the CLI surface; ``--max-budget-usd``
            exists but is dollar-denominated).
        timeout_s: wall-clock timeout for each ``subprocess.run`` call.
            On expiry the adapter raises :class:`LLMClientError`.
        claude_binary: explicit path to the claude binary; if ``None``,
            the adapter resolves it via :func:`shutil.which` at
            construction time and raises :class:`LLMClientError` if
            missing.

    Authentication:
        The adapter mirrors ``TM_LLM_API_KEY`` into ``ANTHROPIC_API_KEY``
        for the child subprocess only (see module docstring). If
        ``TM_LLM_API_KEY`` is unset at call time, :class:`LLMClientError`
        is raised.

    Limitations:
        :meth:`tool_call` returns ``tool_calls=[]`` — the
        ``--print --output-format json`` surface does not expose
        intermediate ``tool_use`` content blocks. See module docstring.

    Output shape:
        The adapter assumes claude's ``--output-format json`` payload is
        a single top-level JSON object with ``result`` (string),
        ``model`` (string), ``usage.input_tokens`` (int),
        ``usage.output_tokens`` (int), and ``is_error`` (bool) fields.
        Extra fields are ignored.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        claude_binary: str | None = None,
    ) -> None:
        self._model = model
        # max_tokens stored for introspection but not forwarded to claude.
        self._max_tokens = max_tokens
        self._timeout_s = float(timeout_s)
        if claude_binary is None:
            resolved = shutil.which("claude")
            if resolved is None:
                raise LLMClientError("claude CLI not found on PATH")
            self._claude_binary = resolved
        else:
            self._claude_binary = claude_binary

    # ------------------------------------------------------------------- API

    def chat(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
    ) -> ChatResponse:
        """Run a vanilla chat completion via ``claude --bare --print``."""
        prompt = _serialize_messages(messages)
        payload = self._invoke_claude(prompt, system=system, json_schema=None)
        text, model, in_t, out_t = _read_chat_fields(payload)
        return ChatResponse(
            text=text,
            model=model,
            input_tokens=in_t,
            output_tokens=out_t,
            stop_reason="end_turn",
        )

    def extract(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
    ) -> ExtractResponse:
        """Force the model to emit a JSON object matching ``schema``.

        The schema is forwarded via ``--json-schema`` so claude does
        structured-output validation server-side; the ``result`` field
        in the response is then a JSON-encoded string of the structured
        object. We :func:`json.loads` it; parse failures raise
        :class:`LLMClientError`.
        """
        schema_json = json.dumps(schema, separators=(",", ":"), sort_keys=True)
        prompt = _serialize_messages(messages)
        payload = self._invoke_claude(prompt, system=None, json_schema=schema_json)
        text, _model, in_t, out_t = _read_chat_fields(payload)
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            head = text[:200]
            raise LLMClientError(f"claude returned non-JSON result: {head}") from exc
        if not isinstance(data, dict):
            head = text[:200]
            raise LLMClientError(f"claude returned non-JSON result: {head}")
        usage = Usage(input_tokens=in_t, output_tokens=out_t)
        return ExtractResponse(data=data, usage=usage)

    def tool_call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> ToolCallResponse:
        """Stub tool-call: returns the assistant text with ``tool_calls=[]``.

        The ``claude --print --output-format json`` surface does not
        expose intermediate ``tool_use`` content blocks. We embed the
        tool definitions in the system prompt as informational context
        but always return an empty ``tool_calls`` list. This method
        exists to satisfy the :class:`LLMClient` Protocol; no current
        ``tm.agents.*`` caller uses it on claude.
        """
        tools_json = json.dumps(tools, separators=(",", ":"), sort_keys=True)
        tools_block = (
            "Available tools (informational — the claude CLI does not "
            f"natively invoke tools via --print):\n{tools_json}"
        )
        if system is not None:
            effective_system = f"{system}\n\n{tools_block}"
        else:
            effective_system = tools_block
        prompt = _serialize_messages(messages)
        payload = self._invoke_claude(prompt, system=effective_system, json_schema=None)
        text, model, in_t, out_t = _read_chat_fields(payload)
        return ToolCallResponse(
            text=text,
            tool_calls=[],
            model=model,
            input_tokens=in_t,
            output_tokens=out_t,
            stop_reason="end_turn",
        )

    # -------------------------------------------------------- internals

    def _build_argv(
        self,
        prompt: str,
        *,
        system: str | None,
        json_schema: str | None,
    ) -> list[str]:
        argv = [
            self._claude_binary,
            "--bare",
            "--print",
            "--output-format",
            "json",
            "--allow-dangerously-skip-permissions",
        ]
        if self._model is not None:
            argv.extend(["--model", self._model])
        if system is not None:
            argv.extend(["--append-system-prompt", system])
        if json_schema is not None:
            argv.extend(["--json-schema", json_schema])
        argv.append(prompt)
        return argv

    def _build_env(self) -> dict[str, str]:
        env = os.environ.copy()
        api_key = env.get(TM_LLM_API_KEY_ENV)
        if not api_key:
            raise LLMClientError(f"{TM_LLM_API_KEY_ENV} not set")
        env[ANTHROPIC_API_KEY_ENV] = api_key
        return env

    def _invoke_claude(
        self,
        prompt: str,
        *,
        system: str | None,
        json_schema: str | None,
    ) -> dict[str, Any]:
        env = self._build_env()
        argv = self._build_argv(prompt, system=system, json_schema=json_schema)
        try:
            completed = subprocess.run(  # noqa: S603 — argv is a controlled list
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                env=env,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMClientError(f"claude timed out after {self._timeout_s}s") from exc
        if completed.returncode != 0:
            stderr_tail = (completed.stderr or "").strip()[-500:]
            raise LLMClientError(f"claude exited {completed.returncode}: {stderr_tail}")
        stdout = completed.stdout or ""
        try:
            payload = json.loads(stdout)
        except json.JSONDecodeError as exc:
            head = stdout[:200]
            raise LLMClientError(f"claude returned non-JSON stdout: {head}") from exc
        if not isinstance(payload, dict):
            head = stdout[:200]
            raise LLMClientError(
                f"claude returned non-object JSON stdout (expected object, "
                f"got {type(payload).__name__}): {head}"
            )
        if payload.get("is_error"):
            detail = payload.get("result") or payload.get("subtype") or "unknown"
            raise LLMClientError(f"claude reported error: {detail}")
        return payload


# ---------------------------------------------------------- internal helpers


def _serialize_messages(messages: list[Message]) -> str:
    """Render a Message list into a single claude prompt string.

    Each turn is preceded by a ``[role]`` marker; turns are joined by a
    blank line. ``system`` is NOT included here — it is passed to the
    CLI via ``--append-system-prompt`` instead.
    """
    parts: list[str] = []
    for msg in messages:
        parts.append(f"[{msg.role}]\n{msg.content}")
    return "\n\n".join(parts)


def _read_chat_fields(payload: dict[str, Any]) -> tuple[str, str, int, int]:
    """Extract ``(result_text, model, input_tokens, output_tokens)``.

    The ``result`` field is required; missing/non-string values raise
    :class:`LLMClientError`. ``model`` and the usage fields default to
    sensible empty values when absent, since claude may omit them in
    edge cases (e.g. cached/short responses).
    """
    result = payload.get("result")
    if not isinstance(result, str):
        keys = sorted(payload.keys())
        raise LLMClientError(
            "claude response missing 'result' string field "
            "(expected top-level shape {'result': str, 'model': str, "
            "'usage': {'input_tokens': int, 'output_tokens': int}, "
            f"'is_error': bool}}; got keys: {keys})"
        )
    model = payload.get("model")
    if not isinstance(model, str):
        model = ""
    usage = payload.get("usage")
    in_t = 0
    out_t = 0
    if isinstance(usage, dict):
        try:
            in_t = int(usage.get("input_tokens", 0) or 0)
            out_t = int(usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            in_t = 0
            out_t = 0
    return result, model, in_t, out_t
