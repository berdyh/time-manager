"""CLI tests for dashboard, export, and backup commands."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from typer.testing import CliRunner

from tm.cli import app
from tm.repositories.events import EventsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository
from tm.repositories.transcripts import TranscriptRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.stores.sqlite_store import SQLiteStore

runner = CliRunner()


def _db(tmp_path: Path) -> Path:
    db_path = tmp_path / "tm_test.db"
    store = SQLiteStore(db_path)
    store.apply_pending_migrations()
    store.close()
    return db_path


def test_dashboard_export_backup_and_privacy(tmp_path: Path) -> None:
    db = _db(tmp_path)
    repo = EventsRepository(db)
    repo.append_event(
        event_id="evt-1",
        case_id="2026-06-04",
        activity="deep_work",
        timestamp="2026-06-04T09:00:00Z",
        lifecycle="complete",
        resource="telegram",
        attributes={"text": "secret note", "duration_minutes": 60},
        extractor_version="capture-v1",
        case_date="2026-06-04",
    )
    TranscriptRepository(db).upsert(
        case_date="2026-06-04",
        transcript_text="secret transcript",
        source="voice",
    )
    vocab = VocabularyRepository(db)
    vocab.add_canonical("custom_work", description="Custom work")
    vocab.add_alias("cw", "custom_work")

    dashboard = runner.invoke(app, ["dashboard", "--db-path", str(db)])
    assert dashboard.exit_code == 0, dashboard.output
    assert "events: 1" in dashboard.output
    assert "transcripts: 1" in dashboard.output
    assert "deep_work: 1" in dashboard.output

    export_path = tmp_path / "export.json"
    export_path.write_text("old", encoding="utf-8")
    export_path.chmod(0o644)
    exported = runner.invoke(
        app, ["export", "--output", str(export_path), "--db-path", str(db)]
    )
    assert exported.exit_code == 0, exported.output
    assert export_path.stat().st_mode & 0o777 == 0o600
    payload = json.loads(export_path.read_text(encoding="utf-8"))
    assert payload["events"][0]["event_id"] == "evt-1"
    assert payload["vocabulary"][0]["activity_name"] == "custom_work"
    assert payload["aliases"][0]["free_text_variant"] == "cw"

    backup_path = tmp_path / "backup.db"
    backup = runner.invoke(
        app, ["backup", "--output", str(backup_path), "--db-path", str(db)]
    )
    assert backup.exit_code == 0, backup.output
    assert backup_path.stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backup_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 1

    redacted = runner.invoke(
        app,
        ["privacy", "redact", "--case-date", "2026-06-04", "--db-path", str(db)],
    )
    assert redacted.exit_code == 0, redacted.output
    event = EventsRepository(db).query_events(case_date="2026-06-04")[0]
    assert event is not None
    assert event["resource"] is None
    assert event["attributes"]["text"] == "[redacted]"
    transcript_record = TranscriptRepository(db).get("2026-06-04")
    assert transcript_record is not None
    assert transcript_record.transcript_text == "[redacted]"

    forgotten = runner.invoke(
        app,
        ["privacy", "forget", "--case-date", "2026-06-04", "--db-path", str(db)],
    )
    assert forgotten.exit_code == 0, forgotten.output
    assert EventsRepository(db).query_events(case_date="2026-06-04") == []
    assert TranscriptRepository(db).get("2026-06-04") is None


def test_dashboard_filters_suggestions_by_case_date(tmp_path: Path) -> None:
    db = _db(tmp_path)
    TranscriptRepository(db).upsert(
        case_date="2026-06-04",
        transcript_text="inside window",
        source="voice",
    )
    TranscriptRepository(db).upsert(
        case_date="2026-06-05",
        transcript_text="outside window",
        source="voice",
    )
    SuggestionTelemetryRepository(db).log_suggestion(
        case_date="2026-06-04",
        recommended_action="in range",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.0,
    )
    SuggestionTelemetryRepository(db).log_suggestion(
        case_date="2026-06-05",
        recommended_action="out of range",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.0,
    )

    dashboard = runner.invoke(
        app,
        [
            "dashboard",
            "--since",
            "2026-06-04",
            "--until",
            "2026-06-04",
            "--db-path",
            str(db),
        ],
    )

    assert dashboard.exit_code == 0, dashboard.output
    assert "suggestions: 1" in dashboard.output
    assert "transcripts: 1" in dashboard.output


def test_export_and_backup_refuse_database_output_path(tmp_path: Path) -> None:
    db = _db(tmp_path)

    exported = runner.invoke(
        app,
        ["export", "--output", str(db), "--db-path", str(db)],
    )
    backup = runner.invoke(
        app,
        ["backup", "--output", str(db), "--overwrite", "--db-path", str(db)],
    )

    assert exported.exit_code == 1, exported.output
    assert "must differ from database path" in exported.output
    assert backup.exit_code == 1, backup.output
    assert "must differ from database path" in backup.output
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM events").fetchone()[0] == 0


def test_export_and_backup_refuse_sqlite_sidecar_output_paths(tmp_path: Path) -> None:
    db = _db(tmp_path)
    for suffix in ("-wal", "-shm", "-journal"):
        sidecar = Path(f"{db}{suffix}")
        exported = runner.invoke(
            app,
            ["export", "--output", str(sidecar), "--db-path", str(db)],
        )
        backup = runner.invoke(
            app,
            ["backup", "--output", str(sidecar), "--overwrite", "--db-path", str(db)],
        )

        assert exported.exit_code == 1, exported.output
        assert "SQLite sidecars" in exported.output
        assert backup.exit_code == 1, backup.output
        assert "SQLite sidecars" in backup.output
        assert not sidecar.exists()


def test_export_and_backup_refuse_canonical_sidecars_for_symlinked_db(
    tmp_path: Path,
) -> None:
    db = _db(tmp_path)
    link = tmp_path / "link.db"
    link.symlink_to(db)
    sidecar = Path(f"{db}-wal")

    exported = runner.invoke(
        app,
        ["export", "--output", str(sidecar), "--db-path", str(link)],
    )
    backup = runner.invoke(
        app,
        ["backup", "--output", str(sidecar), "--overwrite", "--db-path", str(link)],
    )

    assert exported.exit_code == 1, exported.output
    assert "SQLite sidecars" in exported.output
    assert backup.exit_code == 1, backup.output
    assert "SQLite sidecars" in backup.output


def test_export_reads_all_tables_in_one_transaction(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = _db(tmp_path)
    output = tmp_path / "export.json"

    from tm.commands import export as export_cmd

    real_connect = export_cmd.connect_sqlite
    statements: list[str] = []
    connection_count = 0

    class RecordingConn:
        def __init__(self, conn: sqlite3.Connection) -> None:
            self._conn = conn

        def execute(self, sql: str, params: object = ()) -> sqlite3.Cursor:
            statements.append(sql)
            return self._conn.execute(sql, params)  # type: ignore[arg-type]

        def commit(self) -> None:
            statements.append("COMMIT")
            self._conn.commit()

        def rollback(self) -> None:
            statements.append("ROLLBACK")
            self._conn.rollback()

        def close(self) -> None:
            self._conn.close()

    def _recording_connect(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        nonlocal connection_count
        connection_count += 1
        return RecordingConn(real_connect(*args, **kwargs))

    monkeypatch.setattr(export_cmd, "connect_sqlite", _recording_connect)

    result = runner.invoke(
        app,
        ["export", "--output", str(output), "--db-path", str(db)],
    )

    assert result.exit_code == 0, result.output
    assert connection_count == 1
    assert statements[0] == "BEGIN"
    assert "COMMIT" in statements


def test_backup_overwrite_preserves_existing_file_on_failure(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = _db(tmp_path)
    backup_path = tmp_path / "backup.db"
    backup_path.write_text("old backup", encoding="utf-8")

    from tm.commands import export as export_cmd

    real_connect = export_cmd.connect_sqlite

    def _failing_connect(path, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        if Path(path).name.startswith(".backup.db."):
            raise RuntimeError("backup failed")
        return real_connect(path, *args, **kwargs)

    monkeypatch.setattr(export_cmd, "connect_sqlite", _failing_connect)

    result = runner.invoke(
        app,
        ["backup", "--output", str(backup_path), "--overwrite", "--db-path", str(db)],
    )

    assert result.exit_code == 1
    assert backup_path.read_text(encoding="utf-8") == "old backup"
    assert list(tmp_path.glob(".backup.db.*.tmp")) == []
