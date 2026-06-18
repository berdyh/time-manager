"""Security helper tests."""

from __future__ import annotations

import os
import sqlite3
import stat
from pathlib import Path

import pytest

import tm.security as security
from tm.security import (
    ENV_SQLCIPHER_KEY,
    ENV_SQLCIPHER_KEYRING,
    EncryptionUnavailableError,
    connect_sqlite,
    enable_wal_mode,
    harden_sqlite_file_permissions,
    key_source_for_path,
    process_key_override,
)


def test_process_key_override_is_scoped(tmp_path: Path, monkeypatch) -> None:
    db = tmp_path / "staging.db"
    monkeypatch.delenv(ENV_SQLCIPHER_KEY, raising=False)
    monkeypatch.delenv(ENV_SQLCIPHER_KEYRING, raising=False)

    assert key_source_for_path(db) == (None, None)
    with process_key_override(db, "secret"):
        assert key_source_for_path(db) == ("secret", "process")
    assert key_source_for_path(db) == (None, None)


def test_keyring_lookup_canonicalizes_relative_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    class FakeKeyring:
        def __init__(self) -> None:
            self.passwords: dict[tuple[str, str], str] = {}

        def get_password(self, service: str, username: str) -> str | None:
            return self.passwords.get((service, username))

    fake = FakeKeyring()
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv(ENV_SQLCIPHER_KEY, raising=False)
    monkeypatch.setenv(ENV_SQLCIPHER_KEYRING, "1")
    monkeypatch.setattr(security, "_import_keyring", lambda: fake)

    canonical = str((tmp_path / "rel" / "tm.db").resolve(strict=False))
    fake.passwords[("tm", canonical)] = "secret"

    assert key_source_for_path(Path("rel/tm.db")) == ("secret", "keyring")
    assert key_source_for_path(tmp_path / "rel" / "tm.db") == ("secret", "keyring")


def test_keyring_enabled_without_key_fails_closed(tmp_path: Path, monkeypatch) -> None:
    class EmptyKeyring:
        def get_password(self, service: str, username: str) -> None:
            return None

    monkeypatch.delenv(ENV_SQLCIPHER_KEY, raising=False)
    monkeypatch.setenv(ENV_SQLCIPHER_KEYRING, "1")
    monkeypatch.setattr(security, "_import_keyring", lambda: EmptyKeyring())

    with pytest.raises(EncryptionUnavailableError):
        connect_sqlite(tmp_path / "tm.db")
    assert not (tmp_path / "tm.db").exists()


def test_connect_sqlite_creates_private_database_file(tmp_path: Path) -> None:
    db = tmp_path / "tm.db"
    old_umask = os.umask(0o022)
    try:
        conn = connect_sqlite(db)
        conn.execute("CREATE TABLE t(x INTEGER)")
        conn.commit()
        conn.close()
    finally:
        os.umask(old_umask)

    assert stat.S_IMODE(db.stat().st_mode) == 0o600


def test_private_sqlite_umask_uses_global_lock(monkeypatch) -> None:
    events: list[str] = []

    class FakeLock:
        def __enter__(self) -> None:
            events.append("enter")

        def __exit__(self, *args: object) -> None:
            events.append("exit")

    monkeypatch.setattr(security, "_PRIVATE_SQLITE_UMASK_LOCK", FakeLock())

    with security._private_sqlite_umask():
        events.append("body")

    assert events == ["enter", "body", "exit"]


def test_connect_sqlite_uses_expanded_path_for_opening(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    cwd = tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    (cwd / "~").mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(cwd)

    conn = connect_sqlite("~/tm.db")
    conn.execute("CREATE TABLE t(x INTEGER)")
    conn.commit()
    conn.close()

    assert (home / "tm.db").exists()
    assert not (cwd / "~" / "tm.db").exists()


def test_harden_sqlite_file_permissions_covers_existing_sidecars(
    tmp_path: Path,
) -> None:
    db = tmp_path / "tm.db"
    db.write_text("", encoding="utf-8")
    sidecars = [Path(f"{db}{suffix}") for suffix in ("-wal", "-shm", "-journal")]
    for path in [db, *sidecars]:
        path.write_text("", encoding="utf-8")
        path.chmod(0o644)

    harden_sqlite_file_permissions(db)

    for path in [db, *sidecars]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_enable_wal_mode_creates_private_sidecars(tmp_path: Path) -> None:
    db = tmp_path / "tm.db"
    with sqlite3.connect(db) as raw:
        raw.execute("CREATE TABLE t(x INTEGER)")
    db.chmod(0o644)
    old_umask = os.umask(0o022)
    try:
        conn = connect_sqlite(db)
        enable_wal_mode(conn, db)
        conn.execute("INSERT INTO t(x) VALUES (1)")
        conn.commit()
        sidecars = [Path(f"{db}{suffix}") for suffix in ("-wal", "-shm")]
        assert all(path.exists() for path in sidecars)
        for path in [db, *sidecars]:
            assert stat.S_IMODE(path.stat().st_mode) == 0o600
        conn.close()
    finally:
        os.umask(old_umask)
