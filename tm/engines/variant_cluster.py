"""Variant cluster labeling: group ProcessMiner variants by mean outcome_score.

This module is the bridge between
:meth:`tm.engines.process_mining.ProcessMiner.analyze_variants` and
:class:`tm.models.outcome.OutcomeAggregator`.  It does NOT itself run process
mining or compute outcome scores — those are computed by their respective
modules.  Instead, the clusterer walks each :class:`Variant`, looks up the
per-case outcome via :class:`OutcomeAggregator`, computes a mean per variant,
and emits a label.

Two case lenses are supported (mirroring T-PM-02):

* ``workday`` — ``Variant.case_ids`` are ``case_date`` strings (YYYY-MM-DD);
  the clusterer calls :meth:`OutcomeAggregator.for_case_date`.  Labels are
  one of ``good_day`` (mean outcome ≥ 1.5), ``mixed`` (0.5 ≤ mean < 1.5),
  ``bad_day`` (mean < 0.5).
* ``goal_pursuit`` — ``Variant.case_ids`` are ``goal_id`` strings; the
  clusterer calls :meth:`OutcomeAggregator.for_case_goal_id`.  Labels are
  one of ``effective_pursuit`` (mean outcome ≥ ``effective_outcome_threshold``
  AND case_count ≤ ``effective_throughput_max``) or ``stuck`` (otherwise).

Boundary semantics
------------------

The thresholds are evaluated with ``>=`` at the upper boundary and ``<`` at
the lower boundary, so:

* ``mean_outcome == 1.5`` → ``good_day`` (>= inclusive).
* ``mean_outcome == 0.5`` → ``mixed`` (>= 0.5 and < 1.5).
* ``mean_outcome == 0.0`` → ``bad_day`` (< 0.5).

Edge cases
----------

* A variant whose ``case_ids`` contain no cases with computable outcome
  (e.g. events lack ``planned_tasks_*`` attributes and no ``advances_goal``)
  will end up with ``mean_outcome_score = 0.0`` and ``case_count_with_outcome
  = 0``.  By the workday rules above, that variant is labeled ``bad_day``.
  ``OutcomeAggregator`` already returns ``outcome_score = 0`` for such
  cases — we still treat them as "scored" cases for the purpose of computing
  the mean (they pull the mean toward 0).  This is intentional: a variant
  that produces only no-outcome cases IS a bad-day cluster from the user's
  perspective.
* Empty ``VariantAnalysis.variants`` (no variants) → empty labeled tuple,
  empty summary, ``total_cases = cases_with_outcome = 0``.
* Variants whose ``case_count == 0`` (defensive — should never happen
  because :meth:`ProcessMiner.analyze_variants` only emits variants with at
  least one case) are still labeled ``bad_day`` for the workday lens and
  ``stuck`` for goal_pursuit, with ``mean_outcome_score = 0.0``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from tm.engines.process_mining import Variant, VariantAnalysis
from tm.models.outcome import (
    DEFAULT_TASK_COMPLETION_THRESHOLD,
    OutcomeAggregator,
    OutcomeBreakdown,
)
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository

__all__ = [
    "BAD_DAY_THRESHOLD",
    "ClusterLabel",
    "EFFECTIVE_OUTCOME_THRESHOLD",
    "EFFECTIVE_THROUGHPUT_MAX",
    "GOOD_DAY_THRESHOLD",
    "LabeledVariant",
    "VariantClusterer",
    "VariantClustering",
]

# ---------------------------------------------------------------------------
# Public constants — exposed so callers / tests can introspect the thresholds
# ---------------------------------------------------------------------------

#: Mean outcome at or above which a workday-lens variant is labeled ``good_day``.
GOOD_DAY_THRESHOLD: float = 1.5

#: Mean outcome strictly below which a workday-lens variant is labeled ``bad_day``.
BAD_DAY_THRESHOLD: float = 0.5

#: Mean outcome at or above which a goal-pursuit variant is *eligible* to be
#: labeled ``effective_pursuit``.  The throughput cap below must also hold.
EFFECTIVE_OUTCOME_THRESHOLD: float = 1.5

#: Maximum case count for a goal-pursuit variant to be considered
#: ``effective_pursuit``; above this the variant is ``stuck`` regardless of
#: outcome (many cases needed to advance the goal => stuck pattern).
EFFECTIVE_THROUGHPUT_MAX: int = 7


ClusterLabel = Literal["good_day", "mixed", "bad_day", "effective_pursuit", "stuck"]


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LabeledVariant:
    """A :class:`Variant` annotated with its mean outcome and cluster label.

    Attributes
    ----------
    variant:
        The original :class:`Variant` from ``ProcessMiner.analyze_variants``.
    mean_outcome_score:
        Arithmetic mean of :attr:`OutcomeBreakdown.outcome_score` across all
        cases in :attr:`Variant.case_ids` whose outcome is computable.
        ``0.0`` when no case has a computable outcome.
    case_count_with_outcome:
        Number of cases in :attr:`Variant.case_ids` whose outcome contributed
        to :attr:`mean_outcome_score`.  May equal ``0``.
    label:
        Cluster label per the workday or goal_pursuit ruleset (see module
        docstring).
    """

    variant: Variant
    mean_outcome_score: float
    case_count_with_outcome: int
    label: ClusterLabel


@dataclass(frozen=True)
class VariantClustering:
    """Output of :meth:`VariantClusterer.cluster_workday_variants` and
    :meth:`VariantClusterer.cluster_goal_pursuit_variants`.

    Attributes
    ----------
    labeled_variants:
        One :class:`LabeledVariant` per :class:`Variant` in the input
        :class:`VariantAnalysis`, preserved in input order (case_count desc,
        sequence asc — see :meth:`ProcessMiner.analyze_variants`).
    summary:
        ``{label: count_of_variants_with_that_label}``.  Sums to
        ``len(labeled_variants)``.
    total_cases:
        Sum of :attr:`Variant.case_count` across all input variants.
    cases_with_outcome:
        Sum of :attr:`LabeledVariant.case_count_with_outcome` across all
        labeled variants.
    extractor_metadata:
        Diagnostic dict with the lens, thresholds, and (for workday) the
        ``since`` / ``until`` window forwarded by the caller.
    """

    labeled_variants: tuple[LabeledVariant, ...]
    summary: dict[str, int]
    total_cases: int
    cases_with_outcome: int
    extractor_metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# VariantClusterer
# ---------------------------------------------------------------------------


class VariantClusterer:
    """Aggregate variants and label them by mean per-case outcome score.

    Parameters
    ----------
    events_repo:
        Required by some downstream consumers; stored unmodified so
        future work (e.g. window-aware filtering) has access without an API
        churn.  This class never writes events.
    outcome_aggregator:
        The aggregator used to fetch per-case :class:`OutcomeBreakdown`.
    good_day_threshold:
        Mean-outcome lower bound (inclusive) for ``good_day``.  Defaults to
        :data:`GOOD_DAY_THRESHOLD` (1.5).
    bad_day_threshold:
        Mean-outcome upper bound (exclusive) for ``bad_day``.  Defaults to
        :data:`BAD_DAY_THRESHOLD` (0.5).
    effective_outcome_threshold:
        Mean-outcome lower bound (inclusive) for ``effective_pursuit``.
        Defaults to :data:`EFFECTIVE_OUTCOME_THRESHOLD` (1.5).
    effective_throughput_max:
        Maximum case_count for ``effective_pursuit`` (inclusive upper bound).
        Defaults to :data:`EFFECTIVE_THROUGHPUT_MAX` (7).
    """

    def __init__(
        self,
        events_repo: EventsRepository,
        outcome_aggregator: OutcomeAggregator,
        *,
        good_day_threshold: float = GOOD_DAY_THRESHOLD,
        bad_day_threshold: float = BAD_DAY_THRESHOLD,
        effective_outcome_threshold: float = EFFECTIVE_OUTCOME_THRESHOLD,
        effective_throughput_max: int = EFFECTIVE_THROUGHPUT_MAX,
    ) -> None:
        if good_day_threshold <= bad_day_threshold:
            raise ValueError(
                "good_day_threshold must be strictly greater than "
                f"bad_day_threshold; got good={good_day_threshold!r} "
                f"bad={bad_day_threshold!r}"
            )
        if effective_throughput_max < 0:
            raise ValueError(
                "effective_throughput_max must be non-negative; "
                f"got {effective_throughput_max!r}"
            )
        self._events_repo = events_repo
        self._outcome_aggregator = outcome_aggregator
        self._good_day_threshold = float(good_day_threshold)
        self._bad_day_threshold = float(bad_day_threshold)
        self._effective_outcome_threshold = float(effective_outcome_threshold)
        self._effective_throughput_max = int(effective_throughput_max)

    # ------------------------------------------------------------------
    # Workday lens
    # ------------------------------------------------------------------

    def cluster_workday_variants(
        self,
        analysis: VariantAnalysis,
        *,
        since: str | None = None,
        until: str | None = None,
        threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
    ) -> VariantClustering:
        """Label each variant in ``analysis`` for the workday lens.

        ``Variant.case_ids`` are interpreted as ``case_date`` strings.  Each
        is looked up via :meth:`OutcomeAggregator.for_case_date`.  Labels are
        one of ``good_day`` / ``mixed`` / ``bad_day`` per module docstring.

        ``since`` / ``until`` are recorded in ``extractor_metadata`` for
        traceability but do NOT filter case_ids — that filtering already
        happened upstream in
        :meth:`ProcessMiner.analyze_variants`.
        """
        metadata: dict[str, Any] = {
            "lens": "workday",
            "since": since,
            "until": until,
            "good_day_threshold": self._good_day_threshold,
            "bad_day_threshold": self._bad_day_threshold,
            "task_completion_threshold": float(threshold),
        }

        if not analysis.variants:
            return VariantClustering(
                labeled_variants=(),
                summary={},
                total_cases=0,
                cases_with_outcome=0,
                extractor_metadata=metadata,
            )

        labeled: list[LabeledVariant] = []
        total_cases = 0
        total_with_outcome = 0
        for variant in analysis.variants:
            total_cases += variant.case_count
            mean_outcome, n_with_outcome = self._mean_outcome_for_case_dates(
                variant.case_ids, threshold=threshold
            )
            label = self._workday_label(mean_outcome)
            labeled.append(
                LabeledVariant(
                    variant=variant,
                    mean_outcome_score=mean_outcome,
                    case_count_with_outcome=n_with_outcome,
                    label=label,
                )
            )
            total_with_outcome += n_with_outcome

        summary = _summarize_labels(labeled)
        return VariantClustering(
            labeled_variants=tuple(labeled),
            summary=summary,
            total_cases=total_cases,
            cases_with_outcome=total_with_outcome,
            extractor_metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Goal-pursuit lens
    # ------------------------------------------------------------------

    def cluster_goal_pursuit_variants(
        self,
        analysis: VariantAnalysis,
        *,
        goals_repo: GoalsRepository | None = None,
        threshold: float = DEFAULT_TASK_COMPLETION_THRESHOLD,
    ) -> VariantClustering:
        """Label each variant in ``analysis`` for the goal-pursuit lens.

        ``Variant.case_ids`` are interpreted as ``goal_id`` strings.  Each is
        looked up via :meth:`OutcomeAggregator.for_case_goal_id`.  Labels are
        one of ``effective_pursuit`` / ``stuck`` per module docstring.

        ``goals_repo`` is accepted for forward-compat (so callers can layer
        in goal-status filtering or richer metadata in a future task) but is
        currently unused beyond a note in ``extractor_metadata``.
        """
        metadata: dict[str, Any] = {
            "lens": "goal_pursuit",
            "effective_outcome_threshold": self._effective_outcome_threshold,
            "effective_throughput_max": self._effective_throughput_max,
            "task_completion_threshold": float(threshold),
            "goals_repo_available": goals_repo is not None,
        }

        if not analysis.variants:
            return VariantClustering(
                labeled_variants=(),
                summary={},
                total_cases=0,
                cases_with_outcome=0,
                extractor_metadata=metadata,
            )

        labeled: list[LabeledVariant] = []
        total_cases = 0
        total_with_outcome = 0
        for variant in analysis.variants:
            total_cases += variant.case_count
            mean_outcome, n_with_outcome = self._mean_outcome_for_case_goal_ids(
                variant.case_ids, threshold=threshold
            )
            label = self._goal_pursuit_label(mean_outcome, variant.case_count)
            labeled.append(
                LabeledVariant(
                    variant=variant,
                    mean_outcome_score=mean_outcome,
                    case_count_with_outcome=n_with_outcome,
                    label=label,
                )
            )
            total_with_outcome += n_with_outcome

        summary = _summarize_labels(labeled)
        return VariantClustering(
            labeled_variants=tuple(labeled),
            summary=summary,
            total_cases=total_cases,
            cases_with_outcome=total_with_outcome,
            extractor_metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _mean_outcome_for_case_dates(
        self,
        case_ids: tuple[str, ...],
        *,
        threshold: float,
    ) -> tuple[float, int]:
        """Return ``(mean_outcome, count_of_cases_resolved)`` for workday lens.

        Cases whose ``case_date`` is empty (would raise from the aggregator)
        are silently skipped.  Cases with no events at all return an outcome
        of 0 from :meth:`OutcomeAggregator.for_case_date` — those still count
        toward the mean (variant produced no-outcome days, that IS the
        signal).
        """
        if not case_ids:
            return 0.0, 0
        scores: list[int] = []
        for cid in case_ids:
            if not cid:
                # Defensive: the empty-string sentinel for case_date should
                # never appear in a Variant emitted by ProcessMiner, but
                # OutcomeAggregator.for_case_date raises on it — skip rather
                # than break the cluster pass.
                continue
            try:
                breakdown: OutcomeBreakdown = self._outcome_aggregator.for_case_date(
                    cid, threshold=threshold
                )
            except ValueError:
                continue
            scores.append(int(breakdown.outcome_score))
        if not scores:
            return 0.0, 0
        return float(sum(scores)) / float(len(scores)), len(scores)

    def _mean_outcome_for_case_goal_ids(
        self,
        case_ids: tuple[str, ...],
        *,
        threshold: float,
    ) -> tuple[float, int]:
        """Return ``(mean_outcome, count_of_cases_resolved)`` for goal lens."""
        if not case_ids:
            return 0.0, 0
        scores: list[int] = []
        for cid in case_ids:
            if not cid:
                continue
            try:
                breakdown = self._outcome_aggregator.for_case_goal_id(
                    cid, threshold=threshold
                )
            except ValueError:
                continue
            scores.append(int(breakdown.outcome_score))
        if not scores:
            return 0.0, 0
        return float(sum(scores)) / float(len(scores)), len(scores)

    def _workday_label(self, mean_outcome: float) -> ClusterLabel:
        if mean_outcome >= self._good_day_threshold:
            return "good_day"
        if mean_outcome < self._bad_day_threshold:
            return "bad_day"
        return "mixed"

    def _goal_pursuit_label(self, mean_outcome: float, case_count: int) -> ClusterLabel:
        if (
            mean_outcome >= self._effective_outcome_threshold
            and case_count <= self._effective_throughput_max
        ):
            return "effective_pursuit"
        return "stuck"


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _summarize_labels(labeled: list[LabeledVariant]) -> dict[str, int]:
    """Return ``{label: count}`` for the given labeled variants."""
    out: dict[str, int] = {}
    for lv in labeled:
        out[lv.label] = out.get(lv.label, 0) + 1
    return out
