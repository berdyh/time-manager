"""GoalsRepository — SQLite-backed CRUD for Goals.

Connection management: opens a fresh ``sqlite3.Connection`` per call (no
shared long-lived connection) so that repository instances are lightweight and
safe to use from tests with isolated ``tmp_path`` databases.

Do NOT modify ``tm/store.py`` or ``tm/stores/sqlite_store.py`` from this
module.  Goals queries run independently via direct ``sqlite3`` usage.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from tm.models.goals import Goal, ulid

__all__ = ["GoalsRepository"]

# ISO 8601 UTC format used throughout this project.
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return datetime.now(UTC).strftime(_ISO_FMT)


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def _row_to_goal(row: sqlite3.Row) -> Goal:
    return Goal(
        goal_id=row["goal_id"],
        name=row["name"],
        description=row["description"],
        status=row["status"],
        priority=row["priority"],
        target_completion_at=row["target_completion_at"],
        created_at=row["created_at"],
        completed_at=row["completed_at"],
        abandoned_at=row["abandoned_at"],
        abandon_reason=row["abandon_reason"],
    )


class GoalsRepository:
    """CRUD repository for the ``goals`` table.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file, or ``":memory:"`` for tests.  The
        caller is responsible for having applied migrations before using the
        repository (the ``goals`` table must exist).
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add(
        self,
        name: str,
        description: str | None = None,
        priority: int | None = None,
        target_completion_at: datetime | None = None,
    ) -> Goal:
        """Insert a new goal and return the persisted ``Goal``.

        Parameters
        ----------
        name:
            Short human-readable name.  Must not be empty.
        description:
            Optional longer description.
        priority:
            Optional integer in [1, 3] (1 = highest).
        target_completion_at:
            Optional deadline as a ``datetime`` object; stored as ISO 8601
            UTC string.
        """
        goal_id = ulid()
        target_str: str | None = None
        if target_completion_at is not None:
            if target_completion_at.tzinfo is not None:
                target_str = target_completion_at.astimezone(UTC).strftime(_ISO_FMT)
            else:
                target_str = target_completion_at.strftime(_ISO_FMT)

        created_at = _now_iso()
        conn = _open_conn(self._db_path)
        try:
            conn.execute(
                "INSERT INTO goals "
                "(goal_id, name, description, priority, "
                "target_completion_at, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (goal_id, name, description, priority, target_str, created_at),
            )
            conn.commit()
            row = conn.execute(
                "SELECT goal_id, name, description, status, priority, "
                "target_completion_at, created_at, completed_at, "
                "abandoned_at, abandon_reason "
                "FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:  # pragma: no cover — should never happen
            raise RuntimeError(f"Goal {goal_id!r} vanished after insert")
        return _row_to_goal(row)

    def complete(self, goal_id: str) -> Goal:
        """Mark a goal as completed.

        Raises
        ------
        ValueError
            If ``goal_id`` is unknown, or if the goal is already in a terminal
            state (``completed`` or ``abandoned``).
        """
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT goal_id, name, description, status, priority, "
                "target_completion_at, created_at, completed_at, "
                "abandoned_at, abandon_reason "
                "FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown goal_id: {goal_id!r}")
            if row["status"] != "active":
                raise ValueError(
                    f"Goal {goal_id!r} is already in terminal state {row['status']!r}"
                )
            now = _now_iso()
            conn.execute(
                "UPDATE goals SET status='completed', completed_at=? WHERE goal_id=?",
                (now, goal_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT goal_id, name, description, status, priority, "
                "target_completion_at, created_at, completed_at, "
                "abandoned_at, abandon_reason "
                "FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        finally:
            conn.close()

        return _row_to_goal(row)  # type: ignore[arg-type]

    def abandon(self, goal_id: str, reason: str | None = None) -> Goal:
        """Mark a goal as abandoned.

        Raises
        ------
        ValueError
            If ``goal_id`` is unknown, or if the goal is already in a terminal
            state.
        """
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT goal_id, name, description, status, priority, "
                "target_completion_at, created_at, completed_at, "
                "abandoned_at, abandon_reason "
                "FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
            if row is None:
                raise ValueError(f"Unknown goal_id: {goal_id!r}")
            if row["status"] != "active":
                raise ValueError(
                    f"Goal {goal_id!r} is already in terminal state {row['status']!r}"
                )
            now = _now_iso()
            conn.execute(
                "UPDATE goals SET status='abandoned', abandoned_at=?, abandon_reason=? "
                "WHERE goal_id=?",
                (now, reason, goal_id),
            )
            conn.commit()
            row = conn.execute(
                "SELECT goal_id, name, description, status, priority, "
                "target_completion_at, created_at, completed_at, "
                "abandoned_at, abandon_reason "
                "FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        finally:
            conn.close()

        return _row_to_goal(row)  # type: ignore[arg-type]

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, goal_id: str) -> Goal | None:
        """Return the ``Goal`` with the given ID, or ``None`` if not found."""
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT goal_id, name, description, status, priority, "
                "target_completion_at, created_at, completed_at, "
                "abandoned_at, abandon_reason "
                "FROM goals WHERE goal_id = ?",
                (goal_id,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_goal(row) if row is not None else None

    def list(
        self,
        status: Literal["active", "completed", "abandoned", "all"] = "all",
    ) -> list[Goal]:
        """Return goals sorted by ``created_at`` descending.

        Parameters
        ----------
        status:
            Filter by status, or ``"all"`` to return every goal.
        """
        conn = _open_conn(self._db_path)
        try:
            if status == "all":
                rows = conn.execute(
                    "SELECT goal_id, name, description, status, priority, "
                    "target_completion_at, created_at, completed_at, "
                    "abandoned_at, abandon_reason "
                    "FROM goals ORDER BY created_at DESC, goal_id DESC",
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT goal_id, name, description, status, priority, "
                    "target_completion_at, created_at, completed_at, "
                    "abandoned_at, abandon_reason "
                    "FROM goals WHERE status = ? "
                    "ORDER BY created_at DESC, goal_id DESC",
                    (status,),
                ).fetchall()
        finally:
            conn.close()
        return [_row_to_goal(r) for r in rows]
