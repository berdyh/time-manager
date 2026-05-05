"""Goal model — dataclass-based (no Pydantic dependency).

Design note: Pydantic v2 is NOT yet an explicit project dep. Once
``anthropic``'s transitive pull of pydantic is confirmed stable, a follow-up
task (T-GOAL-01+1) should convert this to Pydantic v2 BaseModel. For now,
we use plain ``dataclasses`` + manual validation to keep the surface narrow and
dependency-free.

ULID generation: small custom implementation for time-sortable IDs.
  - 48-bit millisecond timestamp (prefix ensures lexicographic sort)
  - 80-bit cryptographically random component
  - Crockford base32 encoding (avoids ambiguous chars I, L, O, U)
  - Total 26 characters, always uppercase
"""

from __future__ import annotations

import os
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

__all__ = [
    "Goal",
    "Status",
    "ulid",
]

# Status is a type alias; the literal strings are the canonical values.
Status = Literal["active", "completed", "abandoned"]

# ---------------------------------------------------------------------------
# ULID helper
# ---------------------------------------------------------------------------

# Crockford base32 alphabet (no I, L, O, U)
_CROCKFORD = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"


def ulid() -> str:
    """Generate a ULID (Universally Unique Lexicographically Sortable Identifier).

    Returns a 26-character uppercase string.  Sortable by creation time to
    millisecond precision.  Uses ``os.urandom`` for the random component so
    it is safe for concurrent generation.

    Reference: https://github.com/ulid/spec
    """
    # 48-bit timestamp in milliseconds
    ts_ms = int(time.time() * 1000) & 0xFFFFFFFFFFFF  # 6 bytes
    # 80-bit random
    rand = int.from_bytes(os.urandom(10), "big")

    # Encode timestamp: 10 base32 chars (5 bits each → 50 bits fits 48-bit ts)
    t = ts_ms
    ts_chars: list[str] = []
    for _ in range(10):
        ts_chars.append(_CROCKFORD[t & 0x1F])
        t >>= 5
    ts_part = "".join(reversed(ts_chars))

    # Encode random: 16 base32 chars (5 bits each → 80 bits)
    r = rand
    rand_chars: list[str] = []
    for _ in range(16):
        rand_chars.append(_CROCKFORD[r & 0x1F])
        r >>= 5
    rand_part = "".join(reversed(rand_chars))

    return ts_part + rand_part


# ---------------------------------------------------------------------------
# Goal dataclass
# ---------------------------------------------------------------------------

_VALID_STATUSES: frozenset[str] = frozenset({"active", "completed", "abandoned"})


def _validate_status(value: str) -> str:
    if value not in _VALID_STATUSES:
        raise ValueError(
            f"status must be one of {sorted(_VALID_STATUSES)!r}, got {value!r}"
        )
    return value


def _validate_priority(value: int | None) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or not (1 <= value <= 3):
        raise ValueError(
            f"priority must be an integer in [1, 3] or None, got {value!r}"
        )
    return value


@dataclass
class Goal:
    """A first-class goal entity.

    ``goal_id`` is a ULID generated at insert time.  All timestamps are stored
    as ISO 8601 strings (UTC) to match the SQLite TEXT column convention used
    throughout this project.

    Validation is performed in ``__post_init__``; create via
    ``GoalsRepository.add(...)`` for database-backed instances.
    """

    goal_id: str
    name: str
    description: str | None = None
    status: str = "active"
    priority: int | None = None
    target_completion_at: str | None = None
    created_at: str = field(
        default_factory=lambda: datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    completed_at: str | None = None
    abandoned_at: str | None = None
    abandon_reason: str | None = None

    def __post_init__(self) -> None:
        if not self.name or not self.name.strip():
            raise ValueError("Goal.name must not be empty")
        _validate_status(self.status)
        _validate_priority(self.priority)
