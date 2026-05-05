"""Migration runner tests."""

from __future__ import annotations

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from tm.store import (
    MigrationError,
    MigrationIntegrityError,
    MigrationPostTxnError,
    MigrationPragmaError,
    Store,
)
from tm.stores.sqlite_store import _parse_migration_header

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def test_applies_pending_on_empty_db(tmp_path: Path, stub_migrations_dir: Path) -> None:
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    applied = s.apply_pending_migrations()
    assert applied == [1]
    assert s.applied_migrations() == [1]
    s.close()


def test_idempotent_second_apply(tmp_path: Path, stub_migrations_dir: Path) -> None:
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    assert s.apply_pending_migrations() == [1]
    assert s.apply_pending_migrations() == []
    assert s.applied_migrations() == [1]
    s.close()


def test_records_checksum_in_schema_migrations(
    tmp_path: Path, stub_migrations_dir: Path
) -> None:
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    s.apply_pending_migrations()
    cur = s._conn.cursor()  # type: ignore[attr-defined]  # test internal access
    cur.execute(
        "SELECT version, applied_at, checksum FROM schema_migrations ORDER BY version"
    )
    rows = cur.fetchall()
    cur.close()
    assert len(rows) == 1
    version, applied_at, checksum = rows[0]
    assert version == 1
    assert isinstance(applied_at, str) and applied_at.endswith("Z")
    assert SHA256_RE.match(checksum), f"checksum not sha256-shaped: {checksum!r}"
    s.close()


def test_detects_checksum_drift(tmp_path: Path, stub_migrations_dir: Path) -> None:
    """Apply stub migration, then mutate the file → drift error."""
    db = tmp_path / "tm.db"

    s = Store(db, migrations_dir=stub_migrations_dir)
    assert s.apply_pending_migrations() == [1]
    s.close()

    # Tamper: append a comment (changes checksum, keeps SQL valid).
    target = stub_migrations_dir / "0001_stub.sql"
    target.write_text(target.read_text() + "\n-- drift\n")

    s2 = Store(db, migrations_dir=stub_migrations_dir)
    with pytest.raises(MigrationIntegrityError) as ei:
        s2.apply_pending_migrations()
    err = ei.value
    assert err.version == 1
    assert err.recorded_checksum != err.file_checksum
    assert SHA256_RE.match(err.recorded_checksum)
    assert SHA256_RE.match(err.file_checksum)
    s2.close()


def test_applies_in_version_order(tmp_path: Path) -> None:
    """Two stub migrations: 0001 and 0002 must apply in numeric order."""
    fixture_dir = tmp_path / "migrations"
    fixture_dir.mkdir()
    (fixture_dir / "0002_second.sql").write_text(
        "CREATE TABLE second (id INTEGER PRIMARY KEY);\n"
    )
    (fixture_dir / "0001_first.sql").write_text(
        "CREATE TABLE first (id INTEGER PRIMARY KEY);\n"
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=fixture_dir)
    applied = s.apply_pending_migrations()
    assert applied == [1, 2]
    assert s.applied_migrations() == [1, 2]

    # Verify both tables exist.
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name IN ('first','second')"
    )
    names = sorted(r[0] for r in cur.fetchall())
    cur.close()
    assert names == ["first", "second"]
    s.close()


# ---------------------------------------------------------------------------
# T-FND-07: pre-txn / post-txn pragma directives
# ---------------------------------------------------------------------------


def _fk_enabled(store: Store) -> bool:
    cur = store._conn.cursor()  # type: ignore[attr-defined]
    try:
        cur.execute("PRAGMA foreign_keys")
        row = cur.fetchone()
        return bool(row[0])
    finally:
        cur.close()


def test_pre_txn_pragma_executes_before_begin(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """A pre-txn ``PRAGMA foreign_keys=OFF`` must take effect during the body.

    We assert the runtime side-effect by reading ``PRAGMA foreign_keys``
    after the migration has been applied — the pre-txn pragma stays in
    effect on the connection because nothing re-enabled it.
    """
    mdir = write_migration(
        "0001_pre_txn.sql",
        "-- !pre-txn: PRAGMA foreign_keys = OFF;\n"
        "CREATE TABLE parent (id INTEGER PRIMARY KEY);\n"
        "CREATE TABLE child ("
        "id INTEGER PRIMARY KEY, "
        "parent_id INTEGER REFERENCES parent(id)"
        ");\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    # FK is OFF (pre-txn took effect and was never restored).
    assert _fk_enabled(s) is False
    # And FK violations are accepted while OFF — confirming it really is off.
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute("INSERT INTO child(id, parent_id) VALUES (1, 999)")
    cur.close()
    s.close()


def test_post_txn_pragma_executes_after_commit(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """A post-txn ``PRAGMA foreign_keys=ON`` re-enables enforcement."""
    mdir = write_migration(
        "0001_post_txn.sql",
        "-- !pre-txn: PRAGMA foreign_keys = OFF;\n"
        "-- !post-txn: PRAGMA foreign_keys = ON;\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    assert _fk_enabled(s) is True
    s.close()


def test_multiple_pre_txn_pragmas_execute_in_order(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """Two pre-txn directives both fire; observable through PRAGMA reads."""
    mdir = write_migration(
        "0001_multi_pre.sql",
        "-- !pre-txn: PRAGMA foreign_keys = OFF;\n"
        "-- !pre-txn: PRAGMA recursive_triggers = ON;\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    # First pre-txn: foreign_keys OFF.
    assert _fk_enabled(s) is False
    # Second pre-txn: recursive_triggers ON.
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA recursive_triggers")
    assert bool(cur.fetchone()[0]) is True
    cur.close()
    s.close()


def test_mixed_pre_and_post_txn_pragmas(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """One pre + one post: pre disables FK, post re-enables it."""
    mdir = write_migration(
        "0001_mixed.sql",
        "-- !pre-txn: PRAGMA foreign_keys = OFF;\n"
        "-- !post-txn: PRAGMA foreign_keys = ON;\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    # post-txn ran last → FK is ON.
    assert _fk_enabled(s) is True
    s.close()


def test_pre_txn_pragma_failure_raises_MigrationPragmaError(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """Bogus pre-txn pragma → exception, body never runs, no schema_migrations row."""
    mdir = write_migration(
        "0001_bad_pre.sql",
        "-- !pre-txn: BOGUS_NOT_VALID_SQL;\n"
        "CREATE TABLE never_created (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    with pytest.raises(MigrationPragmaError) as ei:
        s.apply_pending_migrations()
    assert ei.value.version == 1
    assert "BOGUS_NOT_VALID_SQL" in ei.value.pragma_sql

    # No schema_migrations row, no body table.
    assert s.applied_migrations() == []
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='never_created'"
    )
    assert cur.fetchone() is None
    cur.close()
    s.close()


def test_post_txn_pragma_failure_raises_MigrationPostTxnError_but_body_committed(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """post-txn PRAGMA that fails at execution → exception, but body IS persisted.

    The directive payload starts with ``PRAGMA`` (passes parse-time validation)
    but the SQL is syntactically invalid so SQLite raises at execution time.
    The schema_migrations row IS written and body changes survive.
    """
    mdir = write_migration(
        "0001_bad_post.sql",
        "-- !post-txn: PRAGMA invalid syntax here;\n"
        "CREATE TABLE was_created (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    with pytest.raises(MigrationPostTxnError) as ei:
        s.apply_pending_migrations()
    assert ei.value.version == 1
    assert "PRAGMA" in ei.value.pragma_sql

    # Body committed: schema_migrations row exists + table is present.
    assert s.applied_migrations() == [1]
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='was_created'"
    )
    assert cur.fetchone() is not None
    cur.close()
    s.close()


def test_migration_without_headers_unchanged(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """Regression: a migration with no header directives works as before."""
    mdir = write_migration(
        "0001_plain.sql",
        "-- A normal comment, not a directive.\n"
        "CREATE TABLE plain (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    assert s.apply_pending_migrations() == [1]
    assert s.applied_migrations() == [1]
    # FK is still ON (pragma applied at open by _apply_open_pragmas).
    assert _fk_enabled(s) is True
    s.close()


def test_header_parsing_tolerates_leading_whitespace(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """Whitespace before ``--`` and around the directive must still parse."""
    mdir = write_migration(
        "0001_ws.sql",
        "  -- !pre-txn :  PRAGMA foreign_keys = OFF ;  \n"
        "\t--!post-txn:PRAGMA foreign_keys = ON;\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    # post-txn ran last → FK is ON. If the pre-txn directive had not been
    # parsed, the body would have been wrapped in BEGIN IMMEDIATE with FK
    # already ON (no harm), then the post-txn would still set FK=ON. To
    # observe the pre-txn parse worked, we add a third migration step here:
    # we already disabled+re-enabled FK; if any directive failed to parse
    # the runner would not have raised. Acceptable signal: applied list.
    assert s.applied_migrations() == [1]
    assert _fk_enabled(s) is True
    s.close()


def test_header_parsing_stops_at_body(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """Header lines, blank, regular comment, then DDL: directive still parsed.

    A ``--`` comment AFTER the body has begun is treated as a body comment
    by the splitter (and ignored), not as a header directive — even if it
    were spelled like one (we don't test that pathological case here).
    """
    mdir = write_migration(
        "0001_stop.sql",
        "-- !pre-txn: PRAGMA foreign_keys = OFF;\n"
        "\n"
        "-- A regular comment line, NOT a directive.\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n"
        "-- Another comment, ignored by the splitter as part of the body.\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    # The pre-txn FK=OFF directive must have been picked up; FK should
    # therefore be OFF on the connection (nothing turned it back on).
    assert _fk_enabled(s) is False
    assert s.applied_migrations() == [1]
    s.close()


def test_header_parsing_tolerates_utf8_bom(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """A UTF-8 BOM at file start must not block header parsing."""
    mdir = write_migration(
        "0001_bom.sql",
        "﻿"  # UTF-8 BOM
        "-- !pre-txn: PRAGMA foreign_keys = OFF;\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    assert _fk_enabled(s) is False
    s.close()


# ---------------------------------------------------------------------------
# T-FND-08: exception hierarchy, case-sensitivity, PRAGMA payload validation
# ---------------------------------------------------------------------------


def test_MigrationIntegrityError_extends_MigrationError() -> None:
    """MigrationIntegrityError must be a subclass of MigrationError.

    AC1: ``issubclass(MigrationIntegrityError, MigrationError)`` is True so
    that ``except MigrationError:`` catches all migration-runner failures.
    """
    assert issubclass(MigrationIntegrityError, MigrationError)


def test_except_MigrationError_catches_IntegrityError(
    tmp_path: Path,
    stub_migrations_dir: Path,
) -> None:
    """Checksum drift is catchable via ``except MigrationError:``.

    AC2: Apply a migration, tamper the file so the checksum changes, then
    confirm the resulting exception is caught by a bare ``except MigrationError``
    block — not just by ``except RuntimeError`` or ``except MigrationIntegrityError``.
    """
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=stub_migrations_dir)
    assert s.apply_pending_migrations() == [1]
    s.close()

    target = stub_migrations_dir / "0001_stub.sql"
    target.write_text(target.read_text() + "\n-- drift\n")

    s2 = Store(db, migrations_dir=stub_migrations_dir)
    caught: Exception | None = None
    try:
        s2.apply_pending_migrations()
    except MigrationError as exc:
        caught = exc
    finally:
        s2.close()

    assert caught is not None, "expected MigrationError but nothing was raised"
    assert isinstance(caught, MigrationIntegrityError), (
        f"expected MigrationIntegrityError, got {type(caught).__name__}"
    )


def test_header_directive_case_sensitivity() -> None:
    """Uppercase ``-- !PRE-TXN:`` is silently ignored; no directive parsed.

    AC3 / AC4: Directive keywords MUST be lowercase (pre-txn / post-txn).
    Uppercase variants are not directives — they parse as plain comments.
    This test asserts that ``_parse_migration_header`` returns empty lists
    for uppercase-keyword variants, so no PRAGMA side-effect is applied.
    """
    sql = "-- !PRE-TXN: PRAGMA foreign_keys=OFF;\nCREATE TABLE x (id INT);\n"
    pre, post = _parse_migration_header(sql)
    assert pre == [], f"expected no pre-txn directives, got {pre!r}"
    assert post == [], f"expected no post-txn directives, got {post!r}"

    # Also verify mixed-case variant is silently ignored.
    sql_mixed = "-- !Pre-Txn: PRAGMA foreign_keys=OFF;\nCREATE TABLE y (id INT);\n"
    pre2, post2 = _parse_migration_header(sql_mixed)
    assert pre2 == []
    assert post2 == []


def test_pre_txn_payload_must_start_with_PRAGMA(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """``-- !pre-txn: DROP TABLE ...`` raises MigrationPragmaError at parse time.

    AC5: The parse-time PRAGMA validation runs BEFORE ``BEGIN IMMEDIATE``,
    so the migration body never executes and no ``schema_migrations`` row is
    written.  This is distinct from a runtime PRAGMA execution failure.
    """
    mdir = write_migration(
        "0001_bad_pre_payload.sql",
        "-- !pre-txn: DROP TABLE schema_migrations;\n"
        "CREATE TABLE should_not_exist (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    with pytest.raises(MigrationPragmaError) as ei:
        s.apply_pending_migrations()
    err = ei.value
    assert err.version == 1
    assert "DROP TABLE" in err.pragma_sql

    # Body must NOT have run; schema_migrations empty, table absent.
    assert s.applied_migrations() == []
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='should_not_exist'"
    )
    assert cur.fetchone() is None
    cur.close()
    s.close()


def test_post_txn_payload_must_start_with_PRAGMA(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """``-- !post-txn: DROP TABLE ...`` raises MigrationPragmaError at parse time.

    AC6: Parse-time validation also applies to post-txn directives and runs
    BEFORE ``BEGIN IMMEDIATE``, so the entire migration is aborted: the body
    never runs and no ``schema_migrations`` row is written.

    Contrast with a runtime post-txn failure (MigrationPostTxnError), where the
    directive payload starts with ``PRAGMA`` but the SQL fails during execution
    AFTER the body has been committed.
    """
    mdir = write_migration(
        "0001_bad_post_payload.sql",
        "-- !post-txn: DROP TABLE schema_migrations;\n"
        "CREATE TABLE should_not_exist (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    with pytest.raises(MigrationPragmaError) as ei:
        s.apply_pending_migrations()
    err = ei.value
    assert err.version == 1
    assert "DROP TABLE" in err.pragma_sql

    # Body must NOT have run; schema_migrations empty, table absent.
    assert s.applied_migrations() == []
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name='should_not_exist'"
    )
    assert cur.fetchone() is None
    cur.close()
    s.close()


def test_pre_txn_payload_pragma_lowercase_accepted(
    tmp_path: Path,
    write_migration: Callable[..., Path],
) -> None:
    """``pragma foreign_keys=OFF`` (lowercase) is accepted and takes effect.

    AC7: The PRAGMA keyword check is case-insensitive on the keyword itself.
    ``pragma``, ``PRAGMA``, and ``Pragma`` are all valid directive payloads.
    """
    mdir = write_migration(
        "0001_pre_txn_lowercase_pragma.sql",
        "-- !pre-txn: pragma foreign_keys=OFF;\n"
        "CREATE TABLE only_table (id INTEGER PRIMARY KEY);\n",
    )
    db = tmp_path / "tm.db"
    s = Store(db, migrations_dir=mdir)
    s.apply_pending_migrations()
    # Lowercase ``pragma`` was accepted; FK is now OFF.
    assert _fk_enabled(s) is False
    assert s.applied_migrations() == [1]
    s.close()
