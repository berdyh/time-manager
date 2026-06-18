"""Shared helpers for subprocess-backed JSON-ish LLM CLIs."""

from __future__ import annotations

import json
from typing import Any

from tm.llm.client import Message
from tm.llm.errors import LLMClientError

__all__ = ["extract_json_object", "read_text_field", "serialize_messages"]


def serialize_messages(messages: list[Message], *, system: str | None = None) -> str:
    """Render a narrow transcript into role-marked plain text."""

    parts: list[str] = []
    if system is not None:
        parts.append(f"[system]\n{system}")
    for message in messages:
        parts.append(f"[{message.role}]\n{message.content}")
    return "\n\n".join(parts)


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
