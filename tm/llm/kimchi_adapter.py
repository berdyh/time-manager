"""Subprocess LLMClient adapter for the Kimchi CLI."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from tm.llm._subprocess_json import (
    build_schema_prompt,
    build_tools_system,
    extract_json_object,
    read_text_field,
    run_json_subprocess,
    serialize_messages,
    usage_int,
)
from tm.llm.client import (
    ChatResponse,
    ExtractResponse,
    Message,
    ToolCallResponse,
    Usage,
)
from tm.llm.errors import LLMClientError

__all__ = ["DEFAULT_TIMEOUT_S", "KIMCHI_MODEL_LABEL", "KimchiAdapter"]

KIMCHI_MODEL_LABEL = "kimchi-cli"
DEFAULT_TIMEOUT_S = 300.0


class KimchiAdapter:
    """LLMClient backed by ``kimchi --print`` in read-only plan mode."""

    def __init__(
        self,
        *,
        model: str | None = None,
        max_tokens: int | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        kimchi_binary: str | None = None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._timeout_s = float(timeout_s)
        if kimchi_binary is None:
            resolved = shutil.which("kimchi")
            if resolved is None:
                raise LLMClientError("kimchi CLI not found on PATH")
            self._kimchi_binary = resolved
        else:
            self._kimchi_binary = kimchi_binary

    def chat(
        self,
        messages: list[Message],
        *,
        system: str | None = None,
    ) -> ChatResponse:
        payload = self._invoke_kimchi(serialize_messages(messages, system=system))
        text = read_text_field(payload, provider="kimchi")
        return ChatResponse(
            text=text,
            model=str(payload.get("model") or self._model or KIMCHI_MODEL_LABEL),
            input_tokens=usage_int(payload, "input_tokens"),
            output_tokens=usage_int(payload, "output_tokens"),
            stop_reason="end_turn",
        )

    def extract(
        self,
        messages: list[Message],
        *,
        schema: dict[str, Any],
    ) -> ExtractResponse:
        prompt = build_schema_prompt(messages, schema=schema)
        payload = self._invoke_kimchi(prompt)
        text = read_text_field(payload, provider="kimchi")
        data = extract_json_object(text, provider="kimchi")
        usage = Usage(
            input_tokens=usage_int(payload, "input_tokens"),
            output_tokens=usage_int(payload, "output_tokens"),
        )
        return ExtractResponse(data=data, usage=usage)

    def tool_call(
        self,
        messages: list[Message],
        *,
        tools: list[dict[str, Any]],
        system: str | None = None,
    ) -> ToolCallResponse:
        effective_system = build_tools_system(system, tools=tools)
        payload = self._invoke_kimchi(
            serialize_messages(messages, system=effective_system)
        )
        text = read_text_field(payload, provider="kimchi")
        return ToolCallResponse(
            text=text,
            tool_calls=[],
            model=str(payload.get("model") or self._model or KIMCHI_MODEL_LABEL),
            input_tokens=usage_int(payload, "input_tokens"),
            output_tokens=usage_int(payload, "output_tokens"),
            stop_reason="end_turn",
        )

    def _build_argv(self, prompt: str) -> list[str]:
        argv = [
            self._kimchi_binary,
            "--plan",
            "--no-session",
            "--mode",
            "json",
            "--print",
        ]
        if self._model is not None:
            argv.extend(["--model", self._model])
        argv.append(prompt)
        return argv

    def _invoke_kimchi(self, prompt: str) -> dict[str, Any]:
        return run_json_subprocess(
            self._build_argv(prompt),
            provider="kimchi",
            timeout_s=self._timeout_s,
            run=subprocess.run,
        )
