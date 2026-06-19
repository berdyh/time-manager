"""CLI tests for capture and transcript retention commands."""

from __future__ import annotations

import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.repositories.events import EventsRepository
from tm.repositories.transcripts import TranscriptRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tm_test.db"
    store = SQLiteStore(db_path)
    store.apply_pending_migrations()
    store.close()
    return db_path


def test_capture_telegram_imports_messages(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export = tmp_path / "telegram.json"
    unix_time = int(datetime(2026, 6, 1, 9, tzinfo=UTC).timestamp())
    export.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2026-06-01T09:00:00",
                        "date_unixtime": str(unix_time),
                        "from": "Shoh",
                        "text": "worked on billing",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(export), "--db-path", str(db)],
    )
    repeated = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(export), "--db-path", str(db)],
    )

    assert result.exit_code == 0, result.output
    assert repeated.exit_code == 0, repeated.output
    assert "telegram capture imported 0 messages" in repeated.output
    events = EventsRepository(db).query_events(case_date="2026-06-01")
    assert len(events) == 1
    assert events[0]["activity"] == "telegram_message"
    assert events[0]["attributes"]["text"] == "worked on billing"


def test_capture_telegram_scopes_message_ids_by_chat(tmp_path: Path) -> None:
    db = _db(tmp_path)
    first = tmp_path / "telegram-a.json"
    second = tmp_path / "telegram-b.json"
    unix_time = int(datetime(2026, 6, 1, 9, tzinfo=UTC).timestamp())
    payload = {
        "messages": [
            {
                "id": 1,
                "type": "message",
                "date": "2026-06-01T09:00:00",
                "date_unixtime": str(unix_time),
                "from": "Shoh",
                "text": "worked on billing",
            }
        ]
    }
    first.write_text(json.dumps({"name": "chat-a", **payload}), encoding="utf-8")
    second.write_text(json.dumps({"name": "chat-b", **payload}), encoding="utf-8")

    first_result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(first), "--db-path", str(db)],
    )
    second_result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(second), "--db-path", str(db)],
    )

    assert first_result.exit_code == 0, first_result.output
    assert second_result.exit_code == 0, second_result.output
    events = EventsRepository(db).query_events(case_date="2026-06-01")
    assert len(events) == 2
    assert {event["attributes"]["chat"] for event in events} == {"chat-a", "chat-b"}


def test_capture_telegram_prefers_date_unixtime(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export = tmp_path / "telegram.json"
    unix_time = int(datetime(2026, 6, 1, 23, 30, tzinfo=UTC).timestamp())
    export.write_text(
        json.dumps(
            {
                "name": "chat-a",
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2026-06-02T23:30:00",
                        "date_unixtime": str(unix_time),
                        "from": "Shoh",
                        "text": "worked late",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(export), "--db-path", str(db)],
    )

    assert result.exit_code == 0, result.output
    events = EventsRepository(db).query_events(case_date="2026-06-01")
    assert len(events) == 1
    assert events[0]["timestamp"] == "2026-06-01T23:30:00Z"


def test_capture_telegram_fallback_chat_does_not_store_input_path(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    first = tmp_path / "nested" / "telegram.json"
    second = tmp_path / "moved.json"
    first.parent.mkdir()
    unix_time = int(datetime(2026, 6, 1, 9, tzinfo=UTC).timestamp())
    payload = json.dumps(
        {
            "messages": [
                {
                    "id": 1,
                    "type": "message",
                    "date_unixtime": str(unix_time),
                    "from": "Shoh",
                    "text": "worked on billing",
                }
            ]
        }
    )
    first.write_text(payload, encoding="utf-8")
    second.write_text(payload, encoding="utf-8")

    first_result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(first), "--db-path", str(db)],
    )
    second_result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(second), "--db-path", str(db)],
    )

    assert first_result.exit_code == 0, first_result.output
    assert second_result.exit_code == 0, second_result.output
    assert "telegram capture imported 0 messages" in second_result.output
    events = EventsRepository(db).query_events(case_date="2026-06-01")
    assert len(events) == 1
    assert str(tmp_path) not in events[0]["attributes"]["chat"]
    assert events[0]["attributes"]["chat"] == "telegram-export"


def test_capture_telegram_fallback_identity_is_stable_when_export_grows(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    first = tmp_path / "telegram.json"
    second = tmp_path / "telegram-updated.json"
    first_time = int(datetime(2026, 6, 1, 9, tzinfo=UTC).timestamp())
    second_time = int(datetime(2026, 6, 1, 10, tzinfo=UTC).timestamp())
    first.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date_unixtime": str(first_time),
                        "from": "Shoh",
                        "text": "worked on billing",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    second.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date_unixtime": str(first_time),
                        "from": "Shoh",
                        "text": "worked on billing",
                    },
                    {
                        "id": 2,
                        "type": "message",
                        "date_unixtime": str(second_time),
                        "from": "Shoh",
                        "text": "reviewed the dashboard",
                    },
                ]
            }
        ),
        encoding="utf-8",
    )

    first_result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(first), "--db-path", str(db)],
    )
    second_result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(second), "--db-path", str(db)],
    )

    assert first_result.exit_code == 0, first_result.output
    assert second_result.exit_code == 0, second_result.output
    assert "telegram capture imported 1 messages" in second_result.output
    events = EventsRepository(db).query_events(case_date="2026-06-01")
    assert len(events) == 2


def test_capture_telegram_fallback_identity_does_not_hash_text(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    export = tmp_path / "telegram.json"
    unix_time = int(datetime(2026, 6, 1, 9, tzinfo=UTC).timestamp())
    export.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date_unixtime": str(unix_time),
                        "from": "Shoh",
                        "text": "ok",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    old_payload = {
        "date": str(unix_time),
        "from": "Shoh",
        "id": 1,
        "text": "ok",
    }
    old_source_digest = hashlib.sha256(
        json.dumps(old_payload, sort_keys=True, ensure_ascii=False).encode("utf-8")
    ).hexdigest()[:32]
    old_source_id = f"telegram-message-{old_source_digest}"
    old_event_digest = hashlib.sha256(f"telegram|{old_source_id}".encode()).hexdigest()[
        :32
    ]

    result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(export), "--db-path", str(db)],
    )

    assert result.exit_code == 0, result.output
    event = EventsRepository(db).query_events(case_date="2026-06-01")[0]
    assert event["attributes"]["source_id"] != old_source_id
    assert event["event_id"] != f"capture-{old_event_digest}"


def test_capture_telegram_skips_naive_date_without_unixtime(tmp_path: Path) -> None:
    db = _db(tmp_path)
    export = tmp_path / "telegram.json"
    export.write_text(
        json.dumps(
            {
                "messages": [
                    {
                        "id": 1,
                        "type": "message",
                        "date": "2026-06-02T23:30:00",
                        "from": "Shoh",
                        "text": "worked late",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "telegram", "--input", str(export), "--db-path", str(db)],
    )

    assert result.exit_code == 0, result.output
    assert "telegram capture imported 0 messages" in result.output
    assert EventsRepository(db).query_events() == []


def test_capture_calendar_imports_ics_events(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ics = tmp_path / "calendar.ics"
    ics.write_text(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:abc",
                "DTSTART:20260602T100000Z",
                "SUMMARY:Deep work block",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "calendar", "--input", str(ics), "--db-path", str(db)],
    )

    assert result.exit_code == 0, result.output
    events = EventsRepository(db).query_events(case_date="2026-06-02")
    assert len(events) == 1
    assert events[0]["activity"] == "calendar_event"
    assert events[0]["attributes"]["source_id"] == "abc"


def test_capture_calendar_rejects_tzid_without_importing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ics = tmp_path / "calendar.ics"
    ics.write_text(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:abc",
                "DTSTART;TZID=America/Los_Angeles:20260602T230000",
                "SUMMARY:Late local event",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "calendar", "--input", str(ics), "--db-path", str(db)],
    )

    assert result.exit_code == 1, result.output
    assert "TZID" in result.output
    assert EventsRepository(db).query_events() == []


def test_capture_calendar_rejects_recurrence_without_importing(tmp_path: Path) -> None:
    db = _db(tmp_path)
    ics = tmp_path / "calendar.ics"
    ics.write_text(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:abc",
                "DTSTART:20260602T100000Z",
                "RRULE:FREQ=WEEKLY;COUNT=4",
                "SUMMARY:Recurring event",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "calendar", "--input", str(ics), "--db-path", str(db)],
    )

    assert result.exit_code == 1, result.output
    assert "recurrence" in result.output
    assert EventsRepository(db).query_events() == []


def test_capture_calendar_rejects_floating_datetime_without_importing(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    ics = tmp_path / "calendar.ics"
    ics.write_text(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:abc",
                "DTSTART:20260602T100000",
                "SUMMARY:Floating local event",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "calendar", "--input", str(ics), "--db-path", str(db)],
    )

    assert result.exit_code == 1, result.output
    assert "requires UTC or offset timestamps" in result.output
    assert EventsRepository(db).query_events() == []


def test_capture_calendar_rejects_invalid_date_without_importing(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    ics = tmp_path / "calendar.ics"
    ics.write_text(
        "\n".join(
            [
                "BEGIN:VCALENDAR",
                "BEGIN:VEVENT",
                "UID:abc",
                "DTSTART;VALUE=DATE:20260230",
                "SUMMARY:Impossible date",
                "END:VEVENT",
                "END:VCALENDAR",
            ]
        ),
        encoding="utf-8",
    )

    result = runner.invoke(
        app,
        ["capture", "calendar", "--input", str(ics), "--db-path", str(db)],
    )

    assert result.exit_code == 1, result.output
    assert "requires UTC or offset timestamps" in result.output
    assert EventsRepository(db).query_events() == []


def test_capture_voice_retains_transcript(tmp_path: Path) -> None:
    db = _db(tmp_path)
    transcript = tmp_path / "voice.txt"
    transcript.write_text("I finished the auth bug.", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "capture",
            "voice",
            "--transcript-file",
            str(transcript),
            "--case-date",
            "2026-06-03",
            "--db-path",
            str(db),
        ],
    )

    assert result.exit_code == 0, result.output
    record = TranscriptRepository(db).get("2026-06-03")
    assert record is not None
    assert record.source == "voice"
    assert "auth bug" in record.transcript_text
    events = EventsRepository(db).query_events(case_date="2026-06-03")
    old_digest = hashlib.sha256(
        b"voice|2026-06-03T12:00:00Z|voice_note|I finished the auth bug."
    ).hexdigest()[:32]
    assert events[0]["event_id"] != f"capture-{old_digest}"


def test_capture_voice_rejects_invalid_case_date(tmp_path: Path) -> None:
    db = _db(tmp_path)
    transcript = tmp_path / "voice.txt"
    transcript.write_text("I finished the auth bug.", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "capture",
            "voice",
            "--transcript-file",
            str(transcript),
            "--case-date",
            "2026-02-30",
            "--db-path",
            str(db),
        ],
    )

    assert result.exit_code == 2, result.output
    assert TranscriptRepository(db).get("2026-02-30") is None
    assert EventsRepository(db).query_events(case_date="2026-02-30") == []


def test_capture_voice_merges_with_existing_day_transcript(tmp_path: Path) -> None:
    db = _db(tmp_path)
    TranscriptRepository(db).upsert(
        case_date="2026-06-03",
        transcript_text="debrief transcript",
        source="debrief",
        extractor_version="debrief-v1",
    )
    transcript = tmp_path / "voice.txt"
    transcript.write_text("voice transcript", encoding="utf-8")

    result = runner.invoke(
        app,
        [
            "capture",
            "voice",
            "--transcript-file",
            str(transcript),
            "--case-date",
            "2026-06-03",
            "--db-path",
            str(db),
        ],
    )

    assert result.exit_code == 0, result.output
    record = TranscriptRepository(db).get("2026-06-03")
    assert record is not None
    assert record.source == "merged"
    assert "debrief transcript" in record.transcript_text
    assert "voice transcript" in record.transcript_text


def test_transcript_merge_keeps_substring_note(tmp_path: Path) -> None:
    db = _db(tmp_path)
    transcripts = TranscriptRepository(db)
    transcripts.upsert(
        case_date="2026-06-03",
        transcript_text="I finished the auth bug.",
        source="debrief",
        extractor_version="debrief-v1",
    )
    transcripts.upsert(
        case_date="2026-06-03",
        transcript_text="auth bug",
        source="voice",
        extractor_version="capture-v1",
    )

    record = transcripts.get("2026-06-03")

    assert record is not None
    assert record.source == "merged"
    assert "I finished the auth bug." in record.transcript_text
    assert "--- additional transcript (voice) ---" in record.transcript_text
    assert record.transcript_text.endswith("auth bug")


def test_transcript_upserts_concurrent_same_day_merge(tmp_path: Path) -> None:
    db = _db(tmp_path)

    def _upsert(text: str) -> None:
        TranscriptRepository(db).upsert(
            case_date="2026-06-03",
            transcript_text=text,
            source="voice",
            extractor_version="capture-v1",
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(_upsert, ["first note", "second note"]))

    record = TranscriptRepository(db).get("2026-06-03")

    assert record is not None
    assert "first note" in record.transcript_text
    assert "second note" in record.transcript_text


def test_transcript_upsert_hardens_sqlite_files_after_write(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = _db(tmp_path)
    from tm.repositories import transcripts as transcripts_mod

    calls: list[Path] = []
    real_harden = transcripts_mod.harden_sqlite_file_permissions

    def _record_harden(path: str | Path) -> None:
        calls.append(Path(path))
        real_harden(path)

    monkeypatch.setattr(
        transcripts_mod,
        "harden_sqlite_file_permissions",
        _record_harden,
    )

    TranscriptRepository(db).upsert(
        case_date="2026-06-03",
        transcript_text="private transcript",
        source="voice",
    )

    assert db in calls
