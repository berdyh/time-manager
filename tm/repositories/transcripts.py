"""Transcript retention for debrief and voice capture replay."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from tm.security import connect_sqlite, enable_wal_mode, harden_sqlite_file_permissions

__all__ = ["TranscriptRecord", "TranscriptRepository"]

_MERGE_SEPARATOR = "\n\n--- additional transcript ({source}) ---\n"
_MERGE_SEPARATOR_RE = re.compile(r"\n\n--- additional transcript \([^)]+\) ---\n")


@dataclass(frozen=True)
class TranscriptRecord:
    case_date: str
    transcript_text: str
    source: str
    extractor_version: str | None
    recorded_at: str


def _row_to_record(row: sqlite3.Row) -> TranscriptRecord:
    return TranscriptRecord(
        case_date=row["case_date"],
        transcript_text=row["transcript_text"],
        source=row["source"],
        extractor_version=row["extractor_version"],
        recorded_at=row["recorded_at"],
    )


def _transcript_segments(transcript_text: str) -> set[str]:
    return {
        segment.strip()
        for segment in _MERGE_SEPARATOR_RE.split(transcript_text.strip())
        if segment.strip()
    }


class TranscriptRepository:
    def __init__(self, db_path: str | Path) -> None:
        self._db_path = str(db_path)

    def upsert(
        self,
        *,
        case_date: str,
        transcript_text: str,
        source: str,
        extractor_version: str | None = None,
    ) -> None:
        if not case_date:
            raise ValueError("case_date must be non-empty")
        if not transcript_text or not transcript_text.strip():
            raise ValueError("transcript_text must be non-empty")
        clean_text = transcript_text.strip()
        conn = connect_sqlite(self._db_path, isolation_level=None, row_factory=True)
        try:
            enable_wal_mode(conn, self._db_path)
            conn.execute("BEGIN IMMEDIATE")
            existing = conn.execute(
                "SELECT transcript_text FROM transcripts WHERE case_date=?",
                (case_date,),
            ).fetchone()
            if existing is None:
                conn.execute(
                    "INSERT INTO transcripts "
                    "(case_date, transcript_text, source, extractor_version) "
                    "VALUES (?, ?, ?, ?)",
                    (case_date, clean_text, source, extractor_version),
                )
            else:
                existing_text = str(existing["transcript_text"]).strip()
                if clean_text in _transcript_segments(existing_text):
                    merged_text = existing_text
                else:
                    merged_text = (
                        existing_text
                        + _MERGE_SEPARATOR.format(source=source)
                        + clean_text
                    )
                conn.execute(
                    "UPDATE transcripts SET transcript_text=?, source=?, "
                    "extractor_version=?, recorded_at=datetime('now') "
                    "WHERE case_date=?",
                    (merged_text, "merged", extractor_version, case_date),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            harden_sqlite_file_permissions(self._db_path)
            conn.close()

    def get(self, case_date: str) -> TranscriptRecord | None:
        conn = connect_sqlite(self._db_path, row_factory=True)
        try:
            row = conn.execute(
                "SELECT case_date, transcript_text, source, extractor_version, "
                "recorded_at FROM transcripts WHERE case_date = ?",
                (case_date,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_record(row) if row is not None else None

    def delete(self, case_date: str) -> int:
        conn = connect_sqlite(self._db_path, row_factory=True)
        try:
            enable_wal_mode(conn, self._db_path)
            cur = conn.execute(
                "DELETE FROM transcripts WHERE case_date = ?",
                (case_date,),
            )
            conn.commit()
            return int(cur.rowcount)
        except Exception:
            conn.rollback()
            raise
        finally:
            harden_sqlite_file_permissions(self._db_path)
            conn.close()
