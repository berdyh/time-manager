"""Minimal v1 eval harness for the DebriefAgent extract pipeline.

T-INT-03 ships an in-tree placeholder for the planned ``time-manager-evals``
companion repo: a small, dependency-free harness that loads
``(transcript, expected)`` fixture pairs from
``tests/integration/fixtures/``, runs the :class:`DebriefAgent` against
each (with the LLM mocked by default, or live when explicitly requested),
and computes:

* **field-level accuracy** — fraction of (event_idx, key) pairs in the
  expected dict that exactly match what the agent persisted.
* **trace-level accuracy** — fraction of fixtures where the *entire*
  expected event list plus summary attributes round-trip without any
  field-level mismatch.
* **variant-assignment accuracy** — fraction of fixtures where the cluster
  label implied by the persisted outcome (``good_day`` /
  ``mixed`` / ``bad_day``) matches the fixture's
  ``expected_variant_label``.

Mock-mode thresholds (locked v1, per the parent task packet):

* field-level >= 0.85
* trace-level >= 0.70
* variant-assignment >= 0.75

Because the LLM is mocked to return the *exact* expected dict, field- and
trace-level should naturally land at 1.0 in v1; the harness exists as a
regression contract so any future agent change that mangles persistence
(timestamp normalization, attribute drops, etc.) trips the threshold.
Live mode uses a separate calibrated gate. The live fixture set is still tiny,
so the thresholds are intentionally lower than mock mode and should be raised
after the companion eval repo has 30+ real examples.

By default there are NO live LLM calls and NO live network. Passing
``live_llm=True`` wires real :class:`AnthropicAdapter` instances for both
the debrief extractor and vocabulary aligner; this requires
``TM_LLM_API_KEY`` and may spend API tokens. Every fixture is exercised
against a fresh ``tmp_path`` SQLite database with the full migration chain
applied.

Public surface:

* :data:`PLAN_THRESHOLD_FIELD_LEVEL`
* :data:`PLAN_THRESHOLD_TRACE_LEVEL`
* :data:`PLAN_THRESHOLD_VARIANT_ASSIGNMENT`
* :class:`EvalResult`
* :func:`load_fixtures`
* :func:`run_eval`

The harness lives under ``tests/`` (not ``tm/``) so it is NOT shipped as
production runtime code; it is exercised exclusively from
``tests/integration/test_eval_harness.py``.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import Mock

from tm.agents import DebriefAgent
from tm.engines.variant_cluster import (
    BAD_DAY_THRESHOLD,
    GOOD_DAY_THRESHOLD,
)
from tm.llm.anthropic_adapter import AnthropicAdapter
from tm.llm.client import ExtractResponse, Usage
from tm.llm.cost_meter import CostMeter
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.store import Store
from tm.vocab_alignment import VocabAligner

__all__ = [
    "FIXTURES_DIR",
    "GOAL_PLACEHOLDER",
    "MIGRATIONS_DIR",
    "LIVE_THRESHOLD_FIELD_LEVEL",
    "LIVE_THRESHOLD_TRACE_LEVEL",
    "LIVE_THRESHOLD_VARIANT_ASSIGNMENT",
    "PLAN_THRESHOLD_FIELD_LEVEL",
    "PLAN_THRESHOLD_TRACE_LEVEL",
    "PLAN_THRESHOLD_VARIANT_ASSIGNMENT",
    "EvalResult",
    "load_fixtures",
    "run_eval",
]


# ---------------------------------------------------------------------------
# Plan-locked thresholds
# ---------------------------------------------------------------------------

#: Minimum fraction of (event, key) pairs that must round-trip correctly.
PLAN_THRESHOLD_FIELD_LEVEL: float = 0.85

#: Minimum fraction of fixtures whose entire trace round-trips correctly.
PLAN_THRESHOLD_TRACE_LEVEL: float = 0.70

#: Minimum fraction of fixtures whose implied variant cluster label matches.
PLAN_THRESHOLD_VARIANT_ASSIGNMENT: float = 0.75

# Live-LLM thresholds are calibrated separately from mock mode. These are
# release-smoke gates for the current tiny in-tree fixture set, not product
# quality thresholds.
LIVE_THRESHOLD_FIELD_LEVEL: float = 0.60
LIVE_THRESHOLD_TRACE_LEVEL: float = 0.20
LIVE_THRESHOLD_VARIANT_ASSIGNMENT: float = 0.50


# ---------------------------------------------------------------------------
# Filesystem locations
# ---------------------------------------------------------------------------

FIXTURES_DIR: Path = Path(__file__).resolve().parent / "fixtures"

#: Repo migrations directory; resolved relative to this file so the harness
#: works from any cwd.
MIGRATIONS_DIR: Path = Path(__file__).resolve().parents[2] / "migrations"

#: Sentinel string used inside fixture ``advances_goal_id`` slots; the harness
#: replaces it at runtime with a real ULID after seeding a goal in the test
#: database.  The agent validates ``advances_goal_id`` against the goals repo
#: before persisting an event, so fixtures that reference a goal MUST go
#: through this substitution path.
GOAL_PLACEHOLDER: str = "__GOAL_PAYMENTS__"


# ---------------------------------------------------------------------------
# EvalResult
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EvalResult:
    """Aggregated outcome of one :func:`run_eval` call.

    Frozen for safety — downstream consumers (assertions in
    ``test_eval_harness``) treat this as a value object.
    """

    fixture_count: int
    field_level_accuracy: float
    trace_level_accuracy: float
    variant_assignment_accuracy: float
    field_level_passed: bool
    trace_level_passed: bool
    variant_assignment_passed: bool
    fixture_names: tuple[str, ...]


# ---------------------------------------------------------------------------
# Fixture loader
# ---------------------------------------------------------------------------


def load_fixtures(
    fixtures_dir: Path = FIXTURES_DIR,
) -> list[tuple[str, str, dict[str, Any]]]:
    """Discover and load all ``<name>_transcript.txt`` + ``<name>_expected.json`` pairs.

    Returns a deterministic-ordered list of ``(fixture_name, transcript_text,
    expected_dict)`` triples.  Pairs whose expected JSON is unreadable are
    silently skipped — the harness logs nothing because we want
    ``run_eval`` to remain pure.  Fixtures whose transcript file exists but
    expected file does NOT are also skipped (the
    ``test_eval_handles_missing_fixture_gracefully`` test exercises this).

    Parameters
    ----------
    fixtures_dir:
        Directory containing the fixture pair files.  Defaults to the
        in-tree :data:`FIXTURES_DIR`.
    """
    out: list[tuple[str, str, dict[str, Any]]] = []
    if not fixtures_dir.is_dir():
        return out
    for transcript_path in sorted(fixtures_dir.glob("*_transcript.txt")):
        name = transcript_path.name[: -len("_transcript.txt")]
        expected_path = fixtures_dir / f"{name}_expected.json"
        if not expected_path.is_file():
            # Missing pair member -> skip silently.
            continue
        try:
            transcript = transcript_path.read_text(encoding="utf-8")
            expected = json.loads(expected_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(expected, dict):
            continue
        out.append((name, transcript, expected))
    return out


# ---------------------------------------------------------------------------
# run_eval
# ---------------------------------------------------------------------------


def run_eval(
    fixtures_dir: Path = FIXTURES_DIR,
    *,
    field_threshold: float | None = None,
    trace_threshold: float | None = None,
    variant_threshold: float | None = None,
    live_llm: bool = False,
) -> EvalResult:
    """Run the in-tree v1 eval against every fixture under ``fixtures_dir``.

    For each fixture: build a tmp SQLite DB, seed vocab + the goal placeholder,
    construct a :class:`DebriefAgent`, invoke ``extract_and_persist``, then
    read the persisted events back and compare. In default mock mode the
    agent's Mock :class:`LLMClient` has ``extract.return_value`` set to the
    fixture's ``expected.extract`` dict (with the goal placeholder swapped for
    the real ULID). In live mode, real :class:`AnthropicAdapter` instances are
    used and the fixture extract is used only as the scoring oracle.

    Each fixture gets its own ``tempfile.TemporaryDirectory`` so the runs
    are fully isolated.

    Field-level accuracy is computed as ``matched_fields / total_fields``
    summed across every fixture.  Trace-level is the count of fixtures where
    every (event_idx, key) match.  Variant-assignment compares
    ``map_outcome_to_label(persisted_outcome_score)`` to the fixture's
    ``expected_variant_label``.

    Returns
    -------
    EvalResult
        Frozen dataclass with the three accuracies + their pass flags.

    Raises
    ------
    RuntimeError
        If ``live_llm=True`` and ``TM_LLM_API_KEY`` is unset.
    """
    if live_llm and not os.environ.get("TM_LLM_API_KEY"):
        raise RuntimeError("live_llm=True requires TM_LLM_API_KEY to be set")

    if field_threshold is None:
        field_threshold = (
            LIVE_THRESHOLD_FIELD_LEVEL if live_llm else PLAN_THRESHOLD_FIELD_LEVEL
        )
    if trace_threshold is None:
        trace_threshold = (
            LIVE_THRESHOLD_TRACE_LEVEL if live_llm else PLAN_THRESHOLD_TRACE_LEVEL
        )
    if variant_threshold is None:
        variant_threshold = (
            LIVE_THRESHOLD_VARIANT_ASSIGNMENT
            if live_llm
            else PLAN_THRESHOLD_VARIANT_ASSIGNMENT
        )

    fixtures = load_fixtures(fixtures_dir)
    fixture_names = tuple(name for name, _, _ in fixtures)

    if not fixtures:
        # No fixtures means accuracy is undefined; treat as 0.0 across the
        # board so a missing-fixtures regression trips the thresholds.
        return EvalResult(
            fixture_count=0,
            field_level_accuracy=0.0,
            trace_level_accuracy=0.0,
            variant_assignment_accuracy=0.0,
            field_level_passed=False,
            trace_level_passed=False,
            variant_assignment_passed=False,
            fixture_names=(),
        )

    total_field_matches = 0
    total_field_count = 0
    correct_traces = 0
    correct_variants = 0

    for name, transcript, expected in fixtures:
        with tempfile.TemporaryDirectory(prefix=f"tm-eval-{name}-") as tmp:
            db_path = Path(tmp) / "eval.db"
            (
                events_repo,
                goals_repo,
                outcome_aggregator,
                agent,
                resolved_extract,
            ) = _build_eval_world(db_path, expected, live_llm=live_llm)

            case_date = str(expected.get("case_date") or "")
            agent_input_transcript = transcript.strip() or "n/a"

            if not live_llm:
                # Configure mock LLM with the resolved expected extract dict.
                agent._llm.extract.return_value = ExtractResponse(  # type: ignore[attr-defined]
                    data=resolved_extract,
                    usage=Usage(input_tokens=1, output_tokens=1),
                )

            try:
                agent.extract_and_persist(
                    agent_input_transcript,
                    case_date=case_date or None,
                )
            except Exception:
                # Any unexpected agent error -> fixture scores 0 across all
                # buckets; still need to advance the field-count denominator
                # so a partial-failure run doesn't get an inflated accuracy
                # ratio.  Each expected event contributes 3 required keys
                # (activity / timestamp / lifecycle) plus any optional keys
                # set in the fixture, plus 2 summary keys.
                total_field_count += _expected_field_count(resolved_extract)
                continue

            persisted_events = events_repo.query_events(case_date=case_date)
            persisted_summary = _find_summary(persisted_events)
            persisted_non_summary = [
                e for e in persisted_events if e.get("activity") != "debrief_summary"
            ]
            # Persisted non-summary events come back ordered by (timestamp,
            # event_id) — same order as the LLM input.

            # Field-level matches.
            matches, total = _score_fields(
                resolved_extract,
                persisted_non_summary,
                persisted_summary,
            )
            total_field_matches += matches
            total_field_count += total

            # Trace-level: 1.0 iff every field matched for this fixture.
            if matches == total and total > 0:
                correct_traces += 1

            # Variant-assignment: derive label from outcome and compare.
            outcome = outcome_aggregator.for_case_date(case_date)
            implied_label = _label_from_outcome(float(outcome.outcome_score))
            expected_label = str(expected.get("expected_variant_label") or "")
            if implied_label == expected_label:
                correct_variants += 1

    n = len(fixtures)
    field_accuracy = (
        float(total_field_matches) / float(total_field_count)
        if total_field_count > 0
        else 0.0
    )
    trace_accuracy = float(correct_traces) / float(n)
    variant_accuracy = float(correct_variants) / float(n)

    return EvalResult(
        fixture_count=n,
        field_level_accuracy=field_accuracy,
        trace_level_accuracy=trace_accuracy,
        variant_assignment_accuracy=variant_accuracy,
        field_level_passed=field_accuracy >= field_threshold,
        trace_level_passed=trace_accuracy >= trace_threshold,
        variant_assignment_passed=variant_accuracy >= variant_threshold,
        fixture_names=fixture_names,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_eval_world(
    db_path: Path,
    expected: dict[str, Any],
    *,
    live_llm: bool = False,
) -> tuple[
    EventsRepository,
    GoalsRepository,
    OutcomeAggregator,
    DebriefAgent,
    dict[str, Any],
]:
    """Spin up an isolated SQLite world wired for one fixture run.

    Returns (events_repo, goals_repo, outcome_aggregator, agent,
    resolved_extract). ``resolved_extract`` is a deep-copied version of
    ``expected['extract']`` with :data:`GOAL_PLACEHOLDER` substituted with a
    real goal ULID seeded into the goals repo. In live mode it is used only
    for scoring; it is never injected into the LLM.
    """
    store = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()

    vocab_repo = VocabularyRepository(db_path)
    vocab_repo.seed_starter_vocabulary()
    vocab_repo.seed_starter_aliases()

    goals_repo = GoalsRepository(db_path)
    events_repo = EventsRepository(db_path)
    cost_meter = CostMeter(db_path, monthly_cap_usd=100.0)

    # Substitute the placeholder for a real goal ULID.
    resolved_extract = _resolve_goal_placeholders(
        expected.get("extract") or {}, goals_repo
    )

    vocab_llm = AnthropicAdapter() if live_llm else Mock()
    aligner = VocabAligner(vocab_repo, vocab_llm)

    debrief_llm = AnthropicAdapter() if live_llm else Mock()
    agent = DebriefAgent(
        llm_client=debrief_llm,
        vocab_aligner=aligner,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
    )
    outcome_aggregator = OutcomeAggregator(events_repo)
    return events_repo, goals_repo, outcome_aggregator, agent, resolved_extract


def _resolve_goal_placeholders(
    extract: dict[str, Any], goals_repo: GoalsRepository
) -> dict[str, Any]:
    """Replace any :data:`GOAL_PLACEHOLDER` advances_goal_id with a real ULID.

    Inserts a goal into the repo on first encounter and reuses the same id
    across all events in the extract.
    """
    goal_id_cache: str | None = None

    def _ensure_goal_id() -> str:
        nonlocal goal_id_cache
        if goal_id_cache is None:
            goal = goals_repo.add(name="payments service refactor")
            goal_id_cache = goal.goal_id
        return goal_id_cache

    out_events: list[dict[str, Any]] = []
    for ev in extract.get("events", []) or []:
        ev_copy = dict(ev)
        if ev_copy.get("advances_goal_id") == GOAL_PLACEHOLDER:
            ev_copy["advances_goal_id"] = _ensure_goal_id()
        out_events.append(ev_copy)

    return {
        "events": out_events,
        "summary": dict(extract.get("summary") or {}),
    }


def _score_fields(
    expected_extract: dict[str, Any],
    persisted_non_summary: list[dict[str, Any]],
    persisted_summary: dict[str, Any] | None,
) -> tuple[int, int]:
    """Compare expected extract vs persisted state, returning (matches, total)."""
    matches = 0
    total = 0

    expected_events = expected_extract.get("events") or []
    for idx, exp_ev in enumerate(expected_events):
        # 'activity', 'timestamp', 'lifecycle' are always expected.
        for key in ("activity", "timestamp", "lifecycle"):
            total += 1
            if idx >= len(persisted_non_summary):
                continue
            persisted_value = persisted_non_summary[idx].get(key)
            if persisted_value == exp_ev.get(key):
                matches += 1

        # Optional fields counted only when set in expected.
        if "advances_goal_id" in exp_ev and exp_ev["advances_goal_id"] is not None:
            total += 1
            if idx < len(persisted_non_summary):
                if (
                    persisted_non_summary[idx].get("advances_goal")
                    == exp_ev["advances_goal_id"]
                ):
                    matches += 1
        if "duration_minutes" in exp_ev and exp_ev["duration_minutes"] is not None:
            total += 1
            if idx < len(persisted_non_summary):
                attrs = persisted_non_summary[idx].get("attributes") or {}
                if attrs.get("duration_minutes") == exp_ev["duration_minutes"]:
                    matches += 1
        if "resource" in exp_ev and exp_ev["resource"] is not None:
            total += 1
            if idx < len(persisted_non_summary):
                if persisted_non_summary[idx].get("resource") == exp_ev["resource"]:
                    matches += 1

    # Summary keys.
    expected_summary = expected_extract.get("summary") or {}
    for key in ("planned_tasks_completed", "planned_tasks_total"):
        total += 1
        if persisted_summary is None:
            continue
        attrs = persisted_summary.get("attributes") or {}
        if attrs.get(key) == expected_summary.get(key):
            matches += 1

    return matches, total


def _expected_field_count(extract: dict[str, Any]) -> int:
    """Count expected fields without scoring any matches (used in fail path)."""
    n = 0
    for ev in extract.get("events", []) or []:
        n += 3  # activity, timestamp, lifecycle
        for opt in ("advances_goal_id", "duration_minutes", "resource"):
            if opt in ev and ev[opt] is not None:
                n += 1
    n += 2  # summary keys
    return n


def _find_summary(events: list[dict[str, Any]]) -> dict[str, Any] | None:
    for ev in events:
        if ev.get("activity") == "debrief_summary":
            return ev
    return None


def _label_from_outcome(outcome_score: float) -> str:
    """Map a single-case outcome score to a workday cluster label.

    Uses the same boundary semantics as
    :class:`tm.engines.variant_cluster.VariantClusterer` (>= for upper, <
    for lower).  A single-case "mean" is just the case's outcome score, so
    a fixture with outcome=2 lands in good_day, outcome=1 in mixed,
    outcome=0 in bad_day.
    """
    if outcome_score >= GOOD_DAY_THRESHOLD:
        return "good_day"
    if outcome_score < BAD_DAY_THRESHOLD:
        return "bad_day"
    return "mixed"
