"""Agent package — orchestrators that wire LLM + repos + cost meter.

Currently ships:

* :class:`tm.agents.debrief.DebriefAgent` — turns a free-prose end-of-day
  transcript into structured events persisted in the events table
  (T-INT-01).
* :class:`tm.agents.scheduler.SchedulerAgent` — produces at most one
  PrPM-guarded proactive suggestion per case_date (T-INT-02).

The end-to-end harness (T-INT-03) will land later as a separate module
under this package.
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
from tm.agents.scheduler import (
    CANDIDATE_SCHEMA,
    DEFAULT_MAX_PROACTIVE_SUGGESTIONS_PER_DAY,
    MAX_PROACTIVE_SUGGESTIONS_ENV_VAR,
    SCHEDULER_VERSION,
    ScheduledSuggestion,
    SchedulerAgent,
    SchedulerSkipReason,
)
from tm.agents.scheduler import (
    SYSTEM_PROMPT as SCHEDULER_SYSTEM_PROMPT,
)
from tm.agents.scheduler_errors import (
    SchedulerContextError,
    SchedulerError,
    SchedulerValidationError,
)

__all__ = [
    "CANDIDATE_SCHEMA",
    "DEFAULT_MAX_PROACTIVE_SUGGESTIONS_PER_DAY",
    "EXTRACTOR_VERSION",
    "EXTRACT_SCHEMA",
    "MAX_PROACTIVE_SUGGESTIONS_ENV_VAR",
    "SCHEDULER_SYSTEM_PROMPT",
    "SCHEDULER_VERSION",
    "SUMMARY_ACTIVITY",
    "SYSTEM_PROMPT",
    "DebriefAgent",
    "DebriefError",
    "DebriefGoalLookupError",
    "DebriefResult",
    "DebriefTimestampError",
    "DebriefValidationError",
    "ExtractedEvent",
    "ScheduledSuggestion",
    "SchedulerAgent",
    "SchedulerContextError",
    "SchedulerError",
    "SchedulerSkipReason",
    "SchedulerValidationError",
]
