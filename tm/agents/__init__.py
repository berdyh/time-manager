"""Agent package — orchestrators that wire LLM + repos + cost meter.

Currently ships:

* :class:`tm.agents.debrief.DebriefAgent` — turns a free-prose end-of-day
  transcript into structured events persisted in the events table
  (T-INT-01).

The scheduler agent (T-INT-02) and end-to-end harness (T-INT-03) will
land later as separate modules under this package.
"""

from __future__ import annotations

from tm.agents.debrief import (
    EXTRACT_SCHEMA,
    EXTRACTOR_VERSION,
    SUMMARY_ACTIVITY,
    SYSTEM_PROMPT,
    DebriefAgent,
    DebriefResult,
    ExtractedEvent,
)
from tm.agents.debrief_errors import (
    DebriefError,
    DebriefGoalLookupError,
    DebriefTimestampError,
    DebriefValidationError,
)

__all__ = [
    "EXTRACTOR_VERSION",
    "EXTRACT_SCHEMA",
    "SUMMARY_ACTIVITY",
    "SYSTEM_PROMPT",
    "DebriefAgent",
    "DebriefError",
    "DebriefGoalLookupError",
    "DebriefResult",
    "DebriefTimestampError",
    "DebriefValidationError",
    "ExtractedEvent",
]
