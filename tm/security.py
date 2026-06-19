"""Optional SQLCipher/keyring support for SQLite connections.

The default path stays plain SQLite. If ``TM_SQLCIPHER_KEY`` is set, or keyring
lookup is explicitly enabled, connections apply ``PRAGMA key`` before any
schema access and fail fast when the local SQLite driver is not SQLCipher
enabled.
"""

from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

__all__ = [
    "ENV_SQLCIPHER_KEY",
    "ENV_SQLCIPHER_KEYRING",
    "EncryptionStatus",
    "EncryptionUnavailableError",
    "connect_sqlite",
    "enable_wal_mode",
    "encryption_status",
    "harden_sqlite_file_permissions",
    "key_source_for_path",
    "process_key_override",
    "store_key_in_keyring",
]

ENV_SQLCIPHER_KEY = "TM_SQLCIPHER_KEY"
ENV_SQLCIPHER_KEYRING = "TM_SQLCIPHER_KEYRING"
_KEYRING_SERVICE = "tm"
_PROCESS_KEY_OVERRIDES: dict[str, str] = {}
_PRIVATE_SQLITE_FILE_MODE = 0o600
_PRIVATE_SQLITE_UMASK_LOCK = threading.Lock()


class EncryptionUnavailableError(RuntimeError):
    """Raised when a key is configured but SQLCipher support is unavailable."""


@dataclass(frozen=True)
class EncryptionStatus:
    sqlcipher_available: bool
    key_source: str | None
    keyring_available: bool


def _db_key_name(db_path: str | Path) -> str:
    if str(db_path) == ":memory:":
        return ":memory:"
    return str(Path(db_path).expanduser().resolve(strict=False))


def _normalize_sqlite_path(db_path: str | Path) -> str | Path:
    if str(db_path) == ":memory:":
        return ":memory:"
    return Path(db_path).expanduser().resolve(strict=False)


def _import_keyring() -> Any | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except Exception:
        return None
    return keyring


def _keyring_enabled() -> bool:
    raw = os.environ.get(ENV_SQLCIPHER_KEYRING, "")
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _sql_literal(value: str) -> str:
    return "'" + value.replace("'", "''") + "'"


def _sqlite_paths(db_path: str | Path) -> tuple[Path, ...]:
    normalized = _normalize_sqlite_path(db_path)
    if normalized == ":memory:":
        return ()
    path = Path(normalized)
    return (
        path,
        Path(f"{path}-wal"),
        Path(f"{path}-shm"),
        Path(f"{path}-journal"),
    )


def _ensure_private_sqlite_file(db_path: str | Path) -> None:
    normalized = _normalize_sqlite_path(db_path)
    if normalized == ":memory:":
        return
    path = Path(normalized)
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        return
    else:
        os.close(fd)


@contextmanager
def _private_sqlite_umask() -> Iterator[None]:
    with _PRIVATE_SQLITE_UMASK_LOCK:
        old_umask = os.umask(0o077)
        try:
            yield
        finally:
            os.umask(old_umask)


def harden_sqlite_file_permissions(db_path: str | Path) -> None:
    """Make the SQLite database and any existing sidecars owner-only."""
    if str(db_path) == ":memory:":
        return
    for path in _sqlite_paths(db_path):
        try:
            if path.exists() and path.is_file() and not path.is_symlink():
                os.chmod(path, _PRIVATE_SQLITE_FILE_MODE)
        except OSError:
            continue


def enable_wal_mode(conn: sqlite3.Connection, db_path: str | Path) -> None:
    harden_sqlite_file_permissions(db_path)
    with _private_sqlite_umask():
        conn.execute("PRAGMA journal_mode=WAL")
    harden_sqlite_file_permissions(db_path)


def key_source_for_path(db_path: str | Path) -> tuple[str | None, str | None]:
    env_key = os.environ.get(ENV_SQLCIPHER_KEY)
    if env_key:
        return env_key, ENV_SQLCIPHER_KEY

    override = _PROCESS_KEY_OVERRIDES.get(_db_key_name(db_path))
    if override:
        return override, "process"

    if not _keyring_enabled():
        return None, None

    keyring = _import_keyring()
    if keyring is None:
        return None, ENV_SQLCIPHER_KEYRING
    key = keyring.get_password(_KEYRING_SERVICE, _db_key_name(db_path))
    if key:
        return str(key), "keyring"
    key = keyring.get_password(_KEYRING_SERVICE, "default")
    if key:
        return str(key), "keyring:default"
    return None, ENV_SQLCIPHER_KEYRING


@contextmanager
def process_key_override(db_path: str | Path, key: str | None) -> Iterator[None]:
    """Temporarily register a process-local key for a specific DB path."""
    if not key:
        yield
        return

    name = _db_key_name(db_path)
    had_old = name in _PROCESS_KEY_OVERRIDES
    old = _PROCESS_KEY_OVERRIDES.get(name)
    _PROCESS_KEY_OVERRIDES[name] = key
    try:
        yield
    finally:
        if had_old and old is not None:
            _PROCESS_KEY_OVERRIDES[name] = old
        else:
            _PROCESS_KEY_OVERRIDES.pop(name, None)


def _apply_key(conn: sqlite3.Connection, key: str) -> None:
    conn.execute(f"PRAGMA key = {_sql_literal(key)}")
    row = conn.execute("PRAGMA cipher_version").fetchone()
    if row is None or not row[0]:
        raise EncryptionUnavailableError(
            "TM_SQLCIPHER_KEY is set, but this Python sqlite3 driver does not "
            "report SQLCipher support via PRAGMA cipher_version"
        )
    # Force key validation before callers run migrations or queries.
    conn.execute("SELECT count(*) FROM sqlite_master").fetchone()


def connect_sqlite(
    db_path: str | Path,
    *,
    timeout: float = 5.0,
    isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "DEFERRED",
    check_same_thread: bool = True,
    row_factory: bool = False,
    sqlcipher_key: str | None = None,
) -> sqlite3.Connection:
    normalized_db_path = _normalize_sqlite_path(db_path)
    key = sqlcipher_key
    source: str | None = None
    if key is None:
        key, source = key_source_for_path(normalized_db_path)
    if key is None and source == ENV_SQLCIPHER_KEYRING:
        raise EncryptionUnavailableError(
            f"{ENV_SQLCIPHER_KEYRING} is enabled, but no SQLCipher key was "
            f"found for {normalized_db_path}"
        )
    _ensure_private_sqlite_file(normalized_db_path)
    with _private_sqlite_umask():
        conn = sqlite3.connect(
            str(normalized_db_path),
            timeout=timeout,
            isolation_level=isolation_level,
            check_same_thread=check_same_thread,
        )
    if row_factory:
        conn.row_factory = sqlite3.Row
    if key:
        try:
            _apply_key(conn, key)
        except Exception:
            conn.close()
            raise
    harden_sqlite_file_permissions(normalized_db_path)
    return conn


def encryption_status(db_path: str | Path) -> EncryptionStatus:
    keyring = _import_keyring()
    key, source = key_source_for_path(db_path)
    available = False
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(":memory:")
        row = conn.execute("PRAGMA cipher_version").fetchone()
        available = bool(row and row[0])
    finally:
        if conn is not None:
            conn.close()
    return EncryptionStatus(
        sqlcipher_available=available,
        key_source=source if key else None,
        keyring_available=keyring is not None,
    )


def store_key_in_keyring(db_path: str | Path, key: str) -> None:
    keyring = _import_keyring()
    if keyring is None:
        raise EncryptionUnavailableError(
            "keyring is not installed; install keyring or use TM_SQLCIPHER_KEY"
        )
    keyring.set_password(_KEYRING_SERVICE, _db_key_name(db_path), key)
