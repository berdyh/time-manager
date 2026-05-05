"""Paths and locations for tm CLI.

Default database lives at ``~/.local/share/tm/tm.db`` (XDG_DATA_HOME).
Override at runtime with the ``TM_DB`` environment variable or the
``--db-path`` option on any command that accepts it.
"""

from __future__ import annotations

import os
from pathlib import Path

_APP_NAME = "tm"


def default_data_dir() -> Path:
    """Return (and create) the application data directory.

    Respects ``XDG_DATA_HOME`` when set; otherwise falls back to
    ``~/.local/share/tm``.  The directory is created with ``parents=True``
    and ``exist_ok=True`` so it is always usable on return.
    """
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    p = Path(base) / _APP_NAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def default_db_path() -> Path:
    """Return the canonical path to the tm SQLite database file.

    Equivalent to ``default_data_dir() / "tm.db"``.  The parent directory
    is created as a side-effect.
    """
    return default_data_dir() / "tm.db"
