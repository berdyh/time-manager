"""Subprocess-based :class:`tm.llm.client.LLMClient` over the OpenAI Codex CLI.

This adapter routes calls through the system ``codex`` binary (``codex
exec --json``) rather than an in-process SDK. It is the second adapter to
ship after :class:`tm.llm.anthropic_adapter.AnthropicAdapter` and forms part
of the multi-backend LLM routing wave described in
``docs/designs/temporal-knowledge-graph.md``.

Approach
--------

* The codex binary is resolved at construction time via :func:`shutil.which`
  (or via an explicit ``codex_binary`` kwarg for tests). A missing binary
  raises :class:`tm.llm.errors.LLMClientError` up-front so callers fail fast.
* Each call invokes ``codex exec --json --skip-git-repo-check -s read-only``
  with the model reasoning effort overridden via ``-c``. We use
  :func:`subprocess.run` with ``capture_output=True`` and ``text=True`` —
  NOT :class:`subprocess.Popen` with streaming. Codex emits its full JSONL
  event stream to stdout; we parse the buffered output after the process
  exits. This keeps the adapter simple and easy to unit test (the only
  external surface is :func:`subprocess.run`, which the tests monkeypatch).
* The wire timeout (``timeout_s``) bounds the subprocess; on
  :class:`subprocess.TimeoutExpired` the adapter raises
  :class:`LLMClientError` with a descriptive message.

Prompt serialization
--------------------

Codex is a single-prompt agent — it has no native multi-turn transcript on
the CLI. We serialize :class:`tm.llm.client.Message` lists into a single
prompt string by emitting role markers like ``[user]`` / ``[assistant]``
between blocks. If a ``system`` parameter is supplied, it is prepended as
``[system]\n{system}\n\n`` before the messages. The model is instructed
(via the prompt itself) to reply as the assistant.

Tool-call limitation
--------------------

The codex CLI has no native tool-use semantics analogous to Anthropic's
``tool_use`` content blocks. :meth:`CodexAdapter.tool_call` is implemented
as a stub that includes the tool definitions in the prompt for context but
always returns ``tool_calls=[]`` — only free-form text. No current
``tm.agents.*`` caller exercises ``tool_call`` on codex, so this is
forward-looking and explicitly documented as a limitation here.

The ``max_tokens`` constructor parameter is accepted for API symmetry with
:class:`AnthropicAdapter` but is NOT passed to codex (which exposes no
direct equivalent on the CLI). The parameter is retained so callers can
treat both adapters uniformly.
"""

from __future__ import annotations

import json
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
    "CODEX_MODEL_LABEL",
    "DEFAULT_REASONING_EFFORT",
    "DEFAULT_TIMEOUT_S",
    "VALID_REASONING_EFFORTS",
    "CodexAdapter",
]

# Label reported back as ``ChatResponse.model`` — codex doesn't surface the
# underlying model id on the JSONL stream consistently, so we use a fixed
# token. Callers log this for observability.
CODEX_MODEL_LABEL = "codex"

# Codex reasoning-effort tier passed via ``-c model_reasoning_effort=...``.
VALID_REASONING_EFFORTS: frozenset[str] = frozenset({"low", "medium", "high", "xhigh"})
DEFAULT_REASONING_EFFORT = "medium"

# Default wall-clock timeout for a single codex invocation, in seconds.
DEFAULT_TIMEOUT_S = 300.0


class CodexAdapter:
    """Subprocess-based :class:`LLMClient` wrapping ``codex exec --json``.

    Parameters:
        model: optional codex model id passed via ``-m``; when ``None``,
            codex picks its configured default.
        max_tokens: accepted for API symmetry with
            :class:`AnthropicAdapter` but NOT forwarded to codex (which
            exposes no direct equivalent on the CLI).
        reasoning_effort: one of ``"low"``, ``"medium"``, ``"high"``,
            ``"xhigh"``. Forwarded as ``-c
            model_reasoning_effort=<value>``.
        timeout_s: wall-clock timeout for each ``subprocess.run`` call.
            On expiry the adapter raises :class:`LLMClientError`.
        codex_binary: explicit path to the codex binary; if ``None``, the
            adapter resolves it via :func:`shutil.which` at construction
            time and raises :class:`LLMClientError` if missing.

    Limitations:
        :meth:`tool_call` returns ``tool_calls=[]`` — codex has no native
        tool-use semantics on the CLI. See module docstring.
    """

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        reasoning_effort: str = DEFAULT_REASONING_EFFORT,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        codex_binary: str | None = None,
    ) -> None:
        if reasoning_effort not in VALID_REASONING_EFFORTS:
            raise LLMClientError(
                f"invalid reasoning_effort={reasoning_effort!r}; "
                f"expected one of {sorted(VALID_REASONING_EFFORTS)}"
            )
        self._model = model
        # max_tokens stored for introspection but not forwarded to codex.
        self._max_tokens = max_tokens
        self._reasoning_effort = reasoning_effort
        self._timeout_s = float(timeout_s)
        if codex_binary is None:
            resolved = shutil.which("codex")
            if resolved is None:
                raise LLMClientError("codex CLI not found on PATH")
            self._codex_binary = resolved
        else:
            self._codex_binary = codex_binary

    # ------------------------------------------------------------------- API

    def chat(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
    ) -> ChatResponse:
        """Run a vanilla chat completion via ``codex exec --json``."""
        prompt = _serialize_messages(messages, system=system)
        events = self._invoke_codex(prompt)
        text = _last_agent_message(events)
        if text is None:
            raise LLMClientError("codex produced no assistant message")
        usage_in, usage_out = _read_usage(events)
        return ChatResponse(
            text=text,
            model=CODEX_MODEL_LABEL,
            input_tokens=usage_in,
            output_tokens=usage_out,
            stop_reason="end_turn",
        )

    def extract(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
    ) -> ExtractResponse:
        """Force the model to emit a JSON object matching ``schema``.

        We have no codex-side tool-use, so we instead embed the schema in
        the prompt and instruct the model to reply with a single JSON
        object. The last ``agent_message`` event is parsed via
        :func:`json.loads`; parse failures raise :class:`LLMClientError`.
        """
        schema_json = json.dumps(schema, separators=(",", ":"), sort_keys=True)
        convo = _serialize_messages(messages, system=None)
        prompt = (
            "You must respond with a single JSON object matching this "
            "schema EXACTLY (no markdown, no prose, just the JSON):\n\n"
            f"{schema_json}\n\n"
            "Conversation:\n"
            f"{convo}"
        )
        events = self._invoke_codex(prompt)
        text = _last_agent_message(events)
        if text is None:
            raise LLMClientError("codex produced no assistant message")
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            head = text[:200]
            raise LLMClientError(f"codex returned non-JSON: {head}") from exc
        if not isinstance(data, dict):
            head = text[:200]
            raise LLMClientError(f"codex returned non-JSON: {head}")
        usage_in, usage_out = _read_usage(events)
        usage = Usage(input_tokens=usage_in, output_tokens=usage_out)
        return ExtractResponse(data=data, usage=usage)

    def tool_call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> ToolCallResponse:
        """Stub tool-call: returns the assistant text with ``tool_calls=[]``.

        Codex's CLI has no native tool-use semantics. We include the tool
        definitions in the prompt as informational context but always
        return an empty ``tool_calls`` list. This method exists to satisfy
        the :class:`LLMClient` Protocol; no current ``tm.agents.*`` caller
        uses it on codex.
        """
        tools_json = json.dumps(tools, separators=(",", ":"), sort_keys=True)
        tools_block = (
            "Available tools (informational — codex does not natively "
            f"invoke tools on the CLI):\n{tools_json}\n\n"
        )
        if system is not None:
            effective_system = f"{system}\n\n{tools_block}"
        else:
            effective_system = tools_block
        prompt = _serialize_messages(messages, system=effective_system)
        events = self._invoke_codex(prompt)
        text = _last_agent_message(events)
        if text is None:
            raise LLMClientError("codex produced no assistant message")
        usage_in, usage_out = _read_usage(events)
        return ToolCallResponse(
            text=text,
            tool_calls=[],
            model=CODEX_MODEL_LABEL,
            input_tokens=usage_in,
            output_tokens=usage_out,
            stop_reason="end_turn",
        )

    # -------------------------------------------------------- internals

    def _build_argv(self, prompt: str) -> list[str]:
        argv = [
            self._codex_binary,
            "exec",
            "--json",
            "--skip-git-repo-check",
            "-s",
            "read-only",
            "-c",
            f'model_reasoning_effort="{self._reasoning_effort}"',
        ]
        if self._model is not None:
            argv.extend(["-m", self._model])
        argv.append(prompt)
        return argv

    def _invoke_codex(self, prompt: str) -> list[dict[str, Any]]:
        argv = self._build_argv(prompt)
        try:
            completed = subprocess.run(  # noqa: S603 — argv is a controlled list
                argv,
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMClientError(f"codex timed out after {self._timeout_s}s") from exc
        if completed.returncode != 0:
            stderr_tail = (completed.stderr or "").strip()[-500:]
            raise LLMClientError(f"codex exited {completed.returncode}: {stderr_tail}")
        return _parse_jsonl(completed.stdout or "")


# ---------------------------------------------------------- internal helpers


def _serialize_messages(
    messages: list[Message],
    *,
    system: str | None,
) -> str:
    """Render a Message list into a single codex prompt string.

    Each turn is preceded by a ``[role]`` marker; turns are joined by a
    blank line. The ``system`` parameter (if any) is rendered as a
    ``[system]`` block prepended to the conversation.
    """
    parts: list[str] = []
    if system is not None:
        parts.append(f"[system]\n{system}")
    for msg in messages:
        parts.append(f"[{msg.role}]\n{msg.content}")
    return "\n\n".join(parts)


def _parse_jsonl(stdout: str) -> list[dict[str, Any]]:
    """Parse newline-delimited JSON events from codex stdout.

    Lines that don't parse as JSON or that aren't JSON objects are
    silently skipped — codex occasionally prefixes the stream with
    human-readable banners.
    """
    events: list[dict[str, Any]] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        try:
            obj = json.loads(stripped)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _last_agent_message(events: list[dict[str, Any]]) -> str | None:
    """Return the text of the LAST ``item.completed`` agent_message event."""
    last: str | None = None
    for event in events:
        if event.get("type") != "item.completed":
            continue
        item = event.get("item")
        if not isinstance(item, dict):
            continue
        if item.get("type") != "agent_message":
            continue
        text = item.get("text")
        if isinstance(text, str):
            last = text
    return last


def _read_usage(events: list[dict[str, Any]]) -> tuple[int, int]:
    """Extract input/output token counts from the last ``turn.completed``."""
    in_t = 0
    out_t = 0
    for event in events:
        if event.get("type") != "turn.completed":
            continue
        usage = event.get("usage")
        if not isinstance(usage, dict):
            continue
        try:
            in_t = int(usage.get("input_tokens", 0) or 0)
            out_t = int(usage.get("output_tokens", 0) or 0)
        except (TypeError, ValueError):
            in_t = 0
            out_t = 0
    return in_t, out_t
