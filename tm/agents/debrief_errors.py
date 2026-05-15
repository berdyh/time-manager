"""Typed exceptions for the debrief agent (T-INT-01).

Kept in its own module so callers (CLI, tests, future Telegram bot) can
import the exception types without pulling in the full agent machinery
(LLM client, repos, vocab aligner) at import time.

Hierarchy is shallow on purpose — keep failure modes obvious:

* :class:`DebriefError` — base class for everything raised by the agent.
* :class:`DebriefValidationError` — LLM output didn't match the expected
  shape (missing keys, wrong types, bad enum value, or duplicate summary
  for a case_date that already has one — surfaced PRE-LLM-call via the
  fast-path SELECT).
* :class:`DuplicateSummaryError` — race-induced violation of the single-
  summary-per-case_date contract surfaced by SQLite's UNIQUE constraint at
  INSERT time. Distinct from :class:`DebriefValidationError` because the
  pre-call guard already passed: another writer beat us between the SELECT
  and the INSERT. Added in T-PM-DEBRIEF-UNIQUE alongside migration 0010.
* :class:`DebriefGoalLookupError` — an event's ``advances_goal_id`` refers
  to a goal that does not exist in the goals table.
* :class:`DebriefTimestampError` — an event timestamp couldn't be parsed
  as ISO 8601.

The agent does NOT wrap unrelated exceptions into these types; SDK errors
from the LLM adapter, ``CostCapExceeded`` from the cost meter, and
``ValueError`` from an empty-string transcript propagate as-is. This is a
deliberate tradeoff per the T-FND-04 carry-forward intel: callers
(scheduler, CLI) own retry policy, and the agent does not paper over
transient failures with broad catches.
"""

from __future__ import annotations

__all__ = [
    "DebriefError",
    "DebriefGoalLookupError",
    "DebriefTimestampError",
    "DebriefValidationError",
    "DuplicateSummaryError",
]


class DebriefError(Exception):
    """Base for all debrief-agent errors."""


class DebriefValidationError(DebriefError):
    """LLM output didn't match the expected schema, or a single-summary-per-
    case_date contract violation was detected (existing summary present).
    """


class DuplicateSummaryError(DebriefError):
    """Race-induced single-summary-per-case_date violation at INSERT time.

    Raised when the SQLite partial UNIQUE index added by migration 0010
    (``uq_events_summary_per_case_date``) rejects a ``debrief_summary``
    INSERT because another writer already persisted a summary for the
    same ``case_date`` between our pre-call SELECT and this INSERT.

    The two related-but-distinct error shapes:

    * :class:`DebriefValidationError` — the SELECT-time guard fired
      (a summary already existed BEFORE we tried to write).
    * :class:`DuplicateSummaryError` — the UNIQUE-index INSERT failed
      (a summary appeared DURING our write, by a concurrent writer).

    Carries ``case_date`` so callers (CLI, daemon RPC envelope) can
    render a precise, actionable message.

    Attributes
    ----------
    case_date:
        The ``YYYY-MM-DD`` that had the collision.
    detail:
        Optional human-readable context (typically the underlying
        ``sqlite3.IntegrityError`` message).
    """

    def __init__(self, case_date: str, detail: str | None = None) -> None:
        self.case_date = case_date
        self.detail = detail
        message = (
            f"duplicate debrief_summary for case_date={case_date!r}"
            if detail is None
            else f"duplicate debrief_summary for case_date={case_date!r}: {detail}"
        )
        super().__init__(message)


class DebriefGoalLookupError(DebriefError):
    """An event's ``advances_goal_id`` refers to a goal that doesn't exist."""


class DebriefTimestampError(DebriefError):
    """An event timestamp couldn't be parsed as ISO 8601."""
