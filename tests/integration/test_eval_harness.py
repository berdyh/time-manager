"""Tests for :mod:`tests.integration.eval_harness`.

Exercises the v1 placeholder eval harness against the in-tree fixtures.
The harness mocks the LLM with the expected output, so field/trace
accuracies should be 1.0 in a healthy build; the thresholds below are the
locked plan minimums (0.85 / 0.70 / 0.75).

NO live LLM calls.  NO live network.
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from tests.integration.eval_harness import (
    FIXTURES_DIR,
    LIVE_THRESHOLD_FIELD_LEVEL,
    LIVE_THRESHOLD_TRACE_LEVEL,
    LIVE_THRESHOLD_VARIANT_ASSIGNMENT,
    PLAN_THRESHOLD_FIELD_LEVEL,
    PLAN_THRESHOLD_TRACE_LEVEL,
    PLAN_THRESHOLD_VARIANT_ASSIGNMENT,
    EvalResult,
    load_fixtures,
    run_eval,
)

# ---------------------------------------------------------------------------
# Plan-threshold constants
# ---------------------------------------------------------------------------


def test_plan_thresholds_are_locked_v1_values() -> None:
    assert PLAN_THRESHOLD_FIELD_LEVEL == 0.85
    assert PLAN_THRESHOLD_TRACE_LEVEL == 0.70
    assert PLAN_THRESHOLD_VARIANT_ASSIGNMENT == 0.75


def test_live_thresholds_are_separate_from_mock_gates() -> None:
    assert LIVE_THRESHOLD_FIELD_LEVEL == 0.60
    assert LIVE_THRESHOLD_TRACE_LEVEL == 0.20
    assert LIVE_THRESHOLD_VARIANT_ASSIGNMENT == 0.50
    assert LIVE_THRESHOLD_FIELD_LEVEL < PLAN_THRESHOLD_FIELD_LEVEL


def test_eval_result_is_frozen() -> None:
    res = EvalResult(
        fixture_count=0,
        field_level_accuracy=0.0,
        trace_level_accuracy=0.0,
        variant_assignment_accuracy=0.0,
        field_level_passed=False,
        trace_level_passed=False,
        variant_assignment_passed=False,
        fixture_names=(),
    )
    with pytest.raises(FrozenInstanceError):
        res.fixture_count = 99  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Fixture discovery
# ---------------------------------------------------------------------------


def test_eval_loads_all_fixtures() -> None:
    """Every transcript file under fixtures/ has a matching expected.json."""
    fixtures = load_fixtures()
    # Plan minimum: 5 fixtures.
    assert len(fixtures) >= 5
    for name, transcript, expected in fixtures:
        assert isinstance(name, str) and name
        assert isinstance(transcript, str) and transcript.strip()
        assert isinstance(expected, dict)
        assert "extract" in expected
        assert "expected_variant_label" in expected
        assert "expected_outcome_score" in expected


def test_load_fixtures_returns_deterministic_order() -> None:
    a = [n for n, _, _ in load_fixtures()]
    b = [n for n, _, _ in load_fixtures()]
    assert a == b
    assert a == sorted(a)


def test_eval_handles_missing_fixture_gracefully(tmp_path: Path) -> None:
    """Transcript without paired expected.json -> silent skip."""
    (tmp_path / "orphan_transcript.txt").write_text("no expected file present")
    fixtures = load_fixtures(tmp_path)
    assert fixtures == []


def test_eval_handles_malformed_expected_gracefully(tmp_path: Path) -> None:
    """Unparseable expected.json -> silent skip."""
    (tmp_path / "bad_transcript.txt").write_text("hi")
    (tmp_path / "bad_expected.json").write_text("{not valid json")
    fixtures = load_fixtures(tmp_path)
    assert fixtures == []


def test_eval_handles_nonexistent_dir() -> None:
    fixtures = load_fixtures(Path("/nonexistent/path/that/does/not/exist"))
    assert fixtures == []


def test_eval_handles_non_dict_expected(tmp_path: Path) -> None:
    """Expected JSON that isn't a dict -> skip."""
    (tmp_path / "list_transcript.txt").write_text("hello")
    (tmp_path / "list_expected.json").write_text("[1, 2, 3]")
    fixtures = load_fixtures(tmp_path)
    assert fixtures == []


# ---------------------------------------------------------------------------
# Threshold gates — the meat of AC5
# ---------------------------------------------------------------------------


def test_eval_meets_field_level_threshold() -> None:
    result = run_eval()
    assert result.fixture_count >= 5
    assert result.field_level_accuracy >= PLAN_THRESHOLD_FIELD_LEVEL, (
        f"field-level accuracy {result.field_level_accuracy} below "
        f"plan threshold {PLAN_THRESHOLD_FIELD_LEVEL}"
    )
    assert result.field_level_passed is True


def test_eval_meets_trace_level_threshold() -> None:
    result = run_eval()
    assert result.trace_level_accuracy >= PLAN_THRESHOLD_TRACE_LEVEL
    assert result.trace_level_passed is True


def test_eval_meets_variant_assignment_threshold() -> None:
    result = run_eval()
    assert result.variant_assignment_accuracy >= PLAN_THRESHOLD_VARIANT_ASSIGNMENT
    assert result.variant_assignment_passed is True


def test_eval_all_thresholds_pass_simultaneously() -> None:
    """The ACs require all 3 thresholds to clear in a single run."""
    result = run_eval()
    assert result.field_level_passed
    assert result.trace_level_passed
    assert result.variant_assignment_passed


# ---------------------------------------------------------------------------
# Behaviour under custom thresholds
# ---------------------------------------------------------------------------


def test_eval_with_zero_thresholds_always_passes() -> None:
    """Sanity check: custom thresholds at 0.0 should never fail."""
    result = run_eval(
        field_threshold=0.0,
        trace_threshold=0.0,
        variant_threshold=0.0,
    )
    assert result.field_level_passed
    assert result.trace_level_passed
    assert result.variant_assignment_passed


def test_eval_with_impossibly_high_thresholds_can_fail() -> None:
    """A 1.01 threshold is unreachable so the gates flip to False."""
    result = run_eval(
        field_threshold=1.01,
        trace_threshold=1.01,
        variant_threshold=1.01,
    )
    # Accuracies are still computed; only the *passed* flags flip.
    assert result.field_level_passed is False
    assert result.trace_level_passed is False
    assert result.variant_assignment_passed is False


def test_eval_against_empty_fixtures_dir_returns_zeroed_result(tmp_path: Path) -> None:
    """Empty fixtures dir -> all-zero accuracies, all gates fail."""
    result = run_eval(tmp_path)
    assert result.fixture_count == 0
    assert result.field_level_accuracy == 0.0
    assert result.trace_level_accuracy == 0.0
    assert result.variant_assignment_accuracy == 0.0
    assert result.field_level_passed is False
    assert result.trace_level_passed is False
    assert result.variant_assignment_passed is False


def test_eval_fixture_names_come_back_sorted() -> None:
    result = run_eval()
    assert result.fixture_names == tuple(sorted(result.fixture_names))


# ---------------------------------------------------------------------------
# Fixture-level sanity (catches plan-threshold drift before run_eval is
# invoked)
# ---------------------------------------------------------------------------


def test_each_fixture_has_required_keys() -> None:
    for transcript_path in FIXTURES_DIR.glob("*_transcript.txt"):
        name = transcript_path.name[: -len("_transcript.txt")]
        expected_path = FIXTURES_DIR / f"{name}_expected.json"
        assert expected_path.is_file(), f"missing pair for fixture {name!r}"
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        assert "case_date" in expected, f"{name}: case_date missing"
        assert "expected_variant_label" in expected, (
            f"{name}: expected_variant_label missing"
        )
        assert expected["expected_variant_label"] in {
            "good_day",
            "mixed",
            "bad_day",
        }, f"{name}: invalid variant label"
        assert "expected_outcome_score" in expected, (
            f"{name}: expected_outcome_score missing"
        )
        assert expected["expected_outcome_score"] in {0, 1, 2}, (
            f"{name}: outcome_score must be 0/1/2"
        )
        assert "extract" in expected, f"{name}: extract missing"
        extract = expected["extract"]
        assert "events" in extract and isinstance(extract["events"], list)
        assert "summary" in extract and isinstance(extract["summary"], dict)


def test_fixture_count_meets_plan_minimum() -> None:
    """Plan locks the v1 minimum at 5 fixtures."""
    pairs = list(FIXTURES_DIR.glob("*_transcript.txt"))
    assert len(pairs) >= 5


# ---------------------------------------------------------------------------
# Live LLM mode
# ---------------------------------------------------------------------------


def test_run_eval_mock_mode_baseline() -> None:
    result = run_eval(live_llm=False)

    assert result.fixture_count >= 5
    assert result.field_level_accuracy >= PLAN_THRESHOLD_FIELD_LEVEL
    assert result.trace_level_accuracy >= PLAN_THRESHOLD_TRACE_LEVEL
    assert result.variant_assignment_accuracy >= PLAN_THRESHOLD_VARIANT_ASSIGNMENT
    assert result.field_level_passed is True
    assert result.trace_level_passed is True
    assert result.variant_assignment_passed is True


@pytest.mark.skipif(
    not __import__("os").environ.get("TM_LLM_API_KEY"),
    reason="TM_LLM_API_KEY not set; live LLM smoke would spend API tokens",
)
def test_run_eval_live_mode_smoke(tmp_path: Path) -> None:
    source_transcript = FIXTURES_DIR / "empty_summary_transcript.txt"
    source_expected = FIXTURES_DIR / "empty_summary_expected.json"
    (tmp_path / source_transcript.name).write_text(
        source_transcript.read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    (tmp_path / source_expected.name).write_text(
        source_expected.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    result = run_eval(tmp_path, live_llm=True)

    assert result.fixture_count == 1
    assert result.fixture_names == ("empty_summary",)
    assert 0.0 <= result.field_level_accuracy <= 1.0
    assert 0.0 <= result.trace_level_accuracy <= 1.0
    assert 0.0 <= result.variant_assignment_accuracy <= 1.0


def test_live_mode_skips_without_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("TM_LLM_API_KEY", raising=False)

    with pytest.raises(
        RuntimeError,
        match="live_llm=True requires TM_LLM_API_KEY",
    ):
        run_eval(live_llm=True)
