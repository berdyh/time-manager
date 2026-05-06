"""Typed exceptions for the scheduler agent (T-INT-02).

Kept in its own module so callers (CLI, tests, future Telegram bot, daemon
batch) can import the exception types without pulling in the full agent
machinery (LLM client, repos, cost meter, ProcessMiner, VariantClusterer).

Hierarchy is shallow on purpose, mirroring the debrief-agent pattern:

* :class:`SchedulerError` — base class for everything raised by the agent.
* :class:`SchedulerValidationError` — LLM output didn't match the expected
  shape (missing keys, wrong types, out-of-range numerics).
* :class:`SchedulerContextError` — the context payload that feeds the LLM
  call could not be built (e.g. ProcessMiner / VariantClusterer threw an
  unrecoverable error).

The agent does NOT wrap unrelated exceptions into these types. SDK errors
from the LLM adapter, :class:`tm.llm.errors.CostCapExceeded` from the cost
meter, and :class:`ValueError` from repository validation propagate as-is —
this is a deliberate tradeoff per the T-FND-04 carry-forward intel: callers
own retry policy, and the agent does not paper over transient failures with
broad catches.
"""

from __future__ import annotations

__all__ = [
    "SchedulerContextError",
    "SchedulerError",
    "SchedulerValidationError",
]


class SchedulerError(Exception):
    """Base for all scheduler-agent errors."""


class SchedulerValidationError(SchedulerError):
    """LLM output did not match the expected schema.

    Raised when the structured-extract dict is missing a required key, has a
    field of the wrong type, or carries an out-of-range numeric (e.g.
    ``predicted_outcome_with`` outside ``[0, 2]``).  The scheduler validates
    defensively because :meth:`tm.llm.client.LLMClient.extract` does NOT
    validate against the supplied schema (T-FND-04 carry-forward).
    """


class SchedulerContextError(SchedulerError):
    """Context payload for the LLM call could not be built.

    Raised when ProcessMiner / VariantClusterer / repository helpers throw an
    unrecoverable error while assembling the context that gets fed to the LLM.
    The scheduler treats empty-but-valid context as a soft signal (returns a
    :class:`tm.agents.scheduler.SchedulerSkipReason` with
    ``reason='empty_context'``) rather than raising; this exception is for
    truly broken inputs (e.g. malformed case_date, repo failure).
    """
