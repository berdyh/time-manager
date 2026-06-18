"""Paths and locations for tm CLI.

Default database lives at ``~/.local/share/tm/tm.db`` (XDG_DATA_HOME).
Override at runtime with the ``TM_DB`` environment variable or the
``--db-path`` option on any command that accepts it.
"""

from __future__ import annotations

import os
from pathlib import Path

_APP_NAME = "tm"
KUZU_PROJECTION_MARKER = ".tm-kuzu-projection"


def default_data_dir() -> Path:
    """Return (and create) the application data directory.

    Respects ``XDG_DATA_HOME`` when set; otherwise falls back to
    ``~/.local/share/tm``.  The directory is created with ``parents=True``
    and ``exist_ok=True`` so it is always usable on return.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / _APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    p.chmod(0o700)
    return p


def default_db_path() -> Path:
    """Return the canonical path to the tm SQLite database file.

    Equivalent to ``default_data_dir() / "tm.db"``.  The parent directory
    is created as a side-effect.
    """
    return default_data_dir() / "tm.db"


def default_kuzu_path() -> Path:
    """Return the default local Kuzu projection directory."""
    return default_data_dir() / "kuzu"


def kuzu_projection_marker_path(kuzu_db_path: Path | str) -> Path:
    """Return the ownership marker path for a Kuzu projection path."""
    path = Path(kuzu_db_path)
    if path.exists() and path.is_dir():
        return path / KUZU_PROJECTION_MARKER
    return path.with_name(f"{path.name}{KUZU_PROJECTION_MARKER}")


def default_socket_path() -> Path:
    """Return the canonical path to the daemon's Unix domain socket.

    Equivalent to ``default_data_dir() / "tm.sock"``.  The parent directory
    is created as a side-effect.
    """
    return default_data_dir() / "tm.sock"


def default_pid_path() -> Path:
    """Return the canonical path to the daemon's PID file.

    Equivalent to ``default_data_dir() / "tm.pid"``.  The parent directory
    is created as a side-effect.
    """
    return default_data_dir() / "tm.pid"
