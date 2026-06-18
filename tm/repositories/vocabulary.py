"""VocabularyRepository — SQLite-backed CRUD for canonical activity vocabulary.

Connection management: opens a fresh ``sqlite3.Connection`` per call (no
shared long-lived connection) so that repository instances are lightweight and
safe to use from tests with isolated ``tmp_path`` databases.

Do NOT modify ``tm/store.py`` or ``tm/stores/sqlite_store.py`` from this
module.  Vocabulary queries run independently via direct ``sqlite3`` usage.

Scope (T-VOC-01):
- Table DDL lives in migrations/0004_vocab.sql.
- Seeding the 16 starter activities is done by ``seed_starter_vocabulary()``.
- Alias seeding (≤5 obvious aliases) is done by ``seed_starter_aliases()``.
- NO LLM soft-alignment, novelty detection, or drift detection — those are
  T-VOC-02 / T-VOC-03.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from tm.security import connect_sqlite, enable_wal_mode

__all__ = ["VocabularyEntry", "VocabularyRepository"]

# ISO 8601 UTC format used throughout this project (mirrors goals.py).
_ISO_FMT = "%Y-%m-%dT%H:%M:%SZ"


def _now_iso() -> str:
    """Return current UTC time as ISO-8601 with trailing Z.

    DEFAULT (datetime('now')) in migrations/0004_vocab.sql is a fallback;
    we always pass _now_iso() to keep ISO-T-Z format consistent across project.
    """
    return datetime.now(UTC).strftime(_ISO_FMT)


# ---------------------------------------------------------------------------
# Starter data constants
# ---------------------------------------------------------------------------

STARTER_VOCABULARY: tuple[str, ...] = (
    "deep_work",
    "shallow_work",
    "meeting",
    "email",
    "code_review",
    "exercise",
    "meal",
    "commute",
    "errand",
    "family_time",
    "social",
    "solo_recreation",
    "learning",
    "rest",
    "sleep",
    "interruption",
)
"""Exactly 16 canonical activity names seeded by ``seed_starter_vocabulary()``."""

# ≤5 highest-confidence aliases: common free-text variants that unambiguously
# map to a single canonical activity.
STARTER_ALIASES: tuple[tuple[str, str], ...] = (
    ("workout", "exercise"),
    ("lunch", "meal"),
    ("breakfast", "meal"),
    ("dinner", "meal"),
    ("standup", "meeting"),
)
"""(free_text_variant, canonical_activity) pairs seeded by
``seed_starter_aliases()``."""


# ---------------------------------------------------------------------------
# Dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class VocabularyEntry:
    """Immutable snapshot of one vocabulary row."""

    activity_name: str
    description: str | None
    vocab_version: str
    added_at: str
    status: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _open_conn(db_path: str) -> sqlite3.Connection:
    conn = connect_sqlite(db_path, row_factory=True)
    conn.execute("PRAGMA foreign_keys=ON")
    enable_wal_mode(conn, db_path)
    return conn


def _row_to_entry(row: sqlite3.Row) -> VocabularyEntry:
    return VocabularyEntry(
        activity_name=row["activity_name"],
        description=row["description"],
        vocab_version=row["vocab_version"],
        added_at=row["added_at"],
        status=row["status"],
    )


# ---------------------------------------------------------------------------
# Repository
# ---------------------------------------------------------------------------


class VocabularyRepository:
    """CRUD repository for the ``vocabulary`` and ``aliases`` tables.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file, or ``":memory:"`` for tests.  The
        caller is responsible for having applied migrations before using the
        repository (the ``vocabulary`` and ``aliases`` tables must exist).
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = str(db_path)

    @property
    def db_path(self) -> Path | str:
        """Read-only path to the SQLite database file."""
        return self._db_path

    # ------------------------------------------------------------------
    # Seeding
    # ------------------------------------------------------------------

    def seed_starter_vocabulary(self) -> int:
        """Idempotently insert the 16 starter activity names.

        Uses ``INSERT OR IGNORE`` so repeated calls are safe.

        Returns
        -------
        int
            Number of rows actually inserted (0 on subsequent calls).
        """
        conn = _open_conn(self._db_path)
        try:
            cursor = conn.cursor()
            total_inserted = 0
            for name in STARTER_VOCABULARY:
                cursor.execute(
                    "INSERT OR IGNORE INTO vocabulary (activity_name, added_at) "
                    "VALUES (?, ?)",
                    (name, _now_iso()),
                )
                total_inserted += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return total_inserted

    def seed_starter_aliases(self) -> int:
        """Idempotently insert the starter alias rows (≤5).

        Uses ``INSERT OR IGNORE`` so repeated calls are safe.  The canonical
        activity names referenced here must already exist in the ``vocabulary``
        table (call :meth:`seed_starter_vocabulary` first).

        Returns
        -------
        int
            Number of rows actually inserted (0 on subsequent calls).
        """
        conn = _open_conn(self._db_path)
        try:
            cursor = conn.cursor()
            total_inserted = 0
            for variant, canonical in STARTER_ALIASES:
                cursor.execute(
                    "INSERT OR IGNORE INTO aliases "
                    "(free_text_variant, canonical_activity, created_at) "
                    "VALUES (?, ?, ?)",
                    (variant, canonical, _now_iso()),
                )
                total_inserted += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        return total_inserted

    # ------------------------------------------------------------------
    # Read operations
    # ------------------------------------------------------------------

    def list_active(self) -> list[VocabularyEntry]:
        """Return all active vocabulary entries sorted by activity_name."""
        conn = _open_conn(self._db_path)
        try:
            rows = conn.execute(
                "SELECT activity_name, description, vocab_version, added_at, status "
                "FROM vocabulary WHERE status = 'active' ORDER BY activity_name",
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_entry(r) for r in rows]

    def list_all(self) -> list[VocabularyEntry]:
        """Return all vocabulary entries (active and archived), sorted by name."""
        conn = _open_conn(self._db_path)
        try:
            rows = conn.execute(
                "SELECT activity_name, description, vocab_version, added_at, status "
                "FROM vocabulary ORDER BY activity_name",
            ).fetchall()
        finally:
            conn.close()
        return [_row_to_entry(r) for r in rows]

    def get(self, activity_name: str) -> VocabularyEntry | None:
        """Return the entry for ``activity_name``, or ``None`` if not found."""
        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT activity_name, description, vocab_version, added_at, status "
                "FROM vocabulary WHERE activity_name = ?",
                (activity_name,),
            ).fetchone()
        finally:
            conn.close()
        return _row_to_entry(row) if row is not None else None

    def resolve(self, label: str) -> str | None:
        """Resolve a free-text label to a canonical activity name.

        Lookup order:
        1. ``label.lower()`` directly in ``vocabulary`` → return as-is.
        2. ``label.lower()`` in ``aliases.free_text_variant`` → return
           ``canonical_activity``.
        3. ``None`` if neither matches.

        Case-insensitive: the input is lowercased before lookup. Vocabulary
        entries and aliases are stored lowercase so this is sufficient.
        """
        lowered = label.lower()
        conn = _open_conn(self._db_path)
        try:
            # Check vocabulary first
            row = conn.execute(
                "SELECT activity_name FROM vocabulary WHERE activity_name = ?",
                (lowered,),
            ).fetchone()
            if row is not None:
                return str(row["activity_name"])

            # Check aliases
            row = conn.execute(
                "SELECT canonical_activity FROM aliases WHERE free_text_variant = ?",
                (lowered,),
            ).fetchone()
            if row is not None:
                return str(row["canonical_activity"])
        finally:
            conn.close()
        return None

    # ------------------------------------------------------------------
    # Write operations
    # ------------------------------------------------------------------

    def add_alias(self, free_text_variant: str, canonical_activity: str) -> None:
        """Add a free-text alias pointing to an existing canonical activity.

        Parameters
        ----------
        free_text_variant:
            The free-text label to recognise.  Automatically stripped and
            lowercased before storage so subsequent :meth:`resolve` calls
            (which lowercase their input) will find the alias regardless of
            the case supplied by the caller.
        canonical_activity:
            Must already exist in the ``vocabulary`` table.

        Raises
        ------
        ValueError
            If ``free_text_variant`` is empty after stripping whitespace, or
            if ``canonical_activity`` is not present in the vocabulary.
        """
        lower_strip = free_text_variant.strip().lower()
        if not lower_strip:
            raise ValueError("free_text_variant must be non-empty after strip")

        conn = _open_conn(self._db_path)
        try:
            row = conn.execute(
                "SELECT activity_name FROM vocabulary WHERE activity_name = ?",
                (canonical_activity,),
            ).fetchone()
            if row is None:
                raise ValueError(
                    f"Cannot add alias: canonical activity {canonical_activity!r} "
                    "not found in vocabulary"
                )
            conn.execute(
                "INSERT OR REPLACE INTO aliases "
                "(free_text_variant, canonical_activity, created_at) "
                "VALUES (?, ?, ?)",
                (lower_strip, canonical_activity, _now_iso()),
            )
            conn.commit()
        finally:
            conn.close()

    def add_canonical(
        self,
        activity_name: str,
        description: str | None = None,
    ) -> VocabularyEntry:
        """Insert a new canonical activity into the vocabulary.

        Parameters
        ----------
        activity_name:
            Must be non-empty and fully lowercase (no uppercase characters).
        description:
            Optional human-readable description of the activity.

        Returns
        -------
        VocabularyEntry
            The freshly inserted row fetched from the database.

        Raises
        ------
        ValueError
            If ``activity_name`` is empty or contains uppercase characters,
            or if the ``activity_name`` already exists in the vocabulary
            (duplicate PK).

        Notes
        -----
        Uses ``INSERT OR ABORT`` so SQLite raises ``IntegrityError`` on a
        duplicate PK; the method catches that and re-raises as
        ``ValueError("activity already exists: <name>")``.
        """
        if not activity_name:
            raise ValueError("activity_name must not be empty")
        if activity_name != activity_name.lower():
            raise ValueError(f"activity_name must be lowercase, got {activity_name!r}")

        conn = _open_conn(self._db_path)
        try:
            try:
                conn.execute(
                    "INSERT OR ABORT INTO vocabulary "
                    "(activity_name, description, added_at) "
                    "VALUES (?, ?, ?)",
                    (activity_name, description, _now_iso()),
                )
                conn.commit()
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"activity already exists: {activity_name}") from exc

            row = conn.execute(
                "SELECT activity_name, description, vocab_version, added_at, status "
                "FROM vocabulary WHERE activity_name = ?",
                (activity_name,),
            ).fetchone()
        finally:
            conn.close()

        if row is None:  # pragma: no cover
            raise RuntimeError(
                f"failed to fetch newly inserted row for {activity_name!r}"
            )
        return _row_to_entry(row)

    def archive(self, activity_name: str) -> None:
        """Set ``status='archived'`` for an activity.

        Idempotent: no-op if the activity is already archived.  Does not delete
        the row (FK safety: aliases referencing this activity are preserved).

        Parameters
        ----------
        activity_name:
            Must exist in the vocabulary (silently ignored if not found so
            callers can be idempotent regardless).
        """
        conn = _open_conn(self._db_path)
        try:
            conn.execute(
                "UPDATE vocabulary SET status='archived' WHERE activity_name = ?",
                (activity_name,),
            )
            conn.commit()
        finally:
            conn.close()
