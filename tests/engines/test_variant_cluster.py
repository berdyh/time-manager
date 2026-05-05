"""Tests for tm.engines.variant_cluster.VariantClusterer (T-OUT-03).

Uses real :class:`ProcessMiner`, real :class:`OutcomeAggregator`, and a
real ``EventsRepository`` backed by a tmp-path SQLite file.  Fixtures seed
events whose attributes drive deterministic outcome scores, so each test
asserts on labels computed end-to-end without mocks.
"""

from __future__ import annotations

import dataclasses
import warnings
from pathlib import Path

import pytest

from tm.engines.process_mining import ProcessMiner, Variant
from tm.engines.variant_cluster import (
    BAD_DAY_THRESHOLD,
    EFFECTIVE_OUTCOME_THRESHOLD,
    EFFECTIVE_THROUGHPUT_MAX,
    GOOD_DAY_THRESHOLD,
    LabeledVariant,
    VariantClusterer,
    VariantClustering,
)
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository

warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_repos(tmp_path: Path) -> tuple[EventsRepository, GoalsRepository]:
    """Apply migrations and return events + goals repositories."""
    from tm.store import Store

    db = tmp_path / "tm.db"
    store = Store(db, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return EventsRepository(db), GoalsRepository(db)


def _seed_workday_outcomes(
    repo: EventsRepository, goals_repo: GoalsRepository
) -> dict[str, float]:
    """Seed five workdays with a known per-day outcome score.

    Each workday has the same activity sequence ``A, B, C`` so they all
    collapse into a single :class:`Variant`.  The variants test fixture
    therefore yields one variant containing five case_dates whose mean
    outcome is ``(2 + 2 + 1 + 1 + 0) / 5 = 1.2`` — the ``mixed`` band.

    Returns a mapping ``{case_date: expected_outcome_score}`` so individual
    tests can re-derive the expected mean.
    """
    g1 = goals_repo.add(name="Outcome Goal").goal_id

    plan: list[tuple[str, int, int, bool]] = [
        # date, completed, total, advances_goal
        ("2026-01-01", 10, 10, True),  # 2 (complete + advance)
        ("2026-01-02", 9, 10, True),  # 2
        ("2026-01-03", 5, 10, True),  # 1 (advance only)
        ("2026-01-04", 8, 10, False),  # 1 (complete only)
        ("2026-01-05", 0, 10, False),  # 0
    ]
    eid = 0
    for date, completed, total, advancing in plan:
        for hour, activity in enumerate(["A", "B", "C"], start=10):
            eid += 1
            attrs: dict[str, object] = {}
            # Stamp the planned-tasks counts on the LAST event of the day so
            # OutcomeAggregator picks them up via per-attribute MAX semantics.
            if activity == "C":
                attrs["planned_tasks_completed"] = completed
                attrs["planned_tasks_total"] = total
            repo.append_event(
                event_id=f"OUT{eid:04d}",
                case_id=f"OC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                attributes=attrs or None,
                advances_goal=g1 if (advancing and activity == "C") else None,
            )
    expected: dict[str, float] = {}
    for date, completed, total, advancing in plan:
        score = 0
        if total > 0 and (completed / total) >= 0.80:
            score += 1
        if advancing:
            score += 1
        expected[date] = float(score)
    return expected


def _seed_bare_workday_log(repo: EventsRepository) -> None:
    """Seed three workdays with NO planned_tasks_* attrs and no advances_goal.

    Each workday has the same A,B,C sequence.  Outcome is 0 for every day,
    so the single variant is labeled ``bad_day``.
    """
    eid = 0
    for date in ("2026-02-01", "2026-02-02", "2026-02-03"):
        for hour, activity in enumerate(["A", "B", "C"], start=10):
            eid += 1
            repo.append_event(
                event_id=f"BARE{eid:04d}",
                case_id=f"B{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
            )


def _seed_goal_pursuit_log(
    repo: EventsRepository,
    goals_repo: GoalsRepository,
    *,
    num_dates: int,
    high_outcome: bool,
) -> str:
    """Seed a single goal-pursuit case with ``num_dates`` working days.

    When ``high_outcome=True`` each day completes 10/10 tasks AND advances
    the goal (per-day score = 2; mean across the goal's events = 2).  When
    ``False``, no planned_tasks_* attributes and no advances_goal so the
    OutcomeBreakdown comes back as outcome_score=0.

    Returns the goal_id, used as the variant case_id under the goal lens.
    """
    goal_id = goals_repo.add(name="GoalPursuit").goal_id
    eid = 0
    for i in range(num_dates):
        date = f"2026-03-{i + 1:02d}"
        for hour, activity in enumerate(["X", "Y"], start=10):
            eid += 1
            attrs: dict[str, object] = {}
            advances = None
            if high_outcome and activity == "Y":
                attrs["planned_tasks_completed"] = 10
                attrs["planned_tasks_total"] = 10
                advances = goal_id
            repo.append_event(
                event_id=f"GP{eid:04d}",
                case_id=f"GPC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                attributes=attrs or None,
                advances_goal=advances,
                case_goal_id=goal_id,
            )
    return goal_id


def _build_clusterer(
    tmp_path: Path,
) -> tuple[VariantClusterer, ProcessMiner, EventsRepository, GoalsRepository]:
    repo, goals = _make_repos(tmp_path)
    aggregator = OutcomeAggregator(repo)
    clusterer = VariantClusterer(repo, aggregator)
    miner = ProcessMiner(repo)
    return clusterer, miner, repo, goals


# ---------------------------------------------------------------------------
# Workday lens — labeling
# ---------------------------------------------------------------------------


def test_cluster_workday_variants_labels_correctly(tmp_path: Path) -> None:
    """The single A,B,C variant covers 5 days w/ mean outcome = 1.2 → mixed."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    expected_per_day = _seed_workday_outcomes(repo, goals)

    analysis = miner.analyze_variants(lens="workday")
    assert analysis.distinct_variants == 1
    assert analysis.variants[0].case_count == 5

    clustering = clusterer.cluster_workday_variants(analysis)
    assert isinstance(clustering, VariantClustering)
    assert len(clustering.labeled_variants) == 1

    only = clustering.labeled_variants[0]
    assert isinstance(only, LabeledVariant)
    expected_mean = sum(expected_per_day.values()) / len(expected_per_day)
    assert only.mean_outcome_score == pytest.approx(expected_mean)
    assert only.case_count_with_outcome == 5
    # 1.2 is in [0.5, 1.5) → mixed
    assert only.label == "mixed"
    assert clustering.total_cases == 5
    assert clustering.cases_with_outcome == 5


def test_cluster_workday_variants_good_day_label(tmp_path: Path) -> None:
    """Every day completes + advances → mean outcome = 2.0 → good_day."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    g1 = goals.add(name="G").goal_id
    eid = 0
    for date in ("2026-04-01", "2026-04-02"):
        for hour, activity in enumerate(["A", "B"], start=10):
            eid += 1
            attrs = (
                {"planned_tasks_completed": 10, "planned_tasks_total": 10}
                if activity == "B"
                else None
            )
            repo.append_event(
                event_id=f"GD{eid:04d}",
                case_id=f"GDC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                attributes=attrs,
                advances_goal=g1 if activity == "B" else None,
            )
    analysis = miner.analyze_variants(lens="workday")
    clustering = clusterer.cluster_workday_variants(analysis)

    assert len(clustering.labeled_variants) == 1
    only = clustering.labeled_variants[0]
    assert only.mean_outcome_score == pytest.approx(2.0)
    assert only.label == "good_day"


def test_cluster_workday_variants_zero_cases_with_outcome_returns_bad_day(
    tmp_path: Path,
) -> None:
    """Variants whose cases have no planned_tasks attrs → mean=0 → bad_day."""
    clusterer, miner, repo, _ = _build_clusterer(tmp_path)
    _seed_bare_workday_log(repo)

    analysis = miner.analyze_variants(lens="workday")
    clustering = clusterer.cluster_workday_variants(analysis)

    assert len(clustering.labeled_variants) == 1
    only = clustering.labeled_variants[0]
    assert only.mean_outcome_score == pytest.approx(0.0)
    # All cases resolved (the aggregator returns score=0 for them) so they
    # still count toward case_count_with_outcome.
    assert only.case_count_with_outcome == 3
    assert only.label == "bad_day"


def test_cluster_workday_variants_empty_analysis(tmp_path: Path) -> None:
    """Zero variants → empty result, empty summary, zero counts."""
    clusterer, miner, _, _ = _build_clusterer(tmp_path)
    analysis = miner.analyze_variants(lens="workday")
    assert analysis.variants == ()

    clustering = clusterer.cluster_workday_variants(analysis)
    assert clustering.labeled_variants == ()
    assert clustering.summary == {}
    assert clustering.total_cases == 0
    assert clustering.cases_with_outcome == 0


def test_cluster_workday_boundary_at_one_point_five_is_good_day(
    tmp_path: Path,
) -> None:
    """Mean exactly 1.5 → good_day (>= inclusive)."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    # Two days: one outcome 2, one outcome 1 → mean 1.5
    g1 = goals.add(name="GBoundary").goal_id
    eid = 0
    plan: list[tuple[str, int, int, bool]] = [
        # date, completed, total, advances
        ("2026-05-01", 10, 10, True),  # 2
        ("2026-05-02", 5, 10, True),  # 1 (advance only)
    ]
    for date, completed, total, advancing in plan:
        for hour, activity in enumerate(["P", "Q"], start=10):
            eid += 1
            attrs = (
                {"planned_tasks_completed": completed, "planned_tasks_total": total}
                if activity == "Q"
                else None
            )
            repo.append_event(
                event_id=f"BND{eid:04d}",
                case_id=f"BNC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                attributes=attrs,
                advances_goal=g1 if (advancing and activity == "Q") else None,
            )
    analysis = miner.analyze_variants(lens="workday")
    clustering = clusterer.cluster_workday_variants(analysis)
    only = clustering.labeled_variants[0]
    assert only.mean_outcome_score == pytest.approx(1.5)
    assert only.label == "good_day"


def test_cluster_workday_boundary_at_zero_point_five_is_mixed(
    tmp_path: Path,
) -> None:
    """Mean exactly 0.5 → mixed (>= bad_day_threshold)."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    g1 = goals.add(name="GLow").goal_id
    eid = 0
    # day1 outcome 1 (advance only), day2 outcome 0 → mean 0.5
    plan: list[tuple[str, bool]] = [
        ("2026-06-01", True),
        ("2026-06-02", False),
    ]
    for date, advancing in plan:
        for hour, activity in enumerate(["A", "B"], start=10):
            eid += 1
            repo.append_event(
                event_id=f"LOW{eid:04d}",
                case_id=f"LC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                advances_goal=g1 if (advancing and activity == "B") else None,
            )
    analysis = miner.analyze_variants(lens="workday")
    clustering = clusterer.cluster_workday_variants(analysis)
    only = clustering.labeled_variants[0]
    assert only.mean_outcome_score == pytest.approx(0.5)
    assert only.label == "mixed"


# ---------------------------------------------------------------------------
# Goal-pursuit lens
# ---------------------------------------------------------------------------


def test_cluster_goal_pursuit_variants_effective(tmp_path: Path) -> None:
    """High outcome + low case count → effective_pursuit."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    # Single goal, 3 working days → variant has case_count=1 (one goal_id).
    _seed_goal_pursuit_log(repo, goals, num_dates=3, high_outcome=True)

    analysis = miner.analyze_variants(lens="goal_pursuit")
    assert analysis.distinct_variants == 1
    only_variant = analysis.variants[0]
    assert only_variant.case_count == 1  # one goal_id

    clustering = clusterer.cluster_goal_pursuit_variants(analysis)
    only = clustering.labeled_variants[0]
    # Goal had 3 days each scoring 2 (advance + complete) → mean = 2.0
    assert only.mean_outcome_score == pytest.approx(2.0)
    # case_count (1) <= 7 AND mean (2.0) >= 1.5 → effective
    assert only.label == "effective_pursuit"


def test_cluster_goal_pursuit_variants_stuck_low_outcome(tmp_path: Path) -> None:
    """Low outcome (no completion + no advance) → stuck regardless of count."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    _seed_goal_pursuit_log(repo, goals, num_dates=2, high_outcome=False)

    analysis = miner.analyze_variants(lens="goal_pursuit")
    clustering = clusterer.cluster_goal_pursuit_variants(analysis)
    only = clustering.labeled_variants[0]
    assert only.mean_outcome_score == pytest.approx(0.0)
    assert only.label == "stuck"


def test_cluster_goal_pursuit_variants_stuck_high_throughput(
    tmp_path: Path,
) -> None:
    """High outcome but case_count above the throughput cap → stuck.

    We construct a variant whose case_count exceeds ``effective_throughput_max``
    by giving multiple goals the SAME activity sequence (X, Y); then the
    variant's case_ids tuple has many distinct goal ids.
    """
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    # Use a tighter throughput cap so the test stays small.
    aggregator = OutcomeAggregator(repo)
    custom_clusterer = VariantClusterer(
        repo,
        aggregator,
        effective_throughput_max=2,
    )

    # Seed 3 goals (above the cap of 2), all with high outcome.
    goal_ids: list[str] = []
    eid = 0
    for i in range(3):
        gid = goals.add(name=f"HG{i}").goal_id
        goal_ids.append(gid)
        for hour, activity in enumerate(["X", "Y"], start=10):
            eid += 1
            attrs = (
                {"planned_tasks_completed": 10, "planned_tasks_total": 10}
                if activity == "Y"
                else None
            )
            repo.append_event(
                event_id=f"HG{eid:04d}",
                case_id=f"HGC{eid}",
                activity=activity,
                timestamp=f"2026-07-{i + 1:02d}T{hour:02d}:00:00Z",
                lifecycle="complete",
                attributes=attrs,
                advances_goal=gid if activity == "Y" else None,
                case_goal_id=gid,
            )

    analysis = miner.analyze_variants(lens="goal_pursuit")
    only_variant = analysis.variants[0]
    assert only_variant.case_count == 3  # three goal_ids, all sharing X,Y

    clustering = custom_clusterer.cluster_goal_pursuit_variants(analysis)
    only = clustering.labeled_variants[0]
    # Mean outcome is 2.0 (each goal completes + advances) but case_count=3
    # exceeds custom cap of 2 → stuck.
    assert only.mean_outcome_score == pytest.approx(2.0)
    assert only.label == "stuck"


def test_cluster_goal_pursuit_empty_analysis(tmp_path: Path) -> None:
    clusterer, miner, _, _ = _build_clusterer(tmp_path)
    analysis = miner.analyze_variants(lens="goal_pursuit")
    clustering = clusterer.cluster_goal_pursuit_variants(analysis)
    assert clustering.labeled_variants == ()
    assert clustering.summary == {}
    assert clustering.total_cases == 0


# ---------------------------------------------------------------------------
# Summary / metadata
# ---------------------------------------------------------------------------


def test_cluster_summary_counts_by_label(tmp_path: Path) -> None:
    """summary dict counts variants per label and sums to len(labeled)."""
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)

    # Seed two distinct variants with different outcomes.
    g1 = goals.add(name="SumG").goal_id
    eid = 0
    # Variant 1 (sequence A,B): two days, both complete + advance → mean 2.0
    for date in ("2026-08-01", "2026-08-02"):
        for hour, activity in enumerate(["A", "B"], start=10):
            eid += 1
            attrs = (
                {"planned_tasks_completed": 10, "planned_tasks_total": 10}
                if activity == "B"
                else None
            )
            repo.append_event(
                event_id=f"SUM{eid:04d}",
                case_id=f"SC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                attributes=attrs,
                advances_goal=g1 if activity == "B" else None,
            )
    # Variant 2 (sequence X,Y): one day, no completion or advance → score 0
    for hour, activity in enumerate(["X", "Y"], start=10):
        eid += 1
        repo.append_event(
            event_id=f"SUM{eid:04d}",
            case_id=f"SC{eid}",
            activity=activity,
            timestamp=f"2026-08-03T{hour:02d}:00:00Z",
            lifecycle="complete",
        )

    analysis = miner.analyze_variants(lens="workday")
    assert analysis.distinct_variants == 2

    clustering = clusterer.cluster_workday_variants(analysis)
    assert sum(clustering.summary.values()) == len(clustering.labeled_variants)
    # Variant 1 → good_day (2.0), variant 2 → bad_day (0.0)
    assert clustering.summary.get("good_day", 0) == 1
    assert clustering.summary.get("bad_day", 0) == 1


def test_cluster_extractor_metadata_includes_thresholds(tmp_path: Path) -> None:
    clusterer, miner, repo, _ = _build_clusterer(tmp_path)
    _seed_bare_workday_log(repo)
    analysis = miner.analyze_variants(lens="workday")

    clustering = clusterer.cluster_workday_variants(
        analysis, since="2026-02-01", until="2026-02-03"
    )
    md = clustering.extractor_metadata
    assert md["lens"] == "workday"
    assert md["since"] == "2026-02-01"
    assert md["until"] == "2026-02-03"
    assert md["good_day_threshold"] == GOOD_DAY_THRESHOLD
    assert md["bad_day_threshold"] == BAD_DAY_THRESHOLD


def test_cluster_extractor_metadata_goal_pursuit(tmp_path: Path) -> None:
    clusterer, miner, repo, goals = _build_clusterer(tmp_path)
    _seed_goal_pursuit_log(repo, goals, num_dates=1, high_outcome=False)
    analysis = miner.analyze_variants(lens="goal_pursuit")

    clustering = clusterer.cluster_goal_pursuit_variants(analysis, goals_repo=goals)
    md = clustering.extractor_metadata
    assert md["lens"] == "goal_pursuit"
    assert md["effective_outcome_threshold"] == EFFECTIVE_OUTCOME_THRESHOLD
    assert md["effective_throughput_max"] == EFFECTIVE_THROUGHPUT_MAX
    assert md["goals_repo_available"] is True


def test_labeled_variant_dataclass_frozen() -> None:
    v = Variant(sequence=("A",), case_count=1, case_ids=("d",))
    lv = LabeledVariant(
        variant=v,
        mean_outcome_score=1.0,
        case_count_with_outcome=1,
        label="mixed",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        lv.label = "good_day"  # type: ignore[misc]


def test_variant_clustering_dataclass_frozen() -> None:
    clustering = VariantClustering(
        labeled_variants=(),
        summary={},
        total_cases=0,
        cases_with_outcome=0,
        extractor_metadata={},
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        clustering.total_cases = 1  # type: ignore[misc]


def test_clusterer_rejects_invalid_threshold_ordering(tmp_path: Path) -> None:
    repo, _ = _make_repos(tmp_path)
    aggregator = OutcomeAggregator(repo)
    with pytest.raises(ValueError, match="strictly greater"):
        VariantClusterer(
            repo,
            aggregator,
            good_day_threshold=0.5,
            bad_day_threshold=0.5,
        )


def test_clusterer_rejects_negative_throughput_cap(tmp_path: Path) -> None:
    repo, _ = _make_repos(tmp_path)
    aggregator = OutcomeAggregator(repo)
    with pytest.raises(ValueError, match="non-negative"):
        VariantClusterer(repo, aggregator, effective_throughput_max=-1)
