"""CLI tests for encryption operator commands."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

from tm.cli import app

runner = CliRunner()


def test_encryption_set_key_rejects_command_line_key() -> None:
    result = runner.invoke(app, ["encryption", "set-key", "--key", "secret"])

    assert result.exit_code == 2
    assert "No such option" in result.output


def test_encryption_set_key_refuses_existing_database(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = tmp_path / "tm.db"
    db.write_text("plain sqlite", encoding="utf-8")

    from tm.commands import encryption as encryption_cmd

    monkeypatch.setattr(
        encryption_cmd,
        "encryption_status",
        lambda _db: SimpleNamespace(sqlcipher_available=True),
    )

    result = runner.invoke(
        app,
        ["encryption", "set-key", "--db-path", str(db)],
    )

    assert result.exit_code == 1
    assert "existing database" in result.output


def test_encryption_set_key_expands_path_before_existing_database_check(
    tmp_path: Path,
    monkeypatch,
) -> None:
    home = tmp_path / "home"
    home.mkdir()
    db = home / "tm.db"
    db.write_text("plain sqlite", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))

    from tm.commands import encryption as encryption_cmd

    monkeypatch.setattr(
        encryption_cmd,
        "encryption_status",
        lambda _db: SimpleNamespace(sqlcipher_available=True),
    )

    def _unexpected_store(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("store_key_in_keyring should not be called")

    monkeypatch.setattr(encryption_cmd, "store_key_in_keyring", _unexpected_store)

    result = runner.invoke(
        app,
        ["encryption", "set-key", "--db-path", "~/tm.db"],
    )

    assert result.exit_code == 1
    assert "existing database" in result.output


def test_encryption_set_key_does_not_create_db_when_keyring_store_fails(
    tmp_path: Path,
    monkeypatch,
) -> None:
    db = tmp_path / "tm.db"

    from tm.commands import encryption as encryption_cmd

    monkeypatch.setattr(
        encryption_cmd,
        "encryption_status",
        lambda _db: SimpleNamespace(sqlcipher_available=True),
    )
    monkeypatch.setattr(
        encryption_cmd,
        "store_key_in_keyring",
        lambda _db, _key: (_ for _ in ()).throw(RuntimeError("keyring locked")),
    )

    def _unexpected_connect(*_args, **_kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("connect_sqlite should not be called")

    monkeypatch.setattr(encryption_cmd, "connect_sqlite", _unexpected_connect)

    result = runner.invoke(
        app,
        ["encryption", "set-key", "--db-path", str(db)],
        input="secret\nsecret\n",
    )

    assert result.exit_code == 1
    assert "keyring locked" in result.output
    assert not db.exists()
