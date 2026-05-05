"""Resilience helpers: small, focused utilities used by the daemon.

v1 scope is intentionally narrow:

* :func:`disk_space_pre_check` — verify enough free disk before opening the
  database, so we fail fast with a clear error instead of corrupting the WAL on
  a full filesystem.
* :class:`KeyringFallback` — placeholder shim for a future SQLCipher /
  OS-keyring integration.  The v1 stub returns ``None`` and prints a one-time
  warning so callers know the real keyring isn't wired yet.
* :class:`ResilienceError` — common exception raised by helpers in this
  module.

Anything fancier (inflight-call WAL recovery, encrypted-backup rotation,
process-supervised restart, etc.) is deferred to a future resilience task.
Keep this file small and honest about what it does and doesn't do.
"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

__all__ = [
    "DEFAULT_MIN_FREE_MB",
    "KeyringFallback",
    "ResilienceError",
    "disk_space_pre_check",
]


DEFAULT_MIN_FREE_MB = 50
"""Default lower bound for free disk space (in MB) before the daemon starts."""


class ResilienceError(RuntimeError):
    """Raised by helpers in :mod:`tm.resilience` when a precondition fails."""


def disk_space_pre_check(
    db_path: Path | str,
    min_free_mb: int = DEFAULT_MIN_FREE_MB,
) -> None:
    """Raise :class:`ResilienceError` if the filesystem hosting ``db_path`` has
    less than ``min_free_mb`` MB free.

    The check is performed against the parent directory of ``db_path`` (the DB
    file itself may not yet exist).  Uses :func:`shutil.disk_usage`, which is
    cheap on every supported platform.

    Parameters
    ----------
    db_path:
        Path to the SQLite database file.  Only the parent directory is
        consulted; the file does not need to exist.
    min_free_mb:
        Minimum acceptable free space in megabytes.  Defaults to
        :data:`DEFAULT_MIN_FREE_MB`.
    """
    p = Path(db_path)
    target = p.parent if p.parent != Path("") else Path(".")
    if not target.exists():
        # Hand off to the caller — they'll create the directory or fail
        # elsewhere.  We don't try to "fix" the path here.
        raise ResilienceError(
            f"disk_space_pre_check: parent directory does not exist: {target}"
        )

    usage = shutil.disk_usage(str(target))
    free_mb = usage.free / (1024 * 1024)
    if free_mb < float(min_free_mb):
        raise ResilienceError(
            f"disk_space_pre_check: only {free_mb:.1f} MB free at {target}; "
            f"need >= {min_free_mb} MB"
        )


# --------------------------------------------------------------------- keyring

# Module-level guard so the warning fires at most once per process.
_KEYRING_FALLBACK_WARNED = False


class KeyringFallback:
    """Placeholder for a future OS-keyring / SQLCipher integration.

    The v1 daemon does not encrypt the SQLite database at rest — the Unix
    socket's ``0600`` file mode is the trust boundary.  This class exists so
    downstream code can already reference the API surface (and so a future
    security task can replace the implementation without touching call sites).

    All methods currently return ``None`` and emit a one-time stderr warning so
    operators know the keyring path is a stub.  When the real implementation
    lands (a separate task), this class will look up secrets via
    `python-keyring` or an equivalent backed-by-OS-keychain library.
    """

    SERVICE_NAME = "tm-daemon"

    def get_secret(self, key: str) -> str | None:
        """Return ``None``; warn once per process that the keyring is a stub."""
        global _KEYRING_FALLBACK_WARNED
        if not _KEYRING_FALLBACK_WARNED:
            _KEYRING_FALLBACK_WARNED = True
            print(
                "warning: KeyringFallback is a stub; no real OS keyring is "
                "wired yet. Secrets such as encryption keys must come from "
                "environment variables for now.",
                file=sys.stderr,
            )
        _ = key  # explicit unused
        return None

    def set_secret(self, key: str, value: str) -> None:
        """No-op stub; logs a one-time warning."""
        global _KEYRING_FALLBACK_WARNED
        if not _KEYRING_FALLBACK_WARNED:
            _KEYRING_FALLBACK_WARNED = True
            print(
                "warning: KeyringFallback.set_secret is a stub; value not "
                "persisted. A future security task will wire up the OS keyring.",
                file=sys.stderr,
            )
        _ = (key, value)
