"""Scheduler success metric: delta-outcome learning signal from suggestion telemetry.

This module aggregates rows from
:class:`tm.repositories.telemetry.SuggestionTelemetryRepository` against
per-case actual outcomes (already populated on the same row by
:meth:`SuggestionTelemetryRepository.record_actual_outcome`).  It does NOT
re-derive ``actual_outcome`` from events — that's the contract of
:class:`tm.models.outcome.OutcomeAggregator`, but populating the telemetry
row is the responsibility of whoever closes out the scheduling loop (T-INT-02
or a downstream daemon).

The :class:`OutcomeAggregator` reference is held so future iterations can
back-fill or cross-check actuals on demand, but the current
:meth:`SchedulerSuccessMetric.summarize` and
:meth:`SchedulerSuccessMetric.per_suggestion_diagnostics` only consume the
already-persisted ``actual_outcome`` column.

Signal definitions
------------------

For each row with a populated ``actual_outcome``:

* ``predicted_delta = predicted_outcome_with - predicted_outcome_without``
* ``actual_minus_predicted_with = actual_outcome - predicted_outcome_with``
* ``hit`` (binary): ``actual_outcome > predicted_outcome_without`` — the
  suggestion was useful in the sense that the actual day exceeded the no-
  action baseline.

Aggregates returned by :meth:`SchedulerSuccessMetric.summarize`:

* ``mean_predicted_outcome_delta``: mean of ``predicted_delta`` across rows
  with ``actual_outcome`` set.
* ``mean_actual_outcome_score``: mean of ``actual_outcome`` across rows
  with it set.
* ``mean_actual_minus_predicted_with``: mean of
  ``actual - predicted_outcome_with`` (model-fit signal).
* ``hit_rate_actual_above_predicted_without``: fraction of rows with
  ``actual_outcome > predicted_outcome_without``.

Edge cases
----------

* Zero rows in the window → :class:`SchedulerMetricSummary` with all metric
  fields ``None`` and counts ``0``.
* Rows without ``actual_outcome`` (suggestion logged but day not closed
  out) → counted in ``total_suggestions`` and in thumbs counts, but
  excluded from the actual-side aggregates.
* ``explicit_thumbs == 1`` → up; ``== 0`` → down; ``None`` → neither.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tm.models.outcome import OutcomeAggregator
from tm.repositories.telemetry import SuggestionRecord, SuggestionTelemetryRepository

__all__ = [
    "SchedulerMetricSummary",
    "SchedulerSuccessMetric",
]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SchedulerMetricSummary:
    """Aggregate scheduler success metric over a time window.

    All ``mean_*`` and ``hit_rate_*`` fields are ``None`` when the underlying
    sample is empty (zero suggestions with ``actual_outcome`` set).
    """

    total_suggestions: int
    suggestions_with_actual: int
    mean_predicted_outcome_delta: float | None
    mean_actual_outcome_score: float | None
    mean_actual_minus_predicted_with: float | None
    hit_rate_actual_above_predicted_without: float | None
    thumbs_up_count: int
    thumbs_down_count: int
    extractor_metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# SchedulerSuccessMetric
# ---------------------------------------------------------------------------


class SchedulerSuccessMetric:
    """Aggregate suggestion telemetry against actual day-end outcomes.

    Parameters
    ----------
    telemetry_repo:
        :class:`SuggestionTelemetryRepository` providing the source rows.
    outcome_aggregator:
        :class:`OutcomeAggregator` retained for forward-compat (e.g. to
        validate / back-fill ``actual_outcome`` on a future iteration).  Not
        invoked by the current methods.
    """

    def __init__(
        self,
        telemetry_repo: SuggestionTelemetryRepository,
        outcome_aggregator: OutcomeAggregator,
    ) -> None:
        self._telemetry_repo = telemetry_repo
        self._outcome_aggregator = outcome_aggregator

    # ------------------------------------------------------------------
    # Window aggregate
    # ------------------------------------------------------------------

    def summarize(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> SchedulerMetricSummary:
        """Return aggregate signals over the [``since``, ``until``] window.

        Both ``since`` and ``until`` are matched against ``suggested_at``
        inclusive.  When both are ``None`` the entire telemetry table is
        scanned.

        See module docstring for the precise definitions of each field.
        """
        records = self._telemetry_repo.list_recent(since=since, until=until)
        return _summarize_records(records, since=since, until=until)

    # ------------------------------------------------------------------
    # Per-suggestion diagnostics
    # ------------------------------------------------------------------

    def per_suggestion_diagnostics(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
    ) -> list[dict[str, Any]]:
        """Return one diagnostic dict per row with ``actual_outcome`` set.

        Each dict contains:

        * ``suggestion_id`` (str)
        * ``case_date`` (str)
        * ``case_goal_id`` (str | None)
        * ``recommended_action`` (str)
        * ``predicted_with`` (float)
        * ``predicted_without`` (float)
        * ``predicted_delta`` (float)  — ``with − without``
        * ``actual`` (int)             — the recorded ``actual_outcome``
        * ``actual_minus_predicted_with`` (float)
        * ``is_hit`` (bool)            — ``actual > predicted_without``
        * ``suggested_at`` (str)
        """
        records = self._telemetry_repo.list_recent(since=since, until=until)
        out: list[dict[str, Any]] = []
        for r in records:
            if r.actual_outcome is None:
                continue
            actual = float(r.actual_outcome)
            predicted_with = float(r.predicted_outcome_with)
            predicted_without = float(r.predicted_outcome_without)
            predicted_delta = predicted_with - predicted_without
            out.append(
                {
                    "suggestion_id": r.suggestion_id,
                    "case_date": r.case_date,
                    "case_goal_id": r.case_goal_id,
                    "recommended_action": r.recommended_action,
                    "predicted_with": predicted_with,
                    "predicted_without": predicted_without,
                    "predicted_delta": predicted_delta,
                    "actual": int(r.actual_outcome),
                    "actual_minus_predicted_with": actual - predicted_with,
                    "is_hit": actual > predicted_without,
                    "suggested_at": r.suggested_at,
                }
            )
        return out


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _summarize_records(
    records: list[SuggestionRecord],
    *,
    since: str | None,
    until: str | None,
) -> SchedulerMetricSummary:
    """Pure helper: turn an iterable of :class:`SuggestionRecord` into a
    :class:`SchedulerMetricSummary`."""
    metadata: dict[str, Any] = {
        "since": since,
        "until": until,
    }

    total = len(records)
    if total == 0:
        return SchedulerMetricSummary(
            total_suggestions=0,
            suggestions_with_actual=0,
            mean_predicted_outcome_delta=None,
            mean_actual_outcome_score=None,
            mean_actual_minus_predicted_with=None,
            hit_rate_actual_above_predicted_without=None,
            thumbs_up_count=0,
            thumbs_down_count=0,
            extractor_metadata=metadata,
        )

    predicted_deltas: list[float] = []
    actual_scores: list[int] = []
    actual_minus_with: list[float] = []
    hits = 0
    thumbs_up = 0
    thumbs_down = 0

    for r in records:
        if r.explicit_thumbs == 1:
            thumbs_up += 1
        elif r.explicit_thumbs == 0:
            thumbs_down += 1

        if r.actual_outcome is None:
            continue
        actual = float(r.actual_outcome)
        predicted_with = float(r.predicted_outcome_with)
        predicted_without = float(r.predicted_outcome_without)
        predicted_deltas.append(predicted_with - predicted_without)
        actual_scores.append(int(r.actual_outcome))
        actual_minus_with.append(actual - predicted_with)
        if actual > predicted_without:
            hits += 1

    n_with_actual = len(actual_scores)
    if n_with_actual == 0:
        return SchedulerMetricSummary(
            total_suggestions=total,
            suggestions_with_actual=0,
            mean_predicted_outcome_delta=None,
            mean_actual_outcome_score=None,
            mean_actual_minus_predicted_with=None,
            hit_rate_actual_above_predicted_without=None,
            thumbs_up_count=thumbs_up,
            thumbs_down_count=thumbs_down,
            extractor_metadata=metadata,
        )

    return SchedulerMetricSummary(
        total_suggestions=total,
        suggestions_with_actual=n_with_actual,
        mean_predicted_outcome_delta=(sum(predicted_deltas) / float(n_with_actual)),
        mean_actual_outcome_score=(sum(actual_scores) / float(n_with_actual)),
        mean_actual_minus_predicted_with=(
            sum(actual_minus_with) / float(n_with_actual)
        ),
        hit_rate_actual_above_predicted_without=(hits / float(n_with_actual)),
        thumbs_up_count=thumbs_up,
        thumbs_down_count=thumbs_down,
        extractor_metadata=metadata,
    )
