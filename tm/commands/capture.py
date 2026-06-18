"""Capture passive inputs into the event log."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Annotated, Any

import typer

from tm._paths import default_db_path
from tm.commands._shared import (
    DbPathOption,
    ensure_migrations,
    utc_today,
    validate_case_date,
)
from tm.models.goals import ulid
from tm.repositories.events import EventsRepository
from tm.repositories.transcripts import TranscriptRepository

capture_app = typer.Typer(help="Capture Telegram, calendar, and voice-note inputs.")


def _case_date_from_ts(timestamp: str) -> str:
    return timestamp[:10]


def _normalize_timestamp(raw: str) -> str:
    value = raw.strip()
    if not value:
        raise ValueError("empty timestamp")
    if len(value) == 10 and value[4] == "-" and value[7] == "-":
        return f"{date.fromisoformat(value).isoformat()}T00:00:00Z"
    if len(value) == 8 and value.isdigit():
        compact_date = f"{value[:4]}-{value[4:6]}-{value[6:]}"
        return f"{date.fromisoformat(compact_date).isoformat()}T00:00:00Z"
    if (
        len(value) == 16
        and value.endswith("Z")
        and value[:8].isdigit()
        and value[8] == "T"
        and value[9:15].isdigit()
    ):
        value = (
            f"{value[:4]}-{value[4:6]}-{value[6:8]}"
            f"T{value[9:11]}:{value[11:13]}:{value[13:15]}+00:00"
        )
    elif value.endswith("Z"):
        value = value[:-1] + "+00:00"
    elif "T" in value and "-" not in value[:10]:
        raise ValueError("timezone required for compact datetime")
    if value.endswith("Z"):
        value = value[:-1] + "+00:00"
    dt = datetime.fromisoformat(value)
    if dt.tzinfo is None:
        raise ValueError("timezone required for datetime")
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _telegram_text(value: Any) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        parts: list[str] = []
        for item in value:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _telegram_chat_identity(raw: Any) -> tuple[str, bool]:
    if isinstance(raw, dict):
        for key in ("id", "name"):
            value = raw.get(key)
            if value is not None:
                return str(value), True
    return "telegram-export", False


def _telegram_source_id(
    *, chat_identity: str, has_chat_identity: bool, msg: dict[str, Any]
) -> str:
    msg_id = msg.get("id")
    if has_chat_identity and msg_id is not None:
        return f"{chat_identity}:{msg_id}"
    date_value = msg.get("date_unixtime")
    if date_value is None:
        date_value = msg.get("date")
    payload = {
        "date": date_value,
        "from": msg.get("from"),
        "id": msg_id,
    }
    digest = hashlib.sha256(
        json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    return f"telegram-message-{digest}"


def _append_capture_event(
    repo: EventsRepository,
    *,
    source: str,
    timestamp: str,
    activity: str,
    text: str,
    source_id: str | None = None,
    extra: dict[str, Any] | None = None,
) -> bool:
    attributes: dict[str, Any] = {
        "source": source,
        "text": text,
    }
    if source_id:
        attributes["source_id"] = source_id
    if extra:
        attributes.update(extra)
    event_id = ulid()
    if source_id:
        digest = hashlib.sha256(f"{source}|{source_id}".encode()).hexdigest()[:32]
        event_id = f"capture-{digest}"
    try:
        repo.append_event(
            event_id=event_id,
            case_id=_case_date_from_ts(timestamp),
            activity=activity,
            timestamp=timestamp,
            lifecycle="complete",
            resource=source,
            attributes=attributes,
            extractor_version="capture-v1",
            case_date=_case_date_from_ts(timestamp),
            schema_version="v1",
        )
    except sqlite3.IntegrityError:
        return False
    return True


@capture_app.command("telegram")
def capture_telegram(
    input_path: Annotated[
        Path,
        typer.Option("--input", "-i", help="Telegram Desktop JSON export path."),
    ],
    db_path: DbPathOption = None,
) -> None:
    """Import Telegram Desktop JSON export messages as capture events."""
    resolved_db = db_path or default_db_path()
    ensure_migrations(resolved_db)

    try:
        raw = json.loads(input_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        typer.echo(f"error: could not read Telegram export: {exc}", err=True)
        raise typer.Exit(1) from exc

    messages = raw.get("messages") if isinstance(raw, dict) else raw
    if not isinstance(messages, list):
        typer.echo("error: expected Telegram export with a messages array", err=True)
        raise typer.Exit(1)
    chat_identity, has_chat_identity = _telegram_chat_identity(raw)

    repo = EventsRepository(resolved_db)
    imported = 0
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("type") not in (None, "message"):
            continue
        text = _telegram_text(msg.get("text"))
        date_raw = msg.get("date_unixtime")
        if date_raw is None:
            date_raw = msg.get("date")
        if not text.strip() or date_raw is None:
            continue
        try:
            if str(date_raw).isdigit():
                timestamp = datetime.fromtimestamp(int(date_raw), UTC).strftime(
                    "%Y-%m-%dT%H:%M:%SZ"
                )
            else:
                timestamp = _normalize_timestamp(str(date_raw))
        except ValueError:
            continue
        imported += int(
            _append_capture_event(
                repo,
                source="telegram",
                timestamp=timestamp,
                activity="telegram_message",
                text=text,
                source_id=_telegram_source_id(
                    chat_identity=chat_identity,
                    has_chat_identity=has_chat_identity,
                    msg=msg,
                ),
                extra={"sender": msg.get("from"), "chat": chat_identity},
            )
        )
    typer.echo(f"telegram capture imported {imported} messages")


def _unfold_ics(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.splitlines():
        if raw.startswith((" ", "\t")) and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw.rstrip("\r"))
    return lines


def _field_value(line: str) -> tuple[str, dict[str, str], str]:
    raw_key, value = line.split(":", 1)
    pieces = raw_key.split(";")
    key = pieces[0].upper()
    params: dict[str, str] = {}
    for piece in pieces[1:]:
        if "=" not in piece:
            continue
        param_key, param_value = piece.split("=", 1)
        params[param_key.upper()] = param_value
    return key, params, value


@capture_app.command("calendar")
def capture_calendar(
    input_path: Annotated[
        Path,
        typer.Option("--input", "-i", help="iCalendar .ics file path."),
    ],
    db_path: DbPathOption = None,
) -> None:
    """Import VEVENT rows from an iCalendar file."""
    resolved_db = db_path or default_db_path()
    ensure_migrations(resolved_db)
    try:
        lines = _unfold_ics(input_path.read_text(encoding="utf-8"))
    except OSError as exc:
        typer.echo(f"error: could not read calendar file: {exc}", err=True)
        raise typer.Exit(1) from exc

    current: dict[str, str] | None = None
    pending: list[tuple[str, str, str | None, dict[str, Any]]] = []
    unsupported_tzid = 0
    unsupported_timestamp = 0
    for line in lines:
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT" and current is not None:
            summary = current.get("SUMMARY", "calendar_event").strip()
            start = current.get("DTSTART")
            if current.get("_UNSUPPORTED_TZID"):
                unsupported_tzid += 1
                current = None
                continue
            if current.get("_UNSUPPORTED_RECURRENCE"):
                unsupported_tzid += 1
                current = None
                continue
            if start:
                try:
                    timestamp = _normalize_timestamp(start)
                except ValueError:
                    unsupported_timestamp += 1
                    current = None
                    continue
                extra: dict[str, Any] = {}
                if current.get("DTEND"):
                    extra["dtend"] = current["DTEND"]
                pending.append((timestamp, summary, current.get("UID"), extra))
            current = None
            continue
        if current is not None and ":" in line:
            key, params, value = _field_value(line)
            if key in {"DTSTART", "DTEND"} and "TZID" in params:
                current["_UNSUPPORTED_TZID"] = params["TZID"]
            if key in {"RRULE", "RDATE", "EXDATE"}:
                current["_UNSUPPORTED_RECURRENCE"] = key
            current[key] = value
    if unsupported_tzid:
        typer.echo(
            "error: calendar import does not support TZID or recurrence; "
            "export UTC single-instance .ics instead",
            err=True,
        )
        raise typer.Exit(1)
    if unsupported_timestamp:
        typer.echo(
            "error: calendar import requires UTC or offset timestamps; "
            "export UTC single-instance .ics instead",
            err=True,
        )
        raise typer.Exit(1)

    repo = EventsRepository(resolved_db)
    imported = 0
    for timestamp, summary, source_id, extra in pending:
        imported += int(
            _append_capture_event(
                repo,
                source="calendar",
                timestamp=timestamp,
                activity="calendar_event",
                text=summary,
                source_id=source_id,
                extra=extra,
            )
        )
    typer.echo(f"calendar capture imported {imported} events")


@capture_app.command("voice")
def capture_voice(
    transcript_file: Annotated[
        Path,
        typer.Option("--transcript-file", help="Already-transcribed voice note text."),
    ],
    db_path: DbPathOption = None,
    case_date: Annotated[
        str | None,
        typer.Option("--case-date", help="Case date for the voice note."),
    ] = None,
) -> None:
    """Capture an already-transcribed voice note for later re-extraction."""
    resolved_db = db_path or default_db_path()
    resolved_case_date = validate_case_date(case_date or utc_today())
    ensure_migrations(resolved_db)
    try:
        transcript = transcript_file.read_text(encoding="utf-8")
    except OSError as exc:
        typer.echo(f"error: could not read transcript file: {exc}", err=True)
        raise typer.Exit(1) from exc
    if not transcript.strip():
        typer.echo("error: transcript is empty", err=True)
        raise typer.Exit(1)

    TranscriptRepository(resolved_db).upsert(
        case_date=resolved_case_date,
        transcript_text=transcript,
        source="voice",
        extractor_version="capture-v1",
    )
    _append_capture_event(
        EventsRepository(resolved_db),
        source="voice",
        timestamp=f"{resolved_case_date}T12:00:00Z",
        activity="voice_note",
        text=transcript,
    )
    typer.echo(f"voice capture stored transcript for {resolved_case_date}")
