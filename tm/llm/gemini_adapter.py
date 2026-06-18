"""Subprocess LLMClient adapter for the Gemini CLI."""

from __future__ import annotations

import json
import shutil
import subprocess
from typing import Any

from tm.llm._subprocess_json import (
    extract_json_object,
    read_text_field,
    serialize_messages,
)
from tm.llm.client import (
    ChatResponse,
    ExtractResponse,
    Message,
    ToolCallResponse,
    Usage,
)
from tm.llm.errors import LLMClientError

__all__ = ["DEFAULT_TIMEOUT_S", "GEMINI_MODEL_LABEL", "GeminiAdapter"]

GEMINI_MODEL_LABEL = "gemini-cli"
DEFAULT_TIMEOUT_S = 300.0


class GeminiAdapter:
    """LLMClient backed by ``gemini --prompt`` in read-only plan mode."""

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        gemini_binary: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout_s = float(timeout_s)
        if gemini_binary is None:
            resolved = shutil.which("gemini")
            if resolved is None:
                raise LLMClientError("gemini CLI not found on PATH")
            self._gemini_binary = resolved
        else:
            self._gemini_binary = gemini_binary

    def chat(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
    ) -> ChatResponse:
        payload = self._invoke_gemini(serialize_messages(messages, system=system))
        text = read_text_field(payload, provider="gemini")
        return ChatResponse(
            text=text,
            model=str(payload.get("model") or self._model or GEMINI_MODEL_LABEL),
            input_tokens=_usage_int(payload, "input_tokens"),
            output_tokens=_usage_int(payload, "output_tokens"),
            stop_reason="end_turn",
        )

    def extract(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
    ) -> ExtractResponse:
        schema_json = json.dumps(schema, separators=(",", ":"), sort_keys=True)
        prompt = (
            "Respond with one JSON object matching this schema. "
            "Do not include markdown or prose.\n\n"
            f"{schema_json}\n\n"
            f"{serialize_messages(messages)}"
        )
        payload = self._invoke_gemini(prompt)
        text = read_text_field(payload, provider="gemini")
        data = extract_json_object(text, provider="gemini")
        usage = Usage(
            input_tokens=_usage_int(payload, "input_tokens"),
            output_tokens=_usage_int(payload, "output_tokens"),
        )
        return ExtractResponse(data=data, usage=usage)

    def tool_call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> ToolCallResponse:
        tools_json = json.dumps(tools, separators=(",", ":"), sort_keys=True)
        effective_system = (
            f"{system}\n\nAvailable tools, informational only:\n{tools_json}"
            if system
            else f"Available tools, informational only:\n{tools_json}"
        )
        payload = self._invoke_gemini(
            serialize_messages(messages, system=effective_system)
        )
        text = read_text_field(payload, provider="gemini")
        return ToolCallResponse(
            text=text,
            tool_calls=[],
            model=str(payload.get("model") or self._model or GEMINI_MODEL_LABEL),
            input_tokens=_usage_int(payload, "input_tokens"),
            output_tokens=_usage_int(payload, "output_tokens"),
            stop_reason="end_turn",
        )

    def _build_argv(self, prompt: str) -> list[str]:
        argv = [
            self._gemini_binary,
            "--skip-trust",
            "--approval-mode",
            "plan",
            "--output-format",
            "json",
        ]
        if self._model is not None:
            argv.extend(["--model", self._model])
        argv.extend(["--prompt", prompt])
        return argv

    def _invoke_gemini(self, prompt: str) -> dict[str, Any]:
        try:
            completed = subprocess.run(  # noqa: S603 - argv is controlled
                self._build_argv(prompt),
                capture_output=True,
                text=True,
                timeout=self._timeout_s,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMClientError(f"gemini timed out after {self._timeout_s}s") from exc
        if completed.returncode != 0:
            stderr_tail = (completed.stderr or "").strip()[-500:]
            raise LLMClientError(f"gemini exited {completed.returncode}: {stderr_tail}")
        try:
            payload = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            head = (completed.stdout or "")[:200]
            raise LLMClientError(f"gemini returned non-JSON stdout: {head}") from exc
        if not isinstance(payload, dict):
            raise LLMClientError("gemini returned non-object JSON stdout")
        return payload


def _usage_int(payload: dict[str, Any], key: str) -> int:
    usage = payload.get("usage")
    value = usage.get(key) if isinstance(usage, dict) else payload.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
