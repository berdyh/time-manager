"""Tests for tm.engines.prescriptive_monitoring (T-INT-02).

Pure-function tests: no I/O, no fixtures. Each guard is exercised at and
around its boundary, plus the composite Guardrails runner is checked for
the no-short-circuit invariant that the locked plan calls out.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from tm.engines.prescriptive_monitoring import (
    DEFAULT_CONFORMANCE_FITNESS_FLOOR,
    DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD,
    CandidateSuggestion,
    ConformanceDeviationGuard,
    CounterfactualGuard,
    Guardrails,
    GuardrailsEvaluation,
    GuardrailVerdict,
    ObjectiveFunctionGuard,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _candidate(
    *,
    with_score: float = 1.5,
    without_score: float = 1.0,
    fitness: float | None = 0.6,
    action: str = "do the thing",
    explanation: str = "because reasons",
) -> CandidateSuggestion:
    return CandidateSuggestion(
        recommended_action=action,
        predicted_outcome_with=with_score,
        predicted_outcome_without=without_score,
        predicted_post_suggestion_fitness=fitness,
        explanation=explanation,
    )


# ---------------------------------------------------------------------------
# Module-level constants and dataclasses
# ---------------------------------------------------------------------------


def test_threshold_constants_exposed() -> None:
    assert DEFAULT_COUNTERFACTUAL_DELTA_THRESHOLD == 0.3
    assert DEFAULT_CONFORMANCE_FITNESS_FLOOR == 0.4


def test_candidate_suggestion_is_frozen() -> None:
    c = _candidate()
    with pytest.raises(FrozenInstanceError):
        c.recommended_action = "other"  # type: ignore[misc]


def test_guardrail_verdict_is_frozen() -> None:
    v = GuardrailVerdict(guard_name="x", passed=True, rationale="r")
    with pytest.raises(FrozenInstanceError):
        v.passed = False  # type: ignore[misc]


def test_guardrails_evaluation_is_frozen() -> None:
    e = GuardrailsEvaluation(accept=True, verdicts=(), predicted_outcome_delta=0.0)
    with pytest.raises(FrozenInstanceError):
        e.accept = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# ObjectiveFunctionGuard
# ---------------------------------------------------------------------------


def test_objective_function_guard_passes_on_positive_delta() -> None:
    guard = ObjectiveFunctionGuard()
    verdict = guard.evaluate(_candidate(with_score=1.5, without_score=1.0))
    assert verdict.guard_name == "objective_function"
    assert verdict.passed is True
    assert "0.5000" in verdict.rationale


def test_objective_function_guard_rejects_zero_delta() -> None:
    guard = ObjectiveFunctionGuard()
    verdict = guard.evaluate(_candidate(with_score=1.0, without_score=1.0))
    assert verdict.passed is False
    assert "not strictly positive" in verdict.rationale


def test_objective_function_guard_rejects_negative_delta() -> None:
    guard = ObjectiveFunctionGuard()
    verdict = guard.evaluate(_candidate(with_score=0.5, without_score=1.0))
    assert verdict.passed is False
    assert "not strictly positive" in verdict.rationale


# ---------------------------------------------------------------------------
# CounterfactualGuard
# ---------------------------------------------------------------------------


def test_counterfactual_guard_passes_at_threshold() -> None:
    # delta == 0.3 exactly with the default 0.3 threshold -> inclusive pass.
    guard = CounterfactualGuard()
    verdict = guard.evaluate(_candidate(with_score=1.3, without_score=1.0))
    assert verdict.passed is True
    assert "threshold=0.3000" in verdict.rationale


def test_counterfactual_guard_rejects_below_threshold() -> None:
    # delta == 0.29 (just below the default threshold).
    guard = CounterfactualGuard()
    verdict = guard.evaluate(_candidate(with_score=1.29, without_score=1.0))
    assert verdict.passed is False
    assert "below threshold" in verdict.rationale


def test_counterfactual_guard_passes_above_threshold() -> None:
    # delta == 0.5 (well above the default 0.3 threshold).
    guard = CounterfactualGuard()
    verdict = guard.evaluate(_candidate(with_score=1.5, without_score=1.0))
    assert verdict.passed is True


def test_counterfactual_guard_custom_threshold() -> None:
    # Custom threshold 0.5 with delta=0.4 -> reject.
    guard = CounterfactualGuard(threshold=0.5)
    verdict = guard.evaluate(_candidate(with_score=1.4, without_score=1.0))
    assert verdict.passed is False
    assert "threshold=0.5000" in verdict.rationale
    # Same threshold but delta=0.5 -> accept.
    verdict_accept = guard.evaluate(_candidate(with_score=1.5, without_score=1.0))
    assert verdict_accept.passed is True


# ---------------------------------------------------------------------------
# ConformanceDeviationGuard
# ---------------------------------------------------------------------------


def test_conformance_guard_noop_when_fitness_is_None() -> None:
    guard = ConformanceDeviationGuard()
    verdict = guard.evaluate(_candidate(fitness=None))
    assert verdict.passed is True
    assert "not provided" in verdict.rationale.lower()


def test_conformance_guard_passes_at_floor() -> None:
    # fitness == 0.4 with the default 0.4 floor -> inclusive pass.
    guard = ConformanceDeviationGuard()
    verdict = guard.evaluate(_candidate(fitness=0.4))
    assert verdict.passed is True


def test_conformance_guard_rejects_below_floor() -> None:
    # fitness == 0.39 (just below the default 0.4 floor).
    guard = ConformanceDeviationGuard()
    verdict = guard.evaluate(_candidate(fitness=0.39))
    assert verdict.passed is False
    assert "below floor" in verdict.rationale


def test_conformance_guard_custom_floor() -> None:
    guard = ConformanceDeviationGuard(floor=0.7)
    assert guard.evaluate(_candidate(fitness=0.7)).passed is True
    assert guard.evaluate(_candidate(fitness=0.69)).passed is False


# ---------------------------------------------------------------------------
# Guardrails composite
# ---------------------------------------------------------------------------


def test_guardrails_aggregate_accept_when_all_pass() -> None:
    g = Guardrails()
    evaluation = g.evaluate(_candidate(with_score=1.5, without_score=1.0, fitness=0.7))
    assert evaluation.accept is True
    assert all(v.passed for v in evaluation.verdicts)
    assert len(evaluation.verdicts) == 3


def test_guardrails_aggregate_reject_when_any_fail() -> None:
    g = Guardrails()
    # Counterfactual delta below threshold -> objective passes, counterfactual
    # fails, conformance passes; aggregate must be reject.
    evaluation = g.evaluate(_candidate(with_score=1.1, without_score=1.0, fitness=0.7))
    assert evaluation.accept is False
    names_failed = {v.guard_name for v in evaluation.verdicts if not v.passed}
    assert names_failed == {"counterfactual"}


def test_guardrails_runs_all_guards_no_short_circuit() -> None:
    g = Guardrails()
    # Zero delta + low fitness: objective fails, counterfactual fails (delta
    # 0 < 0.3), conformance fails (fitness 0.1 < 0.4). All three verdicts
    # should be present and all should be failed.
    evaluation = g.evaluate(_candidate(with_score=1.0, without_score=1.0, fitness=0.1))
    assert evaluation.accept is False
    assert len(evaluation.verdicts) == 3
    failed_names = [v.guard_name for v in evaluation.verdicts if not v.passed]
    assert failed_names == [
        "objective_function",
        "counterfactual",
        "conformance_deviation",
    ]
    # Every rationale string should be informative — none should be empty.
    for v in evaluation.verdicts:
        assert v.rationale.strip()


def test_predicted_outcome_delta_in_evaluation() -> None:
    g = Guardrails()
    evaluation = g.evaluate(_candidate(with_score=1.7, without_score=0.9, fitness=0.8))
    # 1.7 - 0.9 = 0.8 (within float tolerance)
    assert evaluation.predicted_outcome_delta == pytest.approx(0.8)


def test_guardrails_canonical_verdict_order() -> None:
    g = Guardrails()
    evaluation = g.evaluate(_candidate())
    names = [v.guard_name for v in evaluation.verdicts]
    assert names == [
        "objective_function",
        "counterfactual",
        "conformance_deviation",
    ]


def test_guardrails_threshold_properties_exposed() -> None:
    g = Guardrails(
        counterfactual_delta_threshold=0.42,
        conformance_fitness_floor=0.55,
    )
    assert g.counterfactual_delta_threshold == pytest.approx(0.42)
    assert g.conformance_fitness_floor == pytest.approx(0.55)
