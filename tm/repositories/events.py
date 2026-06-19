"""EventsRepository — SQLite-backed write and query path for events.

This is the canonical write path for events going forward.  The legacy
``Store.append_event`` (tm/store.py) remains available for backward compatibility.

**case_date sentinel:** Empty-string ``case_date`` (``''``) is a sentinel meaning
'unset'.  The ``list_distinct_case_dates`` helper filters these out; downstream
consumers (T-OUT-01, T-PM-02, etc.) must filter ``case_date <> ''`` if they read
events directly.  ``_derive_case_date`` never returns ``''`` — it raises
``ValueError`` on malformed input instead.

**until semantics differ by filter domain:** ``query_events(since=None,
until=None)`` filters continuous timestamps and treats ``until`` as an
exclusive upper bound (``timestamp < ?``), matching standard half-open time
intervals.  ``list_distinct_case_dates(since=None, until=None)`` filters
discrete day-bucket strings and treats ``until`` as inclusive
(``case_date <= ?``), matching the human reading of "from May 1 to May 31".

Design notes:
- Opens a fresh ``sqlite3.Connection`` per call (no shared long-lived connection).
- ``advances_goal`` is stored as a soft FK (TEXT column, no REFERENCES constraint).
  Application-layer validation ensures the referenced goal exists before insert.
  The formal REFERENCES goals(goal_id) FK + table rebuild is deferred to a
  future infrastructure task that modifies the migration runner to support a
  pre-txn pragma kind (since SQLite ALTER limitations require
  ``PRAGMA foreign_keys=OFF`` outside any transaction).
- ``case_goal_id`` is also a soft FK to ``goals(goal_id)`` — validated the same way.
  Note: ``case_goal_id`` (case-lens identifier for goal-pursuit cases) and
  ``advances_goal`` (per-event tag of which goal this contributes toward) are
  orthogonal — an event may legitimately tag-as-advancing one goal while being
  part of a different goal-pursuit case lens.
- ``attributes`` are round-tripped via JSON (``attributes_json`` column).
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from tm.security import connect_sqlite, enable_wal_mode

__all__ = ["EventsRepository"]


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = connect_sqlite(db_path, row_factory=True)
    conn.execute("PRAGMA foreign_keys=ON")
    enable_wal_mode(conn, db_path)
    return conn


_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _derive_case_date(timestamp_iso: str) -> str:
    """Derive the ``case_date`` (YYYY-MM-DD) prefix from an ISO timestamp.

    Accepts:

    - A full ISO 8601 timestamp with ``T`` separator, e.g.
      ``2026-05-05T10:00:00Z`` → ``2026-05-05``.
    - A space-separated datetime (SQLite's ``datetime('now')`` format), e.g.
      ``2026-05-05 10:00:00`` → ``2026-05-05``.
    - A bare date, e.g. ``2026-05-05`` → ``2026-05-05``.

    Raises ``ValueError`` for malformed input (so callers never silently store
    an empty ``case_date`` sentinel) and for impossible calendar dates such as
    month 13, day 0, etc.

    Steps:

    1. Accept input; strip surrounding whitespace.
    2. Pre-process: if the string contains ``'T'`` or ``' '``, take the prefix
       before the first such separator; otherwise treat the whole string as the
       candidate date.
    3. Verify the candidate matches ``r'^\\d{4}-\\d{2}-\\d{2}$'``.
    4. Validate via ``datetime.fromisoformat`` to reject impossible calendar
       dates (month > 12, day 0, etc.).  Note: year 0 is also rejected by
       Python's ``datetime`` (must be 1–9999).
    5. Return the validated ``YYYY-MM-DD`` string.
    """
    if not isinstance(timestamp_iso, str) or not timestamp_iso:
        raise ValueError(
            f"invalid timestamp for case_date derivation: {timestamp_iso!r}"
        )

    raw = timestamp_iso.strip()

    # Step 2: split on 'T' or ' ' to isolate the date prefix.
    for sep in ("T", " "):
        if sep in raw:
            raw = raw.split(sep, 1)[0]
            break

    # Step 3: digit-width check.
    if not _DATE_RE.match(raw):
        raise ValueError(
            f"invalid timestamp for case_date derivation: {timestamp_iso!r}"
        )

    # Step 4: calendar validity check via fromisoformat.
    try:
        datetime.fromisoformat(raw)
    except ValueError:
        raise ValueError(f"invalid calendar date: {timestamp_iso!r}") from None

    # Step 5: return the validated date string.
    return raw


def _row_to_event(row: sqlite3.Row) -> dict[str, Any]:
    out: dict[str, Any] = {
        "event_id": row["event_id"],
        "case_id": row["case_id"],
        "activity": row["activity"],
        "timestamp": row["timestamp"],
        "lifecycle": row["lifecycle"],
        "resource": row["resource"],
        "extractor_version": row["extractor_version"],
        "created_at": row["created_at"],
        "advances_goal": row["advances_goal"],
        "vocab_version": row["vocab_version"],
        "schema_version": row["schema_version"],
        "case_date": row["case_date"],
        "case_goal_id": row["case_goal_id"],
    }
    raw = row["attributes_json"] or "{}"
    try:
        out["attributes"] = json.loads(raw)
    except json.JSONDecodeError:
        out["attributes"] = {}
    return out


def _ensure_goal_exists(conn: sqlite3.Connection, goal_id: str) -> None:
    row = conn.execute("SELECT 1 FROM goals WHERE goal_id = ?", (goal_id,)).fetchone()
    if row is None:
        raise ValueError(f"unknown goal: {goal_id!r}")


class EventsRepository:
    """Repository for the ``events`` table.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file, or ``":memory:"`` for tests.  The
        caller is responsible for having applied migrations before using the
        repository (the ``events`` table with the XES columns from migration
        0006 must exist).
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def append_event(
        self,
        *,
        event_id: str,
        case_id: str,
        activity: str,
        timestamp: str,
        lifecycle: str,
        resource: str | None = None,
        attributes: dict[str, Any] | None = None,
        extractor_version: str = "v0",
        advances_goal: str | None = None,
        vocab_version: str | None = None,
        schema_version: str = "v1",
        case_date: str | None = None,
        case_goal_id: str | None = None,
    ) -> None:
        """Insert one event row.

        Parameters
        ----------
        event_id:
            Unique identifier for this event (e.g. a ULID).
        case_id:
            Process instance / trace identifier.
        activity:
            Activity label (e.g. ``"task:start"``).
        timestamp:
            ISO 8601 UTC string.
        lifecycle:
            One of ``'start'``, ``'complete'``, ``'suspend'``, ``'resume'``.
        resource:
            Optional resource identifier (user, service, etc.).
        attributes:
            Optional dict of extra attributes; stored as JSON.
        extractor_version:
            Version tag of the extractor that produced this event.
        advances_goal:
            Optional goal_id this event contributes toward.  If provided, the
            goal must already exist in the ``goals`` table; otherwise
            :exc:`ValueError` is raised.
        vocab_version:
            Optional version tag of the activity vocabulary in use when this
            event was emitted.  Pre-T-VOC-* events have no version tag.
        schema_version:
            XES schema version this event conforms to.  Defaults to ``"v1"``.
        case_date:
            Date-portion of the workday case lens.  When omitted, derived from
            ``timestamp`` via :func:`_derive_case_date`.  A malformed
            ``timestamp`` raises :exc:`ValueError` *before* INSERT to prevent
            silently storing an empty sentinel for valid events.
        case_goal_id:
            Optional goal_id identifying the goal-pursuit case lens.  If
            provided, the goal must exist (same soft-FK semantics as
            ``advances_goal``).  Note that ``advances_goal`` and
            ``case_goal_id`` are orthogonal: they need not refer to the same
            goal.
        """
        if attributes is not None and not isinstance(attributes, dict):
            raise TypeError("'attributes' must be a dict if provided")
        attributes_json = json.dumps(attributes or {}, sort_keys=True, default=str)

        # Resolve case_date BEFORE opening the connection so a malformed
        # timestamp cannot leave a row with case_date='' from a partial INSERT.
        if case_date is None:
            case_date = _derive_case_date(timestamp)

        conn = _open_conn(self._db_path)
        try:
            if advances_goal is not None:
                _ensure_goal_exists(conn, advances_goal)
            if case_goal_id is not None:
                _ensure_goal_exists(conn, case_goal_id)

            conn.execute(
                "INSERT INTO events ("
                "event_id, case_id, activity, timestamp, lifecycle, "
                "resource, attributes_json, extractor_version, advances_goal, "
                "vocab_version, schema_version, case_date, case_goal_id"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    event_id,
                    case_id,
                    activity,
                    timestamp,
                    lifecycle,
                    resource,
                    attributes_json,
                    extractor_version,
                    advances_goal,
                    vocab_version,
                    schema_version,
                    case_date,
                    case_goal_id,
                ),
            )
            conn.commit()
        finally:
            conn.close()

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def query_events(
        self,
        *,
        case_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        activity: str | None = None,
        advances_goal: str | None = None,
        vocab_version: str | None = None,
        schema_version: str | None = None,
        case_date: str | None = None,
        case_goal_id: str | None = None,
        limit: int | None = None,
        order: Literal["asc", "desc"] = "asc",
    ) -> list[dict[str, Any]]:
        """Return events matching the given filters.

        Parameters
        ----------
        case_id:
            Filter by process instance identifier.
        since:
            Lower bound (inclusive) on ``timestamp``.
        until:
            Upper bound (exclusive) on ``timestamp``.
        activity:
            Filter by exact activity label.
        advances_goal:
            Filter by goal_id in ``advances_goal`` column.
        vocab_version:
            Filter by ``vocab_version`` column.
        schema_version:
            Filter by ``schema_version`` column.
        case_date:
            Filter by ``case_date`` column (workday case lens).
        case_goal_id:
            Filter by ``case_goal_id`` column (goal-pursuit case lens).
        limit:
            Maximum number of rows to return.
        order:
            Timestamp ordering for the result set.
        """
        if order not in {"asc", "desc"}:
            raise ValueError("order must be 'asc' or 'desc'")

        clauses: list[str] = []
        params: list[Any] = []

        if case_id is not None:
            clauses.append("case_id = ?")
            params.append(case_id)
        if since is not None:
            clauses.append("timestamp >= ?")
            params.append(since)
        if until is not None:
            clauses.append("timestamp < ?")
            params.append(until)
        if activity is not None:
            clauses.append("activity = ?")
            params.append(activity)
        if advances_goal is not None:
            clauses.append("advances_goal = ?")
            params.append(advances_goal)
        if vocab_version is not None:
            clauses.append("vocab_version = ?")
            params.append(vocab_version)
        if schema_version is not None:
            clauses.append("schema_version = ?")
            params.append(schema_version)
        if case_date is not None:
            clauses.append("case_date = ?")
            params.append(case_date)
        if case_goal_id is not None:
            clauses.append("case_goal_id = ?")
            params.append(case_goal_id)

        sql = (
            "SELECT event_id, case_id, activity, timestamp, lifecycle, "
            "resource, attributes_json, extractor_version, created_at, advances_goal, "
            "vocab_version, schema_version, case_date, case_goal_id "
            "FROM events"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        direction = "DESC" if order == "desc" else "ASC"
        sql += f" ORDER BY timestamp {direction}, event_id {direction}"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        conn = _open_conn(self._db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()

        return [_row_to_event(r) for r in rows]

    def count_advancing_goal(self, goal_id: str) -> int:
        """Return the number of events that advance the given goal.

        This is a helper for outcome-scoring components (e.g. T-OUT-01).

        Parameters
        ----------
        goal_id:
            The goal to count events for.
        """
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) FROM events WHERE advances_goal = ?",
                (goal_id,),
            ).fetchone()
        finally:
            conn.close()

        return int(row[0]) if row is not None else 0

    def list_distinct_case_dates(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[str]:
        """Return distinct ``case_date`` values in ``[since, until]``, sorted ASC.

        Empty-string ``case_date`` values (the unset sentinel) are excluded.
        Consumers who care about backfilled events only should rely on this
        helper rather than scanning the column directly.

        Parameters
        ----------
        since:
            Lower bound (inclusive) on ``case_date``.
        until:
            Upper bound (inclusive) on ``case_date``.
        """
        clauses: list[str] = ["case_date <> ''"]
        params: list[Any] = []
        if since is not None:
            clauses.append("case_date >= ?")
            params.append(since)
        if until is not None:
            clauses.append("case_date <= ?")
            params.append(until)

        sql = (
            "SELECT DISTINCT case_date FROM events WHERE "
            + " AND ".join(clauses)
            + " ORDER BY case_date ASC"
        )

        conn = _open_conn(self._db_path)
        try:
            rows = conn.execute(sql, params).fetchall()
        finally:
            conn.close()
        return [str(r["case_date"]) for r in rows]
