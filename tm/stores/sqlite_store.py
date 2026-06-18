"""Concrete SQLite backend for the Store facade.

- WAL journal mode + ``foreign_keys=ON``.
- Write transactions use ``BEGIN IMMEDIATE`` and retry up to 3 times on
  ``database is locked`` errors with exponential backoff (10ms, 50ms, 200ms).
- Migration runner scans ``migrations/``, applies any version greater than
  ``MAX(applied)`` in numeric order, each in its own transaction, and records
  ``{version, applied_at, checksum}``. A previously-applied migration whose
  on-disk checksum differs from the recorded one raises
  :class:`tm.store.MigrationIntegrityError`.
- All SQL uses parameterized queries; no f-string SQL.

This module is a coordination point per the task registry: later tasks append
methods (cost ledger, vocabulary, etc.) without restructuring the class.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import re
import sqlite3
import sys
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from tm.security import connect_sqlite, enable_wal_mode

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type hints only
    pass

# Resolve to the repo's migrations/ directory by default. Tests may override.
_DEFAULT_MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations"

_MIGRATION_FILENAME_RE = re.compile(r"^(\d{4})_[A-Za-z0-9_\-]+\.sql$")

# Header directive parser: ``-- !pre-txn: <SQL>`` or ``-- !post-txn: <SQL>``.
# Tolerant of leading whitespace, optional whitespace between ``--`` and ``!``,
# and optional whitespace around the colon. Stops at first non-comment/non-empty
# line so the body splitter sees the same SQL it always has.
_HEADER_DIRECTIVE_RE = re.compile(
    r"^\s*--\s*!\s*(pre-txn|post-txn)\s*:\s*(.*?)\s*$",
)
_HEADER_COMMENT_RE = re.compile(r"^\s*--")
_HEADER_BLANK_RE = re.compile(r"^\s*$")
_UTF8_BOM = "﻿"


class MigrationError(RuntimeError):
    """Base class for migration runner failures."""


class MigrationPragmaError(MigrationError):
    """Raised for ``-- !pre-txn:`` directive failures — either at parse time
    (payload does not start with ``PRAGMA``) or at execution time (the pragma
    SQL itself fails before ``BEGIN IMMEDIATE``).

    When raised at **parse time** (payload validation), the migration body has
    not started and no ``schema_migrations`` row is written.

    When raised at **execution time** (pragma SQL failure), same: the body
    never ran and no row is written.

    Attributes:
        version: migration version whose pre-txn directive failed.
        pragma_sql: the offending directive payload string.
        cause: the underlying error (``ValueError`` for parse-time, ``sqlite3``
               error for execution-time failures).
    """

    def __init__(
        self,
        version: int,
        pragma_sql: str,
        cause: BaseException,
    ) -> None:
        super().__init__(
            f"migration {version:04d}: pre-txn pragma failed: "
            f"{pragma_sql!r} ({cause!r})"
        )
        self.version = version
        self.pragma_sql = pragma_sql
        self.cause = cause


class MigrationPostTxnError(MigrationError):
    """Raised when a ``-- !post-txn:`` pragma fails AFTER body commit.

    The body changes ARE persisted and the ``schema_migrations`` row IS
    written; only the post-txn pragma side-effect could not complete. Callers
    may need to manually re-apply the post-txn pragma.

    Attributes:
        version: migration version whose post-txn pragma failed.
        pragma_sql: the offending pragma statement.
        cause: the underlying ``sqlite3`` error.
    """

    def __init__(
        self,
        version: int,
        pragma_sql: str,
        cause: BaseException,
    ) -> None:
        super().__init__(
            f"migration {version:04d}: post-txn pragma failed AFTER commit: "
            f"{pragma_sql!r} ({cause!r})"
        )
        self.version = version
        self.pragma_sql = pragma_sql
        self.cause = cause


class MigrationBodyError(MigrationError):
    """Raised when a migration body statement fails during execution.

    Wraps ``sqlite3.IntegrityError`` and ``sqlite3.OperationalError`` raised
    from within the body transaction (e.g. CHECK / FK / UNIQUE violations,
    or referencing a nonexistent table).  This ensures ``except MigrationError:``
    catches all migration-runner failures uniformly, including body-execution
    errors that would otherwise propagate as raw ``sqlite3`` exceptions.

    NOT raised for:
    - Pre-txn pragma failures (those raise :class:`MigrationPragmaError`).
    - Post-txn pragma failures (those raise :class:`MigrationPostTxnError`).
    - Checksum drift (raises :class:`MigrationIntegrityError` via tm.store).
    - File I/O or parsing errors (not sqlite3 exceptions).

    Attributes:
        version: migration version whose body statement failed.
        cause: the underlying ``sqlite3`` error.
    """

    def __init__(self, version: int, cause: BaseException) -> None:
        super().__init__(f"migration {version:04d}: body statement failed: {cause!r}")
        self.version = version
        self.cause = cause


_REQUIRED_EVENT_KEYS: tuple[str, ...] = (
    "event_id",
    "case_id",
    "activity",
    "timestamp",
    "lifecycle",
    "extractor_version",
)

_BUSY_BACKOFFS_S: tuple[float, ...] = (0.010, 0.050, 0.200)


def _sha256_bytes(body: bytes) -> str:
    return hashlib.sha256(body).hexdigest()


def _is_locked_error(err: sqlite3.OperationalError) -> bool:
    msg = str(err).lower()
    return "database is locked" in msg or "database is busy" in msg


class SQLiteStore:
    """SQLite-backed Store. Open with a filesystem path; ``:memory:`` allowed.

    Connection management: one ``sqlite3.Connection`` per Store instance, with
    pragmas applied once on open. Callers in this single-process phase share
    the connection; the daemon (T-FND-06) will own a single instance.
    """

    def __init__(
        self,
        db_path: str | Path,
        *,
        migrations_dir: str | Path | None = None,
    ) -> None:
        self._db_path = str(db_path)
        self._migrations_dir = (
            Path(migrations_dir) if migrations_dir else _DEFAULT_MIGRATIONS_DIR
        )
        # ``isolation_level=None`` -> we manage transactions explicitly with
        # BEGIN IMMEDIATE for writes. ``check_same_thread=False`` so the
        # busy-retry test (and future daemon work) can drive the conn from
        # helper threads with explicit serialization.
        self._conn = connect_sqlite(
            self._db_path,
            isolation_level=None,
            check_same_thread=False,
            timeout=0.0,  # we implement our own backoff; don't double-wait
            row_factory=True,
        )
        self._apply_open_pragmas()

    # ------------------------------------------------------------------ open

    def _apply_open_pragmas(self) -> None:
        cur = self._conn.cursor()
        # WAL is persistent for on-disk DBs; harmless for :memory:.
        enable_wal_mode(self._conn, self._db_path)
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()

    def close(self) -> None:
        with contextlib.suppress(sqlite3.ProgrammingError):
            self._conn.close()

    # -------------------------------------------------------------- txn util

    @contextlib.contextmanager
    def _write_txn(self) -> Iterator[sqlite3.Cursor]:
        """Run a write transaction with BEGIN IMMEDIATE + busy-retry."""
        last_err: sqlite3.OperationalError | None = None
        for attempt in range(len(_BUSY_BACKOFFS_S) + 1):
            try:
                cur = self._conn.cursor()
                cur.execute("BEGIN IMMEDIATE")
                try:
                    yield cur
                except BaseException:
                    self._conn.rollback()
                    raise
                else:
                    self._conn.commit()
                    cur.close()
                    return
            except sqlite3.OperationalError as err:
                if not _is_locked_error(err):
                    raise
                last_err = err
                if attempt < len(_BUSY_BACKOFFS_S):
                    time.sleep(_BUSY_BACKOFFS_S[attempt])
                    continue
                raise sqlite3.OperationalError(
                    "database is locked after 3 retries"
                ) from last_err

    # ---------------------------------------------------------- migrations

    def _ensure_schema_migrations_table(self) -> None:
        """Bootstrap ``schema_migrations`` so the runner can read history.

        ``0001_init.sql`` also declares this table with ``IF NOT EXISTS`` so
        re-running on a fresh DB is idempotent.
        """
        with self._write_txn() as cur:
            cur.execute(
                "CREATE TABLE IF NOT EXISTS schema_migrations ("
                "version INTEGER PRIMARY KEY, "
                "applied_at TEXT NOT NULL, "
                "checksum TEXT NOT NULL"
                ")"
            )

    def _discover_migrations(self) -> list[tuple[int, Path, str, bytes]]:
        """Return ``[(version, path, checksum, body), ...]`` sorted by version."""
        out: list[tuple[int, Path, str, bytes]] = []
        if not self._migrations_dir.is_dir():
            return out
        for entry in sorted(self._migrations_dir.iterdir()):
            if not entry.is_file():
                continue
            m = _MIGRATION_FILENAME_RE.match(entry.name)
            if not m:
                continue
            version = int(m.group(1))
            body = entry.read_bytes()
            out.append((version, entry, _sha256_bytes(body), body))
        out.sort(key=lambda t: t[0])
        return out

    def applied_migrations(self) -> list[int]:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            )
            if cur.fetchone() is None:
                return []
            cur.execute("SELECT version FROM schema_migrations ORDER BY version")
            return [int(row[0]) for row in cur.fetchall()]
        finally:
            cur.close()

    def _recorded_checksums(self) -> dict[int, str]:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            )
            if cur.fetchone() is None:
                return {}
            cur.execute("SELECT version, checksum FROM schema_migrations")
            return {int(r[0]): str(r[1]) for r in cur.fetchall()}
        finally:
            cur.close()

    def apply_pending_migrations(self) -> list[int]:
        """Apply any migration with version > MAX(applied). Returns versions
        applied this call (empty list when up to date).

        Raises :class:`tm.store.MigrationIntegrityError` if a previously-applied
        migration file has been modified on disk.
        """
        # Local import to avoid a top-level cycle (tm.store imports SQLiteStore).
        from tm.store import MigrationIntegrityError

        self._ensure_schema_migrations_table()
        recorded = self._recorded_checksums()
        discovered = self._discover_migrations()

        # Drift check: every recorded version that is also on disk must match.
        on_disk = {v: ck for v, _p, ck, _b in discovered}
        for version, recorded_ck in recorded.items():
            if version in on_disk and on_disk[version] != recorded_ck:
                raise MigrationIntegrityError(
                    version=version,
                    recorded_checksum=recorded_ck,
                    file_checksum=on_disk[version],
                )

        applied_now: list[int] = []
        max_applied = max(recorded) if recorded else 0
        for version, path, checksum, body in discovered:
            if version <= max_applied:
                continue
            sql_text = body.decode("utf-8")
            pre_txn_stmts, post_txn_stmts = _parse_migration_header(sql_text)

            # Parse-time defence: every directive payload MUST start with
            # PRAGMA (case-insensitive). A non-PRAGMA payload (e.g.
            # ``-- !pre-txn: DROP TABLE schema_migrations;``) is rejected here,
            # BEFORE any BEGIN IMMEDIATE, so the body never runs and no
            # schema_migrations row is written.
            # This is parse-time validation; contrast with runtime
            # MigrationPragmaError / MigrationPostTxnError which fire when a
            # syntactically valid PRAGMA statement fails during execution.
            for _stmt in pre_txn_stmts + post_txn_stmts:
                if not _stmt.lstrip().upper().startswith("PRAGMA"):
                    raise MigrationPragmaError(
                        version=version,
                        pragma_sql=_stmt,
                        cause=ValueError(
                            f"directive payload must start with PRAGMA: {_stmt!r}"
                        ),
                    )

            applied_at = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")

            # 1. Pre-txn pragmas run on the same connection BEFORE BEGIN
            #    IMMEDIATE. Pragmas like ``foreign_keys`` are silent no-ops
            #    inside a transaction (per SQLite docs), so they MUST run
            #    here to actually take effect during body execution.
            for pragma_sql in pre_txn_stmts:
                try:
                    self._conn.execute(pragma_sql)
                except sqlite3.Error as err:
                    raise MigrationPragmaError(
                        version=version,
                        pragma_sql=pragma_sql,
                        cause=err,
                    ) from err

            # 2-4. Body inside BEGIN IMMEDIATE; on body failure the txn
            #      rolls back and post-txn pragmas DO NOT run (so a failure
            #      isn't masked by a "successful" pragma reset).
            try:
                with self._write_txn() as cur:
                    # executescript would auto-commit; we already own the txn, so
                    # split on semicolons via a simple splitter that respects
                    # single-quoted strings and ``--`` line comments. Sufficient
                    # for DDL-only migrations; if a future migration needs
                    # trigger bodies (BEGIN..END) the splitter will need
                    # extending then — out of scope here.
                    for stmt in _split_sql_statements(sql_text):
                        if stmt.strip():
                            cur.execute(stmt)
                    cur.execute(
                        "INSERT INTO schema_migrations(version, applied_at, checksum) "
                        "VALUES (?, ?, ?)",
                        (version, applied_at, checksum),
                    )
            except (sqlite3.IntegrityError, sqlite3.OperationalError) as exc:
                raise MigrationBodyError(version=version, cause=exc) from exc
            print(
                f"applied migration {version:04d} ({path.name})",
                file=sys.stderr,
            )
            applied_now.append(version)

            # 5. Post-txn pragmas: body is committed, schema_migrations row
            #    is written. If a post-txn pragma fails we still consider
            #    the migration "applied" (it WAS) but raise so the caller
            #    knows pragma side-effects did not complete.
            for pragma_sql in post_txn_stmts:
                try:
                    self._conn.execute(pragma_sql)
                except sqlite3.Error as err:
                    raise MigrationPostTxnError(
                        version=version,
                        pragma_sql=pragma_sql,
                        cause=err,
                    ) from err
        return applied_now

    # ----------------------------------------------------------- events: write

    def append_event(self, event: dict[str, Any]) -> None:
        """Insert one event row.

        Required keys: ``event_id, case_id, activity, timestamp, lifecycle,
        extractor_version``. Optional: ``resource``, ``attributes`` (dict, will
        be json-encoded into ``attributes_json``), ``created_at`` (else
        defaulted by SQL).
        """
        missing = [k for k in _REQUIRED_EVENT_KEYS if k not in event]
        if missing:
            raise ValueError(f"append_event: missing required keys: {missing}")

        attributes = event.get("attributes", {}) or {}
        if not isinstance(attributes, dict):
            raise TypeError("append_event: 'attributes' must be a dict if provided")
        attributes_json = json.dumps(attributes, sort_keys=True, default=str)

        params = (
            event["event_id"],
            event["case_id"],
            event["activity"],
            event["timestamp"],
            event["lifecycle"],
            event.get("resource"),
            attributes_json,
            event["extractor_version"],
        )
        with self._write_txn() as cur:
            cur.execute(
                "INSERT INTO events ("
                "event_id, case_id, activity, timestamp, lifecycle, "
                "resource, attributes_json, extractor_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                params,
            )

    # ------------------------------------------------------------ events: read

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        cur = self._conn.cursor()
        try:
            cur.execute(
                "SELECT event_id, case_id, activity, timestamp, lifecycle, "
                "resource, attributes_json, extractor_version, created_at "
                "FROM events WHERE event_id = ?",
                (event_id,),
            )
            row = cur.fetchone()
            return _row_to_event(row) if row is not None else None
        finally:
            cur.close()

    def query_events(
        self,
        *,
        case_id: str | None = None,
        since: str | None = None,
        until: str | None = None,
        activity: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
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

        sql = (
            "SELECT event_id, case_id, activity, timestamp, lifecycle, "
            "resource, attributes_json, extractor_version, created_at "
            "FROM events"
        )
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY timestamp ASC, event_id ASC"
        if limit is not None:
            sql += " LIMIT ?"
            params.append(int(limit))

        cur = self._conn.cursor()
        try:
            cur.execute(sql, params)
            return [_row_to_event(r) for r in cur.fetchall()]
        finally:
            cur.close()


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
    }
    raw = row["attributes_json"] or "{}"
    try:
        out["attributes"] = json.loads(raw)
    except json.JSONDecodeError:
        out["attributes"] = {}
    return out


def _parse_migration_header(sql_text: str) -> tuple[list[str], list[str]]:
    """Extract ``-- !pre-txn:`` and ``-- !post-txn:`` directives.

    Parses ONLY the leading comment block. Stops at the first line that is
    neither a comment nor empty; from that line onward is the migration body
    (which the body splitter sees unchanged — header parsing is a runtime
    concern only and does NOT alter the checksum input).

    Tolerates a leading UTF-8 BOM and arbitrary leading whitespace on each
    header line. Multiple pre-txn or post-txn directives are allowed and are
    returned in source order.

    Directive keywords MUST be lowercase: pre-txn / post-txn.  Uppercase
    variants (e.g. ``-- !PRE-TXN:``) are silently treated as plain comments
    and are NOT parsed as directives.

    Returns:
        ``(pre_txn_stmts, post_txn_stmts)`` — each pragma SQL statement with
        a single trailing ``;`` stripped (sqlite3.execute requires exactly
        one statement, no trailing semicolon-separated tail).
    """
    pre_txn: list[str] = []
    post_txn: list[str] = []
    if not sql_text:
        return pre_txn, post_txn
    # Strip BOM if present at the very start.
    if sql_text.startswith(_UTF8_BOM):
        sql_text = sql_text[len(_UTF8_BOM) :]
    for raw_line in sql_text.splitlines():
        if _HEADER_BLANK_RE.match(raw_line):
            # Empty/whitespace-only line: still in header region, keep scanning.
            continue
        if not _HEADER_COMMENT_RE.match(raw_line):
            # First non-comment, non-empty line: body starts here.
            break
        m = _HEADER_DIRECTIVE_RE.match(raw_line)
        if m is None:
            # Plain ``-- comment`` — skip silently.
            continue
        kind = m.group(1)
        stmt = m.group(2).strip()
        # Drop a single trailing ``;`` if present so sqlite3.execute is happy.
        if stmt.endswith(";"):
            stmt = stmt[:-1].rstrip()
        if not stmt:
            # ``-- !pre-txn:`` with empty payload — skip; nothing to run.
            continue
        if kind == "pre-txn":
            pre_txn.append(stmt)
        else:  # post-txn
            post_txn.append(stmt)
    return pre_txn, post_txn


def _split_sql_statements(sql_text: str) -> list[str]:
    """Naive statement splitter for 0001_init.sql DDL.

    Respects single-quoted strings and ``--`` line comments. Sufficient for
    DDL-only migrations in this task; if a future migration needs trigger
    bodies (BEGIN..END), upgrade this splitter then.
    """
    statements: list[str] = []
    buf: list[str] = []
    i = 0
    n = len(sql_text)
    in_squote = False
    while i < n:
        ch = sql_text[i]
        # line comment
        if not in_squote and ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            # skip to end of line
            while i < n and sql_text[i] != "\n":
                i += 1
            continue
        if ch == "'":
            # toggle, accounting for doubled '' escape
            if in_squote and i + 1 < n and sql_text[i + 1] == "'":
                buf.append("''")
                i += 2
                continue
            in_squote = not in_squote
            buf.append(ch)
            i += 1
            continue
        if ch == ";" and not in_squote:
            stmt = "".join(buf).strip()
            if stmt:
                statements.append(stmt)
            buf = []
            i += 1
            continue
        buf.append(ch)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        statements.append(tail)
    return statements
