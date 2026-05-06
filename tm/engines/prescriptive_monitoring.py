"""Prescriptive Process Monitoring (PrPM) guardrails for the scheduler agent.

Per the locked v1 plan, a proposed scheduler suggestion must clear three
independent guardrails before the agent surfaces it to the user. Each guard is
a *pure evaluator* — given a :class:`CandidateSuggestion`, it returns a
:class:`GuardrailVerdict` describing pass/fail plus a human-readable rationale.
The :class:`Guardrails` composite runs all three guards (no short-circuit) so
the aggregated rationale captures every reason a candidate was rejected, even
when more than one guard fails.

The three guards
----------------

1. :class:`ObjectiveFunctionGuard` — passes iff
   ``predicted_outcome_with - predicted_outcome_without > 0``. Strict ``>``: a
   suggestion that doesn't shift the predicted outcome at all is not worth
   surfacing.

2. :class:`CounterfactualGuard` — passes iff
   ``predicted_outcome_with - predicted_outcome_without >= threshold``. The
   threshold is tunable (default :data:`DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD`
   = ``0.3``); inclusive at the threshold so a suggestion that exactly meets
   the bar still passes. Note that any candidate passing this guard also
   passes :class:`ObjectiveFunctionGuard` (CG is strictly stronger), but both
   still run so the rationale is explicit.

3. :class:`ConformanceDeviationGuard` — passes iff
   ``predicted_post_suggestion_fitness is None`` OR
   ``predicted_post_suggestion_fitness >= floor``. The floor is tunable
   (default :data:`DEFAULT_CONFORMANCE_FITNESS_FLOOR` = ``0.4``); inclusive at
   the floor. When the LLM is unable to predict a fitness number it returns
   ``None`` and the guard is a documented no-op pass.

All guards are pure; they raise on shape errors only (e.g. unexpected NaN),
otherwise they always return a :class:`GuardrailVerdict`. The composite never
raises.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

__all__ = [
    "DEFAULT_CONFORMANCE_FITNESS_FLOOR",
    "DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD",
    "CandidateSuggestion",
    "ConformanceDeviationGuard",
    "CounterfactualGuard",
    "GuardrailVerdict",
    "Guardrails",
    "GuardrailsEvaluation",
    "ObjectiveFunctionGuard",
]


# ---------------------------------------------------------------------------
# Public defaults — exposed so callers / tests can introspect the tunables.
# ---------------------------------------------------------------------------

#: Default minimum ``predicted_outcome_with - predicted_outcome_without`` for
#: :class:`CounterfactualGuard` to pass. Inclusive lower bound (``>=``).
DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD: float = 0.3

#: Default minimum ``predicted_post_suggestion_fitness`` for
#: :class:`ConformanceDeviationGuard` to pass. Inclusive lower bound (``>=``).
DEFAULT_CONFORMANCE_FITNESS_FLOOR: float = 0.4


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class CandidateSuggestion:
    """Pre-guardrail candidate produced by the scheduler's LLM call.

    Attributes
    ----------
    recommended_action:
        Free-text action recommendation (e.g. ``"close email and start a 90
        minute deep-work block on the payments service refactor"``).  Must be
        non-empty; the scheduler agent is responsible for pre-validating that.
    predicted_outcome_with:
        Predicted ``outcome_score`` (in ``[0, 2]``) if the user follows the
        recommendation. Aligns with :class:`tm.models.outcome.OutcomeBreakdown`.
    predicted_outcome_without:
        Predicted ``outcome_score`` (in ``[0, 2]``) if the user *doesn't*
        follow the recommendation (counterfactual baseline).
    predicted_post_suggestion_fitness:
        Predicted token-replay fitness (in ``[0, 1]``) of the resulting trace
        against the discovered process model, if the LLM was able to estimate
        it.  ``None`` is acceptable and is treated as a no-op pass by
        :class:`ConformanceDeviationGuard`.
    explanation:
        LLM-generated reason for the recommendation; logged in telemetry.
    """

    recommended_action: str
    predicted_outcome_with: float
    predicted_outcome_without: float
    predicted_post_suggestion_fitness: float | None
    explanation: str


@dataclass(frozen=True, slots=True)
class GuardrailVerdict:
    """Single-guard result.

    Attributes
    ----------
    guard_name:
        Stable, machine-readable name of the guard (e.g.
        ``"objective_function"``). Used for log filtering and tests.
    passed:
        ``True`` iff the guard's condition was satisfied.
    rationale:
        Human-readable explanation of why the guard passed or failed.
    """

    guard_name: str
    passed: bool
    rationale: str


@dataclass(frozen=True, slots=True)
class GuardrailsEvaluation:
    """Aggregate of all guards run on one candidate.

    Attributes
    ----------
    accept:
        ``True`` iff every guard in :attr:`verdicts` has ``passed=True``.
    verdicts:
        One verdict per guard, in the canonical order:
        objective → counterfactual → conformance.
    predicted_outcome_delta:
        ``predicted_outcome_with - predicted_outcome_without``. Echoed as a
        diagnostic so callers don't have to recompute it.
    """

    accept: bool
    verdicts: tuple[GuardrailVerdict, ...]
    predicted_outcome_delta: float


# ---------------------------------------------------------------------------
# Individual guards
# ---------------------------------------------------------------------------


_OBJECTIVE_FUNCTION_GUARD_NAME = "objective_function"
_COUNTERFACTUAL_GUARD_NAME = "counterfactual"
_CONFORMANCE_GUARD_NAME = "conformance_deviation"


def _delta(candidate: CandidateSuggestion) -> float:
    """Return ``predicted_outcome_with - predicted_outcome_without``.

    Defensive against ``NaN`` propagation: if either side is NaN the resulting
    delta is also NaN, which fails both ``>`` and ``>=`` comparisons in the
    guards below — the candidate is rejected with a clear rationale.
    """
    return float(candidate.predicted_outcome_with) - float(
        candidate.predicted_outcome_without
    )


class ObjectiveFunctionGuard:
    """Reject candidates whose predicted-outcome delta is not strictly positive.

    A scheduler that surfaces zero-delta or negative-delta suggestions is just
    adding noise to the user's day. The guard requires
    ``predicted_outcome_with > predicted_outcome_without`` (strict ``>``).
    """

    name = _OBJECTIVE_FUNCTION_GUARD_NAME

    def evaluate(self, candidate: CandidateSuggestion) -> GuardrailVerdict:
        delta = _delta(candidate)
        if math.isnan(delta):
            return GuardrailVerdict(
                guard_name=self.name,
                passed=False,
                rationale=(
                    "predicted_outcome_delta is NaN "
                    f"(with={candidate.predicted_outcome_with!r}, "
                    f"without={candidate.predicted_outcome_without!r})"
                ),
            )
        if delta > 0:
            return GuardrailVerdict(
                guard_name=self.name,
                passed=True,
                rationale=f"predicted_outcome_delta={delta:.4f} > 0",
            )
        return GuardrailVerdict(
            guard_name=self.name,
            passed=False,
            rationale=(
                f"predicted_outcome_delta={delta:.4f} not strictly positive; "
                "suggestion does not improve predicted outcome"
            ),
        )


class CounterfactualGuard:
    """Reject candidates that don't beat the counterfactual baseline by enough.

    Requires ``predicted_outcome_with - predicted_outcome_without >= threshold``
    (inclusive at the threshold so a candidate exactly hitting the bar still
    passes). Default threshold is :data:`DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD`.
    """

    name = _COUNTERFACTUAL_GUARD_NAME

    def __init__(
        self,
        *,
        threshold: float = DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD,
    ) -> None:
        self._threshold = float(threshold)

    @property
    def threshold(self) -> float:
        return self._threshold

    def evaluate(self, candidate: CandidateSuggestion) -> GuardrailVerdict:
        delta = _delta(candidate)
        if math.isnan(delta):
            return GuardrailVerdict(
                guard_name=self.name,
                passed=False,
                rationale=(
                    "predicted_outcome_delta is NaN "
                    f"(threshold={self._threshold:.4f})"
                ),
            )
        if delta >= self._threshold:
            return GuardrailVerdict(
                guard_name=self.name,
                passed=True,
                rationale=(
                    f"predicted_outcome_delta={delta:.4f} >= "
                    f"threshold={self._threshold:.4f}"
                ),
            )
        return GuardrailVerdict(
            guard_name=self.name,
            passed=False,
            rationale=(
                f"predicted_outcome_delta={delta:.4f} below "
                f"threshold={self._threshold:.4f}"
            ),
        )


class ConformanceDeviationGuard:
    """Reject candidates whose predicted post-suggestion fitness is too low.

    Operates on :attr:`CandidateSuggestion.predicted_post_suggestion_fitness`:

    * ``None`` is documented no-op pass — the LLM declined to predict.
    * Otherwise pass iff ``fitness >= floor`` (inclusive at the floor).

    Default floor is :data:`DEFAULT_CONFORMANCE_FITNESS_FLOOR`.
    """

    name = _CONFORMANCE_GUARD_NAME

    def __init__(
        self,
        *,
        floor: float = DEFAULT_CONFORMANCE_FITNESS_FLOOR,
    ) -> None:
        self._floor = float(floor)

    @property
    def floor(self) -> float:
        return self._floor

    def evaluate(self, candidate: CandidateSuggestion) -> GuardrailVerdict:
        fitness = candidate.predicted_post_suggestion_fitness
        if fitness is None:
            return GuardrailVerdict(
                guard_name=self.name,
                passed=True,
                rationale="conformance fitness not provided",
            )
        try:
            fitness_f = float(fitness)
        except (TypeError, ValueError):
            return GuardrailVerdict(
                guard_name=self.name,
                passed=False,
                rationale=(
                    f"predicted_post_suggestion_fitness not numeric: {fitness!r}"
                ),
            )
        if math.isnan(fitness_f):
            return GuardrailVerdict(
                guard_name=self.name,
                passed=False,
                rationale="predicted_post_suggestion_fitness is NaN",
            )
        if fitness_f >= self._floor:
            return GuardrailVerdict(
                guard_name=self.name,
                passed=True,
                rationale=(
                    f"predicted_post_suggestion_fitness={fitness_f:.4f} >= "
                    f"floor={self._floor:.4f}"
                ),
            )
        return GuardrailVerdict(
            guard_name=self.name,
            passed=False,
            rationale=(
                f"predicted_post_suggestion_fitness={fitness_f:.4f} below "
                f"floor={self._floor:.4f}"
            ),
        )


# ---------------------------------------------------------------------------
# Composite
# ---------------------------------------------------------------------------


class Guardrails:
    """Composite runner: evaluates all three guards on a candidate.

    All three guards run unconditionally — there is no short-circuit. This
    means the caller gets a complete rationale (every reason the candidate
    was rejected, not just the first), which is critical for telemetry
    debugging and for surfacing meaningful feedback to the user.

    Parameters
    ----------
    counterfactual_delta_threshold:
        Forwarded to :class:`CounterfactualGuard`. Defaults to
        :data:`DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD`.
    conformance_fitness_floor:
        Forwarded to :class:`ConformanceDeviationGuard`. Defaults to
        :data:`DEFAULT_CONFORMANCE_FITNESS_FLOOR`.
    """

    def __init__(
        self,
        *,
        counterfactual_delta_threshold: float = DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD,
        conformance_fitness_floor: float = DEFAULT_CONFORMANCE_FITNESS_FLOOR,
    ) -> None:
        self._objective = ObjectiveFunctionGuard()
        self._counterfactual = CounterfactualGuard(
            threshold=counterfactual_delta_threshold
        )
        self._conformance = ConformanceDeviationGuard(
            floor=conformance_fitness_floor
        )

    @property
    def counterfactual_delta_threshold(self) -> float:
        return self._counterfactual.threshold

    @property
    def conformance_fitness_floor(self) -> float:
        return self._conformance.floor

    def evaluate(self, candidate: CandidateSuggestion) -> GuardrailsEvaluation:
        """Run all three guards in canonical order and aggregate the result.

        The order is:

        1. :class:`ObjectiveFunctionGuard`
        2. :class:`CounterfactualGuard`
        3. :class:`ConformanceDeviationGuard`

        Every guard runs even if an earlier one failed, so
        :attr:`GuardrailsEvaluation.verdicts` always has length 3 and the
        rationale captures every reason a candidate was rejected.
        """
        verdicts = (
            self._objective.evaluate(candidate),
            self._counterfactual.evaluate(candidate),
            self._conformance.evaluate(candidate),
        )
        accept = all(v.passed for v in verdicts)
        delta = _delta(candidate)
        return GuardrailsEvaluation(
            accept=accept,
            verdicts=verdicts,
            predicted_outcome_delta=delta,
        )
