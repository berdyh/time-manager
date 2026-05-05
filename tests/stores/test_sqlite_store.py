"""SQLite store tests: pragmas, CRUD, constraints, busy-retry, type guard."""

from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

import pytest

from tm.store import Store, StoreReader

REPO_ROOT = Path(__file__).resolve().parents[2]
REAL_MIGRATIONS = REPO_ROOT / "migrations"


def _make_store(db_path: Path) -> Store:
    s = Store(db_path, migrations_dir=REAL_MIGRATIONS)
    s.apply_pending_migrations()
    return s


def _event(
    *,
    event_id: str,
    case_id: str = "2026-05-05",
    activity: str = "code_review",
    timestamp: str = "2026-05-05T10:00:00Z",
    lifecycle: str = "complete",
    resource: str | None = "user",
    attributes: dict | None = None,
    extractor_version: str = "v0",
) -> dict:
    out = {
        "event_id": event_id,
        "case_id": case_id,
        "activity": activity,
        "timestamp": timestamp,
        "lifecycle": lifecycle,
        "resource": resource,
        "extractor_version": extractor_version,
    }
    if attributes is not None:
        out["attributes"] = attributes
    return out


def test_wal_mode_enabled(tmp_path: Path) -> None:
    s = _make_store(tmp_path / "tm.db")
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA journal_mode")
    mode = cur.fetchone()[0]
    cur.close()
    s.close()
    assert mode.lower() == "wal"


def test_foreign_keys_enabled(tmp_path: Path) -> None:
    s = _make_store(tmp_path / "tm.db")
    cur = s._conn.cursor()  # type: ignore[attr-defined]
    cur.execute("PRAGMA foreign_keys")
    val = cur.fetchone()[0]
    cur.close()
    s.close()
    assert val == 1


def test_append_and_get_event(tmp_path: Path) -> None:
    s = _make_store(tmp_path / "tm.db")
    s.append_event(
        _event(event_id="e1", attributes={"duration_min": 25, "tag": "pr-review"})
    )
    got = s.get_event("e1")
    assert got is not None
    assert got["event_id"] == "e1"
    assert got["activity"] == "code_review"
    assert got["lifecycle"] == "complete"
    assert got["attributes"] == {"duration_min": 25, "tag": "pr-review"}
    assert got["extractor_version"] == "v0"
    assert got["created_at"]  # populated by SQL default
    assert s.get_event("does-not-exist") is None
    s.close()


def test_query_by_case_id(tmp_path: Path) -> None:
    s = _make_store(tmp_path / "tm.db")
    s.append_event(_event(event_id="a", case_id="C1", timestamp="2026-05-05T08:00:00Z"))
    s.append_event(_event(event_id="b", case_id="C1", timestamp="2026-05-05T09:00:00Z"))
    s.append_event(_event(event_id="c", case_id="C2", timestamp="2026-05-05T10:00:00Z"))
    rows = s.query_events(case_id="C1")
    assert [r["event_id"] for r in rows] == ["a", "b"]
    other = s.query_events(case_id="C2")
    assert [r["event_id"] for r in other] == ["c"]
    s.close()


def test_query_with_time_window(tmp_path: Path) -> None:
    s = _make_store(tmp_path / "tm.db")
    s.append_event(_event(event_id="a", timestamp="2026-05-05T08:00:00Z"))
    s.append_event(_event(event_id="b", timestamp="2026-05-05T10:00:00Z"))
    s.append_event(_event(event_id="c", timestamp="2026-05-05T12:00:00Z"))
    rows = s.query_events(
        since="2026-05-05T09:00:00Z",
        until="2026-05-05T12:00:00Z",
    )
    assert [r["event_id"] for r in rows] == ["b"]
    rows2 = s.query_events(since="2026-05-05T10:00:00Z")
    assert [r["event_id"] for r in rows2] == ["b", "c"]
    s.close()


def test_lifecycle_check_constraint_rejects_bad_value(tmp_path: Path) -> None:
    s = _make_store(tmp_path / "tm.db")
    bad = _event(event_id="x", lifecycle="bogus")
    with pytest.raises(sqlite3.IntegrityError):
        s.append_event(bad)
    s.close()


def test_busy_retry_on_locked_db(tmp_path: Path) -> None:
    """Hold a write txn from a second connection, then release; the Store
    must retry and succeed.

    Uses threading.Event for deterministic synchronization rather than sleeps.
    """
    db = tmp_path / "tm.db"
    s = _make_store(db)

    holder_started = threading.Event()
    release_holder = threading.Event()
    holder_done = threading.Event()

    def hold_write_lock() -> None:
        conn = sqlite3.connect(str(db), isolation_level=None, timeout=0.0)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("BEGIN IMMEDIATE")
            # Make a real write so the WAL lock is actually held.
            conn.execute(
                "INSERT INTO events ("
                "event_id, case_id, activity, timestamp, lifecycle, "
                "resource, attributes_json, extractor_version"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    "holder",
                    "C0",
                    "lock_holder",
                    "2026-05-05T07:00:00Z",
                    "complete",
                    None,
                    "{}",
                    "v0",
                ),
            )
            holder_started.set()
            release_holder.wait(timeout=2.0)
            conn.commit()
        finally:
            conn.close()
            holder_done.set()

    t = threading.Thread(target=hold_write_lock)
    t.start()
    assert holder_started.wait(timeout=2.0)

    # Schedule lock release between retry #2 (50ms) and retry #3 (200ms): wait
    # ~80ms after the Store starts trying. We launch a tiny releaser thread so
    # the main thread can stay inside append_event.
    def delayed_release() -> None:
        time.sleep(0.080)
        release_holder.set()

    releaser = threading.Thread(target=delayed_release)
    releaser.start()

    # This call must retry on locked-db and ultimately succeed once the holder
    # commits and releases its write lock.
    s.append_event(_event(event_id="retried", case_id="C0"))

    releaser.join(timeout=2.0)
    t.join(timeout=2.0)
    assert holder_done.is_set()

    got = s.get_event("retried")
    assert got is not None and got["event_id"] == "retried"
    s.close()


# ---------------------------------------------------------- AC10: type guard


def test_storereader_protocol_lacks_writes(tmp_path: Path) -> None:
    """A ``StoreReader``-typed view of a Store does not expose write methods.

    We use ``hasattr`` against the Protocol class itself (write methods must
    NOT be declared on it) plus ``isinstance`` to confirm a Store is a valid
    StoreReader at runtime. This is the simpler of the two AC10 options and
    keeps the test runner self-contained.
    """
    s = _make_store(tmp_path / "tm.db")

    # Runtime check: Store satisfies the read protocol.
    assert isinstance(s, StoreReader)

    # The Protocol itself MUST NOT declare write methods. If it did, mypy
    # would happily allow writes through a StoreReader-typed reference.
    assert not hasattr(StoreReader, "append_event")
    assert not hasattr(StoreReader, "apply_pending_migrations")

    # Read methods are on the Protocol.
    assert hasattr(StoreReader, "get_event")
    assert hasattr(StoreReader, "query_events")
    assert hasattr(StoreReader, "applied_migrations")

    s.close()
