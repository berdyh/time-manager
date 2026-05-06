"""Typed exceptions for the debrief agent (T-INT-01).

Kept in its own module so callers (CLI, tests, future Telegram bot) can
import the exception types without pulling in the full agent machinery
(LLM client, repos, vocab aligner) at import time.

Hierarchy is shallow on purpose — keep failure modes obvious:

* :class:`DebriefError` — base class for everything raised by the agent.
* :class:`DebriefValidationError` — LLM output didn't match the expected
  shape (missing keys, wrong types, bad enum value, or duplicate summary
  for a case_date that already has one).
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
]


class DebriefError(Exception):
    """Base for all debrief-agent errors."""


class DebriefValidationError(DebriefError):
    """LLM output didn't match the expected schema, or a single-summary-per-
    case_date contract violation was detected (existing summary present).
    """


class DebriefGoalLookupError(DebriefError):
    """An event's ``advances_goal_id`` refers to a goal that doesn't exist."""


class DebriefTimestampError(DebriefError):
    """An event timestamp couldn't be parsed as ISO 8601."""
