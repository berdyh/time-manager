"""EventsRepository — SQLite-backed write and query path for events.

This is the canonical write path for events going forward.  The legacy
``Store.append_event`` (tm/store.py) remains available for backward compatibility.

Design notes:
- Opens a fresh ``sqlite3.Connection`` per call (no shared long-lived connection).
- ``advances_goal`` is stored as a soft FK (TEXT column, no REFERENCES constraint).
  Application-layer validation ensures the referenced goal exists before insert.
  The formal REFERENCES goals(goal_id) FK will be added by T-PM-01 when it
  rebuilds the events table for XES extension.
- ``attributes`` are round-tripped via JSON (``attributes_json`` column).
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

__all__ = ["EventsRepository"]


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


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
    }
    raw = row["attributes_json"] or "{}"
    try:
        out["attributes"] = json.loads(raw)
    except json.JSONDecodeError:
        out["attributes"] = {}
    return out


class EventsRepository:
    """Repository for the ``events`` table.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file, or ``":memory:"`` for tests.  The
        caller is responsible for having applied migrations before using the
        repository (the ``events`` table with the ``advances_goal`` column from
        migration 0005 must exist).
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
        """
        if attributes is not None and not isinstance(attributes, dict):
            raise TypeError("'attributes' must be a dict if provided")
        attributes_json = json.dumps(attributes or {}, sort_keys=True, default=str)

        conn = _open_conn(self._db_path)
        try:
            if advances_goal is not None:
                row = conn.execute(
                    "SELECT 1 FROM goals WHERE goal_id = ?", (advances_goal,)
                ).fetchone()
                if row is None:
                    raise ValueError(f"unknown goal: {advances_goal!r}")

            conn.execute(
                "INSERT INTO events ("
                "event_id, case_id, activity, timestamp, lifecycle, "
                "resource, attributes_json, extractor_version, advances_goal"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
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
        limit: int | None = None,
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
        limit:
            Maximum number of rows to return.
        """
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

        sql = (
            "SELECT event_id, case_id, activity, timestamp, lifecycle, "
            "resource, attributes_json, extractor_version, created_at, advances_goal "
            "FROM events"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp ASC, event_id ASC"
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
