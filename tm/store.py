"""Store facade: read Protocol + concrete write-capable Store class.

The ``StoreReader`` Protocol exposes only read methods so type-checkers can
catch accidental writes from non-daemon callers. The concrete ``Store`` class
implements both reads and writes; it is the single canonical persistence
surface used by the daemon (writer) and CLI/bot (readers).

Concrete backend lives in :mod:`tm.stores.sqlite_store`. Keep this module
small: later tasks add a couple of methods each.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from tm.stores.sqlite_store import (
    MigrationError,
    MigrationPostTxnError,
    MigrationPragmaError,
    SQLiteStore,
)

# MigrationIntegrityError is defined below.  It must extend MigrationError so
# that callers who write ``except MigrationError:`` automatically catch checksum-
# drift failures without needing to list two exception types.

__all__ = [
    "MigrationError",
    "MigrationIntegrityError",
    "MigrationPostTxnError",
    "MigrationPragmaError",
    "Store",
    "StoreReader",
]


class MigrationIntegrityError(MigrationError):
    """Raised when a previously-applied migration's checksum no longer matches.

    Extends :class:`MigrationError` so that ``except MigrationError:`` catches
    all migration-runner failures, including checksum-drift detection.

    Attributes:
        version: the migration version with mismatched checksum.
        recorded_checksum: the sha256 stored in ``schema_migrations``.
        file_checksum: the sha256 of the on-disk migration body.
    """

    def __init__(
        self, version: int, recorded_checksum: str, file_checksum: str
    ) -> None:
        super().__init__(
            f"migration {version:04d}: checksum drift "
            f"(recorded={recorded_checksum}, on_disk={file_checksum})"
        )
        self.version = version
        self.recorded_checksum = recorded_checksum
        self.file_checksum = file_checksum


@runtime_checkable
class StoreReader(Protocol):
    """Read-only view of the Store. Mypy uses this to forbid writes."""

    def get_event(self, event_id: str) -> dict[str, Any] | None: ...

    def query_events(
        self,
        *,
        case_id: str | None = ...,
        since: str | None = ...,
        until: str | None = ...,
        activity: str | None = ...,
        limit: int | None = ...,
    ) -> list[dict[str, Any]]: ...

    def applied_migrations(self) -> list[int]: ...


class Store(SQLiteStore):
    """Concrete read+write store. Implements :class:`StoreReader` plus writes.

    Write methods (``apply_pending_migrations``, ``append_event``) are inherited
    from :class:`SQLiteStore`. The daemon will be the only legitimate caller of
    these once T-FND-06 lands.
    """


def _assert_reader_is_protocol() -> None:
    """Static check: a Store instance is a StoreReader, but a StoreReader-typed
    reference does not expose write methods. Verified by tests/types/.
    """
