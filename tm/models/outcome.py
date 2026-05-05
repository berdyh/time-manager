"""Daily outcome scoring per locked v1 plan.

outcome_score in {0, 1, 2} = int(did_complete_tasks) + int(did_advance_goal)

did_complete_tasks: planned_tasks_completed / planned_tasks_total >= 0.80
did_advance_goal:   any event in the case has advances_goal != None

This module is split into two boundaries:

* :func:`compute_outcome_breakdown` is a PURE function over an iterable of
  event dicts (the shape produced by ``EventsRepository._row_to_event``).
  It performs no I/O and is straightforward to unit-test.
* :class:`OutcomeAggregator` is the integration boundary that knows
  :class:`tm.repositories.events.EventsRepository` and glues database
  queries to the pure scorer for the two case lenses defined in the plan
  (workday ``case_date`` and goal-pursuit ``case_goal_id``).

Out of scope for T-OUT-01 (handled by downstream tasks):

* variant cluster labels (T-OUT-03)
* scheduler success metric (T-OUT-03)
* the LLM-driven debrief that POPULATES ``planned_tasks_completed`` /
  ``planned_tasks_total`` in ``events.attributes`` (T-INT-01)
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from tm.repositories.events import EventsRepository

__all__ = [
    "DEFAULT_TASK_COMPLETION_THRESHOLD",
    "OutcomeAggregator",
    "OutcomeBreakdown",
    "compute_outcome_breakdown",
    "compute_outcome_score",
]

# Threshold per plan: 0.80 = "did finish daily plan"
DEFAULT_TASK_COMPLETION_THRESHOLD: float = 0.80


@dataclass(frozen=True)
class OutcomeBreakdown:
    """Result of :func:`compute_outcome_breakdown` with diagnostic context.

    The dataclass is frozen so that downstream callers can safely treat it as
    a value object (e.g. cache keys, equality checks in tests).
    """

    outcome_score: int  # 0, 1, or 2
    did_complete_tasks: bool
    did_advance_goal: bool
    planned_tasks_completed: int  # max value observed across events (0 if none)
    planned_tasks_total: int  # max value observed across events (0 if none)
    advancing_goal_event_count: int  # count of events with advances_goal != None
    case_event_count: int  # total events considered


def _coerce_int(value: Any) -> int:
    """Coerce a raw attribute value to a non-negative int.

    The LLM extractor (T-INT-01) is expected to populate
    ``planned_tasks_completed`` / ``planned_tasks_total`` as JSON ints, but
    historically LLM outputs may also surface as strings (``"8"``).  We accept
    both, and fall back to ``0`` for anything we cannot decode (None, bool,
    floats with no integer interpretation, garbage strings, etc.).

    Booleans are explicitly rejected here even though ``bool`` is an ``int``
    subclass in Python — a debrief writing ``True`` for ``planned_tasks_total``
    is far more likely a bug than the literal value ``1``.
    """
    if value is None or isinstance(value, bool):
        return 0
    if isinstance(value, int):
        return max(0, value)
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):  # NaN / inf
            return 0
        return max(0, int(value))
    if isinstance(value, str):
        try:
            return max(0, int(value.strip()))
        except (ValueError, AttributeError):
            return 0
    return 0


def compute_outcome_breakdown(
    events: Iterable[dict[str, Any]],
    *,
    threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
) -> OutcomeBreakdown:
    """Pure function: compute outcome score from an iterable of event dicts.

    Each event dict is expected to follow the shape produced by
    :func:`tm.repositories.events._row_to_event` — specifically:

    * ``"attributes"``: ``dict[str, Any]`` (already-parsed JSON)
    * ``"advances_goal"``: ``str | None``

    Reads ``attributes['planned_tasks_completed']`` and
    ``attributes['planned_tasks_total']``.  When multiple events expose these
    attributes, takes the **maximum** value of each across the iterable on the
    assumption that the latest debrief / summary event carries the canonical
    day-level totals (lower-water-mark events from earlier in the day are
    superseded).

    If ``planned_tasks_total <= 0`` after aggregation, OR neither attribute
    appears anywhere, ``did_complete_tasks`` is ``False``.

    Parameters
    ----------
    events:
        Any iterable of event dicts.  Consumed once.
    threshold:
        Completion ratio at which ``did_complete_tasks`` flips True.
        Defaults to :data:`DEFAULT_TASK_COMPLETION_THRESHOLD` (0.80).
        Must be in (0, 1].

    Returns
    -------
    OutcomeBreakdown
        Frozen dataclass with the score and diagnostic counters.
    """
    if not (0.0 < threshold <= 1.0):
        raise ValueError(f"threshold must be in (0, 1], got {threshold!r}")

    max_completed = 0
    max_total = 0
    advancing_count = 0
    event_count = 0

    for event in events:
        event_count += 1
        attributes = event.get("attributes") or {}
        if isinstance(attributes, dict):
            completed = _coerce_int(attributes.get("planned_tasks_completed"))
            total = _coerce_int(attributes.get("planned_tasks_total"))
            if completed > max_completed:
                max_completed = completed
            if total > max_total:
                max_total = total
        if event.get("advances_goal") is not None:
            advancing_count += 1

    if max_total > 0:
        ratio = max_completed / max_total
        did_complete_tasks = ratio >= threshold
    else:
        did_complete_tasks = False

    did_advance_goal = advancing_count > 0
    outcome_score = int(did_complete_tasks) + int(did_advance_goal)

    return OutcomeBreakdown(
        outcome_score=outcome_score,
        did_complete_tasks=did_complete_tasks,
        did_advance_goal=did_advance_goal,
        planned_tasks_completed=max_completed,
        planned_tasks_total=max_total,
        advancing_goal_event_count=advancing_count,
        case_event_count=event_count,
    )


def compute_outcome_score(
    events: Iterable[dict[str, Any]],
    *,
    threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
) -> int:
    """Convenience wrapper returning just the integer score in ``{0, 1, 2}``."""
    return compute_outcome_breakdown(events, threshold=threshold).outcome_score


class OutcomeAggregator:
    """Per-case aggregator: fetches events for a case lens and computes outcome.

    Two case lenses are supported per the plan:

    * **workday** (``case_date``) — every persisted event has ``case_date``
      set; this lens is always-on.
    * **goal-pursuit** (``case_goal_id``) — events explicitly tagged with the
      goal-pursuit case identifier.

    The aggregator is a thin glue layer: it owns no state beyond a reference to
    the underlying :class:`EventsRepository` (read-only usage; this class never
    writes events).
    """

    def __init__(self, events_repo: EventsRepository) -> None:
        self._events_repo = events_repo

    def for_case_date(
        self,
        case_date: str,
        *,
        threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
    ) -> OutcomeBreakdown:
        """Compute outcome for a workday case (``case_date='YYYY-MM-DD'``).

        Empty-string ``case_date`` (the unset sentinel from migration 0006)
        is rejected with :exc:`ValueError` rather than silently returning an
        empty breakdown — empty case_date means "unset", not "match all".
        """
        if not case_date:
            raise ValueError("case_date must be a non-empty 'YYYY-MM-DD' string")
        events = self._events_repo.query_events(case_date=case_date)
        return compute_outcome_breakdown(events, threshold=threshold)

    def for_case_goal_id(
        self,
        case_goal_id: str,
        *,
        threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
    ) -> OutcomeBreakdown:
        """Compute outcome for a goal-pursuit case (across all dates)."""
        if not case_goal_id:
            raise ValueError("case_goal_id must be a non-empty string")
        events = self._events_repo.query_events(case_goal_id=case_goal_id)
        return compute_outcome_breakdown(events, threshold=threshold)

    def for_date_range(
        self,
        *,
        since: str,
        until: str | None = None,
        threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
    ) -> dict[str, OutcomeBreakdown]:
        """Bulk: return ``{case_date: breakdown}`` for distinct dates in window.

        Uses :meth:`EventsRepository.list_distinct_case_dates` to enumerate the
        distinct ``case_date`` values in ``[since, until]`` (which already
        excludes the empty-string sentinel), then evaluates each.

        Cost is O(N_dates * 1 query) which is fine for v1 daily-rollup
        volumes (typical N <= 30).
        """
        case_dates = self._events_repo.list_distinct_case_dates(
            since=since, until=until
        )
        return {
            case_date: self.for_case_date(case_date, threshold=threshold)
            for case_date in case_dates
        }
