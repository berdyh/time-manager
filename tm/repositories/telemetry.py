"""SuggestionTelemetryRepository — SQLite-backed store for suggestion telemetry.

Stores predicted-vs-actual outcome data so downstream tasks can compute
delta-outcome learning signals.

Connection management: opens a fresh ``sqlite3.Connection`` per call (no
shared long-lived connection) so that repository instances are lightweight and
safe to use from tests with isolated ``tmp_path`` databases.

Do NOT modify ``tm/store.py`` or ``tm/stores/sqlite_store.py`` from this module.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tm.models.goals import ulid

__all__ = ["SuggestionTelemetryRepository", "SuggestionRecord"]

# ISO 8601 UTC format pinned across this project.
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    return datetime.now(UTC).strftime(_ISO_FMT)


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@dataclass(frozen=True)
class SuggestionRecord:
    """Immutable snapshot of a suggestion_telemetry row."""

    suggestion_id: str
    suggested_at: str
    case_date: str
    case_goal_id: str | None
    recommended_action: str
    predicted_outcome_with: float
    predicted_outcome_without: float
    predicted_outcome_delta: float
    conformance_deviation: float | None
    actual_outcome: int | None
    explicit_thumbs: int | None
    llm_explanation_text: str | None
    created_at: str


def _row_to_record(row: sqlite3.Row) -> SuggestionRecord:
    return SuggestionRecord(
        suggestion_id=row["suggestion_id"],
        suggested_at=row["suggested_at"],
        case_date=row["case_date"],
        case_goal_id=row["case_goal_id"],
        recommended_action=row["recommended_action"],
        predicted_outcome_with=row["predicted_outcome_with"],
        predicted_outcome_without=row["predicted_outcome_without"],
        predicted_outcome_delta=row["predicted_outcome_delta"],
        conformance_deviation=row["conformance_deviation"],
        actual_outcome=row["actual_outcome"],
        explicit_thumbs=row["explicit_thumbs"],
        llm_explanation_text=row["llm_explanation_text"],
        created_at=row["created_at"],
    )


_SELECT_COLS = (
    "suggestion_id, suggested_at, case_date, case_goal_id, recommended_action, "
    "predicted_outcome_with, predicted_outcome_without, predicted_outcome_delta, "
    "conformance_deviation, actual_outcome, explicit_thumbs, llm_explanation_text, "
    "created_at"
)


class SuggestionTelemetryRepository:
    """CRUD repository for the ``suggestion_telemetry`` table.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file, or ``":memory:"`` for tests.  The
        caller is responsible for having applied migration 0007 before use.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def log_suggestion(
        self,
        *,
        suggestion_id: str | None = None,
        case_date: str,
        case_goal_id: str | None = None,
        recommended_action: str,
        predicted_outcome_with: float,
        predicted_outcome_without: float,
        conformance_deviation: float | None = None,
        llm_explanation_text: str | None = None,
    ) -> SuggestionRecord:
        """Insert a new suggestion telemetry row and return the persisted record.

        Parameters
        ----------
        suggestion_id:
            Optional caller-supplied ID.  If ``None``, a ULID is generated.
        case_date:
            The date lens for this suggestion (non-empty string, e.g. ``"2026-05-05"``).
        case_goal_id:
            Optional goal ID associated with the case lens.
        recommended_action:
            The action that was recommended (non-empty string).
        predicted_outcome_with:
            Predicted outcome if the recommendation is followed. Must be in [0, 2].
        predicted_outcome_without:
            Predicted outcome without the recommendation. Must be in [0, 2].
        conformance_deviation:
            Optional distance metric from current case to discovered-process model.
        llm_explanation_text:
            Optional free-text explanation from the LLM.
        """
        if not case_date or not isinstance(case_date, str):
            raise ValueError("case_date must be a non-empty string")
        if not recommended_action or not isinstance(recommended_action, str):
            raise ValueError("recommended_action must be a non-empty string")
        if not (0.0 <= predicted_outcome_with <= 2.0):
            raise ValueError(
                "predicted_outcome_with must be in [0, 2], "
                f"got {predicted_outcome_with!r}"
            )
        if not (0.0 <= predicted_outcome_without <= 2.0):
            raise ValueError(
                "predicted_outcome_without must be in [0, 2], "
                f"got {predicted_outcome_without!r}"
            )

        sid = suggestion_id if suggestion_id is not None else ulid()
        suggested_at = _now_iso()
        predicted_outcome_delta = predicted_outcome_with - predicted_outcome_without

        conn = _open_conn(self._db_path)
        try:
            conn.execute(
                "INSERT INTO suggestion_telemetry "
                "(suggestion_id, suggested_at, case_date, case_goal_id, "
                "recommended_action, predicted_outcome_with, "
                "predicted_outcome_without, predicted_outcome_delta, "
                "conformance_deviation, llm_explanation_text) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sid,
                    suggested_at,
                    case_date,
                    case_goal_id,
                    recommended_action,
                    predicted_outcome_with,
                    predicted_outcome_without,
                    predicted_outcome_delta,
                    conformance_deviation,
                    llm_explanation_text,
                ),
            )
            conn.commit()
            row = conn.execute(
                "SELECT "
                + _SELECT_COLS
                + " FROM suggestion_telemetry WHERE suggestion_id = ?",
                (sid,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:  # pragma: no cover — should never happen
            raise RuntimeError(f"Suggestion {sid!r} vanished after insert")
        return _row_to_record(row)

    def record_actual_outcome(self, suggestion_id: str, actual_outcome: int) -> None:
        """Record the actual observed outcome for a prior suggestion.

        Parameters
        ----------
        suggestion_id:
            ID of the suggestion to update.
        actual_outcome:
            Integer in {0, 1, 2}.  Raises ``ValueError`` for any other value.
        """
        if actual_outcome not in (0, 1, 2):
            raise ValueError(
                f"actual_outcome must be 0, 1, or 2; got {actual_outcome!r}"
            )
        conn = _open_conn(self._db_path)
        try:
            cursor = conn.execute(
                "UPDATE suggestion_telemetry "
                "SET actual_outcome=? WHERE suggestion_id=?",
                (actual_outcome, suggestion_id),
            )
            conn.commit()
            affected = cursor.rowcount
        finally:
            conn.close()

        if affected == 0:
            raise ValueError(f"unknown suggestion_id: {suggestion_id!r}")

    def record_thumbs(self, suggestion_id: str, thumbs: bool) -> None:
        """Record explicit thumbs-up (True) or thumbs-down (False) feedback.

        Parameters
        ----------
        suggestion_id:
            ID of the suggestion to update.
        thumbs:
            ``True`` for thumbs-up (stored as 1),
            ``False`` for thumbs-down (stored as 0).
        """
        thumbs_int = 1 if thumbs else 0
        conn = _open_conn(self._db_path)
        try:
            cursor = conn.execute(
                "UPDATE suggestion_telemetry "
                "SET explicit_thumbs=? WHERE suggestion_id=?",
                (thumbs_int, suggestion_id),
            )
            conn.commit()
            affected = cursor.rowcount
        finally:
            conn.close()

        if affected == 0:
            raise ValueError(f"unknown suggestion_id: {suggestion_id!r}")

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def get(self, suggestion_id: str) -> SuggestionRecord | None:
        """Return the record with the given ID, or ``None`` if not found."""
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT "
                + _SELECT_COLS
                + " FROM suggestion_telemetry WHERE suggestion_id = ?",
                (suggestion_id,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_record(row) if row is not None else None

    def list_recent(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        limit: int | None = None,
    ) -> list[SuggestionRecord]:
        """Return records ordered by suggested_at DESC, then suggestion_id DESC.

        Parameters
        ----------
        since:
            Optional ISO timestamp lower bound (inclusive).
        until:
            Optional ISO timestamp upper bound (inclusive).
        limit:
            Optional maximum number of records to return.
        """
        query = f"SELECT {_SELECT_COLS} FROM suggestion_telemetry WHERE 1=1"
        params: list[Any] = []

        if since is not None:
            query += " AND suggested_at >= ?"
            params.append(since)
        if until is not None:
            query += " AND suggested_at <= ?"
            params.append(until)

        query += " ORDER BY suggested_at DESC, suggestion_id DESC"

        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)

        conn = _open_conn(self._db_path)
        try:
            rows = conn.execute(query, params).fetchall()
        finally:
            conn.close()

        return [_row_to_record(r) for r in rows]

    def count_for_case_date(self, case_date: str) -> int:
        """Return the number of suggestions logged for ``case_date``."""
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM suggestion_telemetry "
                "INDEXED BY idx_suggestion_telemetry_case_date "
                "WHERE case_date = ?",
                (case_date,),
            ).fetchone()
        finally:
            conn.close()

        return int(row["cnt"]) if row is not None else 0

    def delta_outcome_summary(
        self,
        *,
        since: str,
        until: str | None = None,
    ) -> dict[str, Any]:
        """Aggregate predicted vs actual outcome statistics over a time window.

        Parameters
        ----------
        since:
            ISO timestamp lower bound (inclusive).
        until:
            Optional ISO timestamp upper bound (inclusive).

        Returns
        -------
        dict with keys:
            - ``count``: total suggestions in window
            - ``count_with_actual``: suggestions where actual_outcome is set
            - ``mean_predicted_delta``: average predicted_outcome_delta over rows
              with actual_outcome set (``None`` if count_with_actual == 0)
            - ``mean_actual_minus_predicted``: average
              ``(actual_outcome - predicted_outcome_delta)`` over rows with
              actual_outcome set (``None`` if count_with_actual == 0)
        """
        base_where = "suggested_at >= ?"
        params: list[Any] = [since]

        if until is not None:
            base_where += " AND suggested_at <= ?"
            params.append(until)

        conn = _open_conn(self._db_path)
        try:
            count_row = conn.execute(
                f"SELECT COUNT(*) AS cnt FROM suggestion_telemetry WHERE {base_where}",
                params,
            ).fetchone()
            count = count_row["cnt"] if count_row else 0

            agg_row = conn.execute(
                "SELECT "
                "COUNT(*) AS cnt_actual, "
                "AVG(predicted_outcome_delta) AS mean_pred_delta, "
                "AVG(actual_outcome - predicted_outcome_delta) AS mean_act_minus_pred "
                "FROM suggestion_telemetry "
                f"WHERE actual_outcome IS NOT NULL AND {base_where}",
                params,
            ).fetchone()
        finally:
            conn.close()

        count_with_actual: int = agg_row["cnt_actual"] if agg_row else 0
        mean_predicted_delta: float | None = None
        mean_actual_minus_predicted: float | None = None

        if agg_row and count_with_actual > 0:
            mean_predicted_delta = agg_row["mean_pred_delta"]
            mean_actual_minus_predicted = agg_row["mean_act_minus_pred"]

        return {
            "count": count,
            "count_with_actual": count_with_actual,
            "mean_predicted_delta": mean_predicted_delta,
            "mean_actual_minus_predicted": mean_actual_minus_predicted,
        }
