"""mypy enforcement probe for StoreReader Protocol.

Running `mypy tests/types/probe.py` MUST produce attr-defined errors for the
write-method calls below. tests/types/test_protocol_enforcement.py asserts this.
"""

from tm.store import StoreReader


def must_fail_apply(reader: StoreReader) -> None:
    reader.apply_pending_migrations()  # type: ignore[attr-defined]


def must_fail_append(reader: StoreReader) -> None:
    reader.append_event({})  # type: ignore[attr-defined]


def must_pass_get(reader: StoreReader) -> None:
    _ = reader.get_event("some-id")
