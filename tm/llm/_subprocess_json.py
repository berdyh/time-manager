"""Shared helpers for subprocess-backed JSON-ish LLM CLIs."""

from __future__ import annotations

import json
import subprocess
from collections.abc import Callable, Sequence
from typing import Any

from tm.llm.client import Message
from tm.llm.errors import LLMClientError

__all__ = [
    "build_schema_prompt",
    "build_tools_system",
    "extract_json_object",
    "read_text_field",
    "run_json_subprocess",
    "serialize_messages",
    "usage_int",
]

_SubprocessRun = Callable[..., subprocess.CompletedProcess[str]]


def serialize_messages(messages: list[Message], *, system: str | None = None) -> str:
    """Render a narrow transcript into role-marked plain text."""

    parts: list[str] = []
    if system is not None:
        parts.append(f"[system]\n{system}")
    for message in messages:
        parts.append(f"[{message.role}]\n{message.content}")
    return "\n\n".join(parts)


def build_schema_prompt(messages: list[Message], *, schema: dict[str, Any]) -> str:
    """Build the standard extract prompt used by subprocess-backed adapters."""
    schema_json = json.dumps(schema, separators=(",", ":"), sort_keys=True)
    return (
        "Respond with one JSON object matching this schema. "
        "Do not include markdown or prose.\n\n"
        f"{schema_json}\n\n"
        f"{serialize_messages(messages)}"
    )


def build_tools_system(
    system: str | None,
    *,
    tools: list[dict[str, Any]],
) -> str:
    """Append informational tool metadata to an optional system prompt."""
    tools_json = json.dumps(tools, separators=(",", ":"), sort_keys=True)
    tool_text = f"Available tools, informational only:\n{tools_json}"
    return f"{system}\n\n{tool_text}" if system else tool_text


def read_text_field(payload: dict[str, Any], *, provider: str) -> str:
    """Return assistant text from common CLI JSON output shapes."""

    for key in ("result", "response", "text", "content", "message"):
        value = payload.get(key)
        if isinstance(value, str):
            return value
    candidates = payload.get("candidates")
    if isinstance(candidates, list) and candidates:
        first = candidates[0]
        if isinstance(first, dict):
            value = first.get("text") or first.get("content")
            if isinstance(value, str):
                return value
    keys = sorted(payload.keys())
    raise LLMClientError(f"{provider} response missing text field; keys={keys}")


def extract_json_object(text: str, *, provider: str) -> dict[str, Any]:
    """Parse a JSON object from a direct or fenced response string."""

    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if len(lines) >= 3 and lines[0].startswith("```"):
            stripped = "\n".join(lines[1:-1]).strip()
    try:
        data = json.loads(stripped)
    except json.JSONDecodeError as exc:
        head = stripped[:200]
        raise LLMClientError(f"{provider} returned non-JSON: {head}") from exc
    if not isinstance(data, dict):
        head = stripped[:200]
        raise LLMClientError(f"{provider} returned non-object JSON: {head}")
    return data


def run_json_subprocess(
    argv: Sequence[str],
    *,
    provider: str,
    timeout_s: float,
    run: _SubprocessRun = subprocess.run,
) -> dict[str, Any]:
    """Run a CLI command and parse its stdout as a JSON object."""
    try:
        completed = run(  # noqa: S603 - argv is controlled by the adapter
            list(argv),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise LLMClientError(f"{provider} timed out after {timeout_s}s") from exc

    if completed.returncode != 0:
        stderr_tail = (completed.stderr or "").strip()[-500:]
        raise LLMClientError(f"{provider} exited {completed.returncode}: {stderr_tail}")

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        head = (completed.stdout or "")[:200]
        raise LLMClientError(f"{provider} returned non-JSON stdout: {head}") from exc
    if not isinstance(payload, dict):
        raise LLMClientError(f"{provider} returned non-object JSON stdout")
    return payload


def usage_int(payload: dict[str, Any], key: str) -> int:
    """Read token usage from either top-level fields or a nested usage object."""
    usage = payload.get("usage")
    value = usage.get(key) if isinstance(usage, dict) else payload.get(key)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
