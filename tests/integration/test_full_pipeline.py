"""End-to-end integration tests for the v1 time-manager pipeline (T-INT-03).

These tests exercise the REAL glue between every layer — DebriefAgent
seeds events on a real SQLite DB, ProcessMiner / VariantClusterer /
OutcomeAggregator / KuzuStore / SchedulerAgent / SuggestionTelemetryRepository
all run against the same DB, and the LLM client is mocked at the
boundary only.  No live network, no live API, no production code edits.

Coverage matrix (mirrors AC1):

  - debrief -> outcome           (good_day / bad_day / mixed_day_*)
  - 7-day variant clustering     (good_day + bad_day clusters surface)
  - scheduler -> telemetry       (propose succeeds; rate-limit; record_actual_outcome)
  - kuzu projection round-trip   (list_models, get_petri_net)
  - error propagation            (validation errors not silently swallowed)
  - debrief_summary filter       (T-INT-01b regression — end-to-end)

Each test gets its own ``tmp_path`` SQLite DB seeded with the full
migration chain.  Vocabulary repo is also seeded with starter data so
canonical activities like ``deep_work`` and ``meeting`` round-trip
cleanly.
"""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any
from unittest.mock import Mock

import pytest

from tm.agents import (
    DebriefAgent,
    DebriefValidationError,
    ScheduledSuggestion,
    SchedulerAgent,
    SchedulerSkipReason,
)
from tm.engines.process_mining import ProcessMiner
from tm.engines.scheduler_metric import SchedulerSuccessMetric
from tm.engines.variant_cluster import VariantClusterer
from tm.llm.cost_meter import CostMeter
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.store import Store
from tm.stores.kuzu_projection import rebuild_kuzu_projection
from tm.stores.kuzu_store import KuzuStore
from tm.vocab_alignment import VocabAligner

# PM4Py emits noisy DeprecationWarnings; silence them for cleaner test output.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "pipeline.db"
    store = Store(path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return path


@pytest.fixture()
def vocab_repo(db_path: Path) -> VocabularyRepository:
    repo = VocabularyRepository(db_path)
    repo.seed_starter_vocabulary()
    repo.seed_starter_aliases()
    return repo


@pytest.fixture()
def goals_repo(db_path: Path) -> GoalsRepository:
    return GoalsRepository(db_path)


@pytest.fixture()
def events_repo(db_path: Path) -> EventsRepository:
    return EventsRepository(db_path)


@pytest.fixture()
def telemetry_repo(db_path: Path) -> SuggestionTelemetryRepository:
    return SuggestionTelemetryRepository(db_path)


@pytest.fixture()
def cost_meter(db_path: Path) -> CostMeter:
    return CostMeter(db_path, monthly_cap_usd=100.0)


@pytest.fixture()
def outcome_aggregator(events_repo: EventsRepository) -> OutcomeAggregator:
    return OutcomeAggregator(events_repo)


@pytest.fixture()
def process_miner(events_repo: EventsRepository) -> ProcessMiner:
    return ProcessMiner(events_repo)


@pytest.fixture()
def variant_clusterer(
    events_repo: EventsRepository, outcome_aggregator: OutcomeAggregator
) -> VariantClusterer:
    return VariantClusterer(events_repo, outcome_aggregator)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_debrief_agent(
    *,
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    extract_return: dict[str, Any],
) -> tuple[DebriefAgent, Mock]:
    debrief_llm = Mock()
    debrief_llm.extract.return_value = extract_return
    vocab_llm = Mock()
    aligner = VocabAligner(vocab_repo, vocab_llm)
    agent = DebriefAgent(
        llm_client=debrief_llm,
        vocab_aligner=aligner,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
    )
    return agent, debrief_llm


def _make_scheduler_agent(
    *,
    process_miner: ProcessMiner,
    variant_clusterer: VariantClusterer,
    outcome_aggregator: OutcomeAggregator,
    telemetry_repo: SuggestionTelemetryRepository,
    events_repo: EventsRepository,
    goals_repo: GoalsRepository,
    cost_meter: CostMeter,
    extract_return: dict[str, Any],
) -> tuple[SchedulerAgent, Mock]:
    llm = Mock()
    llm.extract.return_value = extract_return
    agent = SchedulerAgent(
        llm_client=llm,
        process_miner=process_miner,
        variant_clusterer=variant_clusterer,
        outcome_aggregator=outcome_aggregator,
        telemetry_repo=telemetry_repo,
        events_repo=events_repo,
        goals_repo=goals_repo,
        cost_meter=cost_meter,
    )
    return agent, llm


def _great_day_extract(
    case_date: str, *, advances_goal_id: str | None = None
) -> dict[str, Any]:
    return {
        "events": [
            {
                "activity": "deep_work",
                "timestamp": f"{case_date}T09:00:00Z",
                "lifecycle": "complete",
                "advances_goal_id": advances_goal_id,
                "duration_minutes": 90,
            },
            {
                "activity": "meeting",
                "timestamp": f"{case_date}T13:00:00Z",
                "lifecycle": "complete",
            },
            {
                "activity": "exercise",
                "timestamp": f"{case_date}T18:00:00Z",
                "lifecycle": "complete",
            },
        ],
        "summary": {"planned_tasks_completed": 8, "planned_tasks_total": 10},
    }


def _bad_day_extract(case_date: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "activity": "interruption",
                "timestamp": f"{case_date}T10:00:00Z",
                "lifecycle": "complete",
            }
        ],
        "summary": {"planned_tasks_completed": 2, "planned_tasks_total": 10},
    }


def _mixed_day_extract(
    case_date: str, *, advances_goal_id: str | None = None
) -> dict[str, Any]:
    return {
        "events": [
            {
                "activity": "deep_work",
                "timestamp": f"{case_date}T09:00:00Z",
                "lifecycle": "complete",
                "advances_goal_id": advances_goal_id,
            },
            {
                "activity": "meeting",
                "timestamp": f"{case_date}T14:00:00Z",
                "lifecycle": "complete",
            },
        ],
        "summary": {"planned_tasks_completed": 3, "planned_tasks_total": 10},
    }


def _completion_only_extract(case_date: str) -> dict[str, Any]:
    return {
        "events": [
            {
                "activity": "shallow_work",
                "timestamp": f"{case_date}T09:00:00Z",
                "lifecycle": "complete",
            },
            {
                "activity": "meeting",
                "timestamp": f"{case_date}T11:00:00Z",
                "lifecycle": "complete",
            },
        ],
        "summary": {"planned_tasks_completed": 8, "planned_tasks_total": 10},
    }


def _good_candidate_extract() -> dict[str, Any]:
    return {
        "recommended_action": "block 90 minutes for deep work after lunch",
        "predicted_outcome_with": 1.7,
        "predicted_outcome_without": 1.0,
        "predicted_post_suggestion_fitness": 0.7,
        "explanation": "historical good_day variants include a deep-work block",
    }


# ---------------------------------------------------------------------------
# debrief -> outcome
# ---------------------------------------------------------------------------


def test_debrief_to_outcome_pipeline_great_day(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    outcome_aggregator: OutcomeAggregator,
) -> None:
    """transcript -> debrief -> 3 events + summary -> outcome_score=2."""
    case_date = "2026-05-05"
    goal = goals_repo.add(name="ship payments service refactor")
    extract = _great_day_extract(case_date, advances_goal_id=goal.goal_id)
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )

    result = agent.extract_and_persist("Productive day.", case_date=case_date)
    assert result.events_persisted == 4  # 3 events + 1 summary

    breakdown = outcome_aggregator.for_case_date(case_date)
    assert breakdown.outcome_score == 2
    assert breakdown.did_complete_tasks is True  # 8/10 >= 0.80
    assert breakdown.did_advance_goal is True


def test_debrief_to_outcome_pipeline_bad_day(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    outcome_aggregator: OutcomeAggregator,
) -> None:
    """No goal advance + 2/10 ratio < 0.80 -> outcome_score=0."""
    case_date = "2026-05-08"
    extract = _bad_day_extract(case_date)
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )

    agent.extract_and_persist("Rough day.", case_date=case_date)
    breakdown = outcome_aggregator.for_case_date(case_date)
    assert breakdown.outcome_score == 0
    assert breakdown.did_complete_tasks is False
    assert breakdown.did_advance_goal is False


def test_debrief_to_outcome_pipeline_mixed_day_completion_only(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    outcome_aggregator: OutcomeAggregator,
) -> None:
    """Completion >= 0.80 but no advances_goal -> outcome_score=1."""
    case_date = "2026-05-06"
    extract = _completion_only_extract(case_date)
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )

    agent.extract_and_persist("Got the list done.", case_date=case_date)
    breakdown = outcome_aggregator.for_case_date(case_date)
    assert breakdown.outcome_score == 1
    assert breakdown.did_complete_tasks is True
    assert breakdown.did_advance_goal is False


def test_debrief_to_outcome_pipeline_mixed_day_goal_only(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    outcome_aggregator: OutcomeAggregator,
) -> None:
    """Advanced a goal but completion ratio < 0.80 -> outcome_score=1."""
    case_date = "2026-05-07"
    goal = goals_repo.add(name="ship payments service refactor")
    extract = _mixed_day_extract(case_date, advances_goal_id=goal.goal_id)
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )

    agent.extract_and_persist("Pushed the goal.", case_date=case_date)
    breakdown = outcome_aggregator.for_case_date(case_date)
    assert breakdown.outcome_score == 1
    assert breakdown.did_complete_tasks is False
    assert breakdown.did_advance_goal is True


# ---------------------------------------------------------------------------
# Multi-day variant clustering
# ---------------------------------------------------------------------------


def test_full_pipeline_seven_days_then_variant_clusters(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
    variant_clusterer: VariantClusterer,
) -> None:
    """Seed 7 case_dates with mixed outcomes; expect at least one good and one bad cluster."""  # noqa: E501
    goal = goals_repo.add(name="ship payments service refactor")

    # 4 good_days + 3 bad_days, each persisted via the real DebriefAgent path.
    good_dates = ["2026-05-01", "2026-05-02", "2026-05-03", "2026-05-04"]
    bad_dates = ["2026-05-05", "2026-05-06", "2026-05-07"]

    for d in good_dates:
        agent, _ = _make_debrief_agent(
            vocab_repo=vocab_repo,
            goals_repo=goals_repo,
            events_repo=events_repo,
            cost_meter=cost_meter,
            extract_return=_great_day_extract(d, advances_goal_id=goal.goal_id),
        )
        agent.extract_and_persist("Good.", case_date=d)

    for d in bad_dates:
        agent, _ = _make_debrief_agent(
            vocab_repo=vocab_repo,
            goals_repo=goals_repo,
            events_repo=events_repo,
            cost_meter=cost_meter,
            extract_return=_bad_day_extract(d),
        )
        agent.extract_and_persist("Bad.", case_date=d)

    # query_events 'until' is exclusive on timestamp; widen past May 7 events.
    analysis = process_miner.analyze_variants(
        lens="workday", since="2026-05-01", until="2026-05-08"
    )
    assert analysis.total_cases == 7
    # At least 2 distinct sequences: good_day (deep_work,meeting,exercise)
    # vs bad_day (interruption).
    assert analysis.distinct_variants >= 2

    clustering = variant_clusterer.cluster_workday_variants(analysis)
    summary = clustering.summary
    # Good and bad clusters should both surface.
    assert summary.get("good_day", 0) >= 1
    assert summary.get("bad_day", 0) >= 1


# ---------------------------------------------------------------------------
# Scheduler -> telemetry
# ---------------------------------------------------------------------------


def test_full_pipeline_scheduler_proposes_after_seeding_history(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
    variant_clusterer: VariantClusterer,
    outcome_aggregator: OutcomeAggregator,
    telemetry_repo: SuggestionTelemetryRepository,
) -> None:
    """5 days of seeded history -> scheduler proposes a guardrail-passing suggestion."""
    goal = goals_repo.add(name="ship payments service refactor")
    history_dates = [
        "2026-05-01",
        "2026-05-02",
        "2026-05-03",
        "2026-05-04",
        "2026-05-05",
    ]
    for d in history_dates:
        agent, _ = _make_debrief_agent(
            vocab_repo=vocab_repo,
            goals_repo=goals_repo,
            events_repo=events_repo,
            cost_meter=cost_meter,
            extract_return=_great_day_extract(d, advances_goal_id=goal.goal_id),
        )
        agent.extract_and_persist("Good.", case_date=d)

    # Today (the scheduler's case_date) gets a partial trace so context isn't empty.
    today = "2026-05-06"
    today_agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=_completion_only_extract(today),
    )
    today_agent.extract_and_persist("Today.", case_date=today)

    scheduler, _ = _make_scheduler_agent(
        process_miner=process_miner,
        variant_clusterer=variant_clusterer,
        outcome_aggregator=outcome_aggregator,
        telemetry_repo=telemetry_repo,
        events_repo=events_repo,
        goals_repo=goals_repo,
        cost_meter=cost_meter,
        extract_return=_good_candidate_extract(),
    )
    result = scheduler.propose_suggestion(case_date=today)

    assert isinstance(result, ScheduledSuggestion)
    assert result.case_date == today
    rec = telemetry_repo.get(result.suggestion_id)
    assert rec is not None
    assert rec.recommended_action == "block 90 minutes for deep work after lunch"


def test_full_pipeline_scheduler_rate_limited_after_first_propose(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
    variant_clusterer: VariantClusterer,
    outcome_aggregator: OutcomeAggregator,
    telemetry_repo: SuggestionTelemetryRepository,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Calling propose twice for the same case_date -> second call rate_limited."""
    monkeypatch.delenv("TM_MAX_PROACTIVE_SUGGESTIONS_PER_DAY", raising=False)
    case_date = "2026-05-10"
    # Seed a partial trace so context is non-empty.
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=_completion_only_extract(case_date),
    )
    agent.extract_and_persist("Today.", case_date=case_date)

    scheduler, _ = _make_scheduler_agent(
        process_miner=process_miner,
        variant_clusterer=variant_clusterer,
        outcome_aggregator=outcome_aggregator,
        telemetry_repo=telemetry_repo,
        events_repo=events_repo,
        goals_repo=goals_repo,
        cost_meter=cost_meter,
        extract_return=_good_candidate_extract(),
    )
    first = scheduler.propose_suggestion(case_date=case_date)
    second = scheduler.propose_suggestion(case_date=case_date)

    assert isinstance(first, ScheduledSuggestion)
    assert isinstance(second, SchedulerSkipReason)
    assert second.reason == "rate_limited"


def test_full_pipeline_telemetry_records_actual_outcome(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
    variant_clusterer: VariantClusterer,
    outcome_aggregator: OutcomeAggregator,
    telemetry_repo: SuggestionTelemetryRepository,
) -> None:
    """Log a suggestion, record actual_outcome, then summarize via the metric."""
    case_date = "2026-05-12"
    # Partial trace.
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=_completion_only_extract(case_date),
    )
    agent.extract_and_persist("Today.", case_date=case_date)

    scheduler, _ = _make_scheduler_agent(
        process_miner=process_miner,
        variant_clusterer=variant_clusterer,
        outcome_aggregator=outcome_aggregator,
        telemetry_repo=telemetry_repo,
        events_repo=events_repo,
        goals_repo=goals_repo,
        cost_meter=cost_meter,
        extract_return=_good_candidate_extract(),
    )
    suggestion = scheduler.propose_suggestion(case_date=case_date)
    assert isinstance(suggestion, ScheduledSuggestion)

    # End-of-day: record the actual observed outcome.
    telemetry_repo.record_actual_outcome(suggestion.suggestion_id, actual_outcome=2)

    metric = SchedulerSuccessMetric(telemetry_repo, outcome_aggregator)
    summary = metric.summarize()
    assert summary.total_suggestions == 1
    assert summary.suggestions_with_actual == 1
    assert summary.mean_actual_outcome_score == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Kuzu projection round-trip
# ---------------------------------------------------------------------------


def test_full_pipeline_kuzu_projection_after_discovery(
    tmp_path: Path,
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
) -> None:
    """Discover -> project to KuzuStore -> list_models / get_petri_net round-trip."""
    # Seed at least 3 days so PM4Py has something to mine.
    for d in ["2026-05-01", "2026-05-02", "2026-05-03"]:
        agent, _ = _make_debrief_agent(
            vocab_repo=vocab_repo,
            goals_repo=goals_repo,
            events_repo=events_repo,
            cost_meter=cost_meter,
            extract_return=_great_day_extract(d),
        )
        agent.extract_and_persist("Day.", case_date=d)

    kuzu_path = tmp_path / "kuzu"
    kuzu = KuzuStore(kuzu_path)
    persisted = rebuild_kuzu_projection(
        events_repo=events_repo,
        kuzu_store=kuzu,
        process_miner=process_miner,
        lens="workday",
        since="2026-05-01",
        until="2026-05-31",
    )
    assert persisted.lens == "workday"
    assert persisted.case_count >= 3

    listed = kuzu.list_models(lens="workday")
    assert any(m.model_id == persisted.model_id for m in listed)

    net = kuzu.get_petri_net(persisted.model_id)
    assert net is not None
    # Round-trip preserves at least the activity vocabulary we mined.
    assert net.activities, "expected non-empty activities tuple from a 3-day log"


# ---------------------------------------------------------------------------
# Error propagation
# ---------------------------------------------------------------------------


def test_pipeline_handles_validation_errors_propagated(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
) -> None:
    """A malformed LLM extract surfaces DebriefValidationError, not a silent swallow."""
    # Missing 'events' key in the LLM output.
    extract: dict[str, Any] = {
        "summary": {"planned_tasks_completed": 0, "planned_tasks_total": 0}
    }
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )
    with pytest.raises(DebriefValidationError):
        agent.extract_and_persist("Bad output.", case_date="2026-05-09")

    # Nothing was persisted.
    assert events_repo.query_events(case_date="2026-05-09") == []


def test_pipeline_handles_double_debrief_for_same_case_date(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    outcome_aggregator: OutcomeAggregator,
) -> None:
    """Single-summary-per-case_date contract holds end-to-end."""
    case_date = "2026-05-11"
    extract = _great_day_extract(case_date)
    first_agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )
    first_agent.extract_and_persist("First.", case_date=case_date)

    second_agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=extract,
    )
    with pytest.raises(DebriefValidationError):
        second_agent.extract_and_persist("Second.", case_date=case_date)

    # Outcome remains correct (T-OUT-01 MAX semantics not corrupted).
    breakdown = outcome_aggregator.for_case_date(case_date)
    assert breakdown.planned_tasks_completed == 8
    assert breakdown.planned_tasks_total == 10


# ---------------------------------------------------------------------------
# debrief_summary filter (T-INT-01b regression — end-to-end)
# ---------------------------------------------------------------------------


def test_full_pipeline_processminer_excludes_debrief_summary_by_default(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
) -> None:
    """T-INT-01b filter is enforced through the full debrief -> processminer path."""
    case_date = "2026-05-15"
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=_great_day_extract(case_date),
    )
    agent.extract_and_persist("Today.", case_date=case_date)

    # The summary event lives in the events table.
    summary_rows = events_repo.query_events(
        case_date=case_date, activity="debrief_summary"
    )
    assert len(summary_rows) == 1, "summary event was persisted"

    # ProcessMiner sees the activities BUT not the summary, by default.
    # (until is exclusive on timestamp -> widen so the day's events fit.)
    analysis = process_miner.analyze_variants(
        lens="workday", since=case_date, until="2026-05-16"
    )
    assert analysis.total_cases == 1
    # Real activities are present...
    assert any(variant.sequence for variant in analysis.variants)
    # ...but the synthetic summary event is filtered out by default.
    for variant in analysis.variants:
        assert "debrief_summary" not in variant.sequence


def test_full_pipeline_processminer_can_opt_in_to_summary_events(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    process_miner: ProcessMiner,
) -> None:
    """include_summary_events=True surfaces the summary event when callers want it."""
    case_date = "2026-05-16"
    agent, _ = _make_debrief_agent(
        vocab_repo=vocab_repo,
        goals_repo=goals_repo,
        events_repo=events_repo,
        cost_meter=cost_meter,
        extract_return=_great_day_extract(case_date),
    )
    agent.extract_and_persist("Today.", case_date=case_date)

    # 'until' is exclusive on timestamp, so widen past 23:59:59Z.
    analysis = process_miner.analyze_variants(
        lens="workday",
        since=case_date,
        until="2026-05-17",
        include_summary_events=True,
    )
    found = False
    for variant in analysis.variants:
        if "debrief_summary" in variant.sequence:
            found = True
            break
    assert found, "include_summary_events=True must include the summary"


# ---------------------------------------------------------------------------
# Date-range outcome rollup (extra layer integration coverage)
# ---------------------------------------------------------------------------


def test_full_pipeline_outcome_for_date_range_after_multi_day_seed(
    vocab_repo: VocabularyRepository,
    goals_repo: GoalsRepository,
    events_repo: EventsRepository,
    cost_meter: CostMeter,
    outcome_aggregator: OutcomeAggregator,
) -> None:
    """OutcomeAggregator.for_date_range maps distinct case_dates to breakdowns."""
    goal = goals_repo.add(name="ship payments service refactor")
    for d in ["2026-05-01", "2026-05-02", "2026-05-03"]:
        agent, _ = _make_debrief_agent(
            vocab_repo=vocab_repo,
            goals_repo=goals_repo,
            events_repo=events_repo,
            cost_meter=cost_meter,
            extract_return=_great_day_extract(d, advances_goal_id=goal.goal_id),
        )
        agent.extract_and_persist("Day.", case_date=d)

    rollup = outcome_aggregator.for_date_range(since="2026-05-01", until="2026-05-31")
    assert set(rollup.keys()) == {"2026-05-01", "2026-05-02", "2026-05-03"}
    for breakdown in rollup.values():
        assert breakdown.outcome_score == 2
