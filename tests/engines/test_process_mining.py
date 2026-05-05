"""Tests for tm.engines.process_mining.ProcessMiner.

The tests run the *real* PM4Py against tiny synthetic event logs persisted
through an isolated tmp-path SQLite + EventsRepository.  No PM4Py mocking —
the operations are deterministic on small inputs and complete in well under
a second per test.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import pytest

from tm.engines.process_mining import (
    ConformanceResult,
    DiscoveredModel,
    PerformanceAnalysis,
    PerformanceMetric,
    ProcessMiner,
    Variant,
    VariantAnalysis,
    _events_to_dataframe,
)
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository

# PM4Py emits noisy DeprecationWarnings + tqdm bars in some environments;
# keep the test output clean.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _make_repo(tmp_path: Path) -> EventsRepository:
    """Apply migrations on a tmp DB and return an EventsRepository."""
    from tm.store import Store

    db = tmp_path / "tm.db"
    store = Store(db, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return EventsRepository(db)


def _make_repo_with_goals(
    tmp_path: Path,
) -> tuple[EventsRepository, GoalsRepository]:
    from tm.store import Store

    db = tmp_path / "tm.db"
    store = Store(db, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return EventsRepository(db), GoalsRepository(db)


def _seed_workday_log(repo: EventsRepository) -> None:
    """Three workdays each with the sequence A, B, C (1-hour gaps)."""
    plan = [
        ("2026-01-01", ["A", "B", "C"]),
        ("2026-01-02", ["A", "B", "C"]),
        ("2026-01-03", ["A", "B", "C"]),
    ]
    eid = 0
    for date, seq in plan:
        for hour, activity in enumerate(seq, start=10):
            eid += 1
            repo.append_event(
                event_id=f"EVT{eid:04d}",
                case_id=f"C{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
            )


def _seed_goal_log(
    repo: EventsRepository, goals_repo: GoalsRepository
) -> tuple[str, str]:
    """Two goal-pursuit cases each with sequence X, Y."""
    g1 = goals_repo.add(name="Goal One").goal_id
    g2 = goals_repo.add(name="Goal Two").goal_id
    eid = 0
    for date, gid in [
        ("2026-02-01", g1),
        ("2026-02-02", g1),
        ("2026-02-03", g2),
    ]:
        for hour, act in [(10, "X"), (11, "Y")]:
            eid += 1
            repo.append_event(
                event_id=f"GVT{eid:04d}",
                case_id=f"GC{eid}",
                activity=act,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
                case_goal_id=gid,
            )
    return g1, g2


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------


def test_discover_workday_returns_DiscoveredModel(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)

    model = miner.discover_inductive_miner(lens="workday")

    assert isinstance(model, DiscoveredModel)
    assert model.case_count == 3
    assert model.activity_count == 3
    assert model.fitness is not None and model.fitness >= 0.5
    assert model.precision is not None and model.precision >= 0.0
    assert model.process_tree_repr  # non-empty
    assert model.petri_net_summary["transitions"] >= 3
    assert model.petri_net_summary["places"] >= 2
    assert model.extractor_metadata["lens"] == "workday"


def test_discover_goal_pursuit_lens(tmp_path: Path) -> None:
    repo, goals = _make_repo_with_goals(tmp_path)
    g1, g2 = _seed_goal_log(repo, goals)
    miner = ProcessMiner(repo)

    model = miner.discover_inductive_miner(lens="goal_pursuit")

    assert isinstance(model, DiscoveredModel)
    # Two distinct goal-pursuit case ids (g1 and g2).
    assert model.case_count == 2
    assert model.activity_count == 2
    assert model.extractor_metadata["lens"] == "goal_pursuit"


def test_discover_empty_log_returns_zeroed_model(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    miner = ProcessMiner(repo)

    model = miner.discover_inductive_miner(lens="workday")

    assert model.case_count == 0
    assert model.activity_count == 0
    assert model.fitness is None
    assert model.precision is None
    assert model.process_tree_repr == ""
    assert model.petri_net_summary == {"places": 0, "transitions": 0, "arcs": 0}


def test_discover_single_activity_log(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.append_event(
        event_id="EVT0001",
        case_id="C1",
        activity="deep_work",
        timestamp="2026-03-01T09:00:00Z",
        lifecycle="complete",
    )
    miner = ProcessMiner(repo)
    model = miner.discover_inductive_miner(lens="workday")

    assert model.case_count == 1
    assert model.activity_count == 1
    assert "deep_work" in model.process_tree_repr


def test_discover_filters_by_time_window(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)

    # Window covers only 2026-01-02.
    model = miner.discover_inductive_miner(
        lens="workday",
        since="2026-01-02T00:00:00Z",
        until="2026-01-03T00:00:00Z",
    )
    assert model.case_count == 1
    assert model.extractor_metadata["since"] == "2026-01-02T00:00:00Z"
    assert model.extractor_metadata["until"] == "2026-01-03T00:00:00Z"


def test_discover_extractor_metadata_includes_pm4py_version(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    model = miner.discover_inductive_miner(lens="workday")

    assert "pm4py_version" in model.extractor_metadata
    assert isinstance(model.extractor_metadata["pm4py_version"], str)
    assert model.extractor_metadata["pm4py_version"]  # non-empty


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def test_conformance_perfect_replay_returns_fitness_one(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)

    model = miner.discover_inductive_miner(lens="workday")
    result = miner.conformance_token_replay(model, lens="workday")

    assert isinstance(result, ConformanceResult)
    assert result.trace_count == 3
    assert result.fitting_traces == 3
    assert result.aggregate_fitness == pytest.approx(1.0)
    for fit in result.trace_fitness_per_case.values():
        assert fit == pytest.approx(1.0)


def test_conformance_against_alien_log_lower_fitness(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)

    # Inject a 4th workday with a sequence the IM has never seen.
    repo.append_event(
        event_id="EVT9001",
        case_id="C9001",
        activity="alien_activity_1",
        timestamp="2026-01-04T10:00:00Z",
        lifecycle="complete",
    )
    repo.append_event(
        event_id="EVT9002",
        case_id="C9002",
        activity="alien_activity_2",
        timestamp="2026-01-04T11:00:00Z",
        lifecycle="complete",
    )

    miner = ProcessMiner(repo)
    # Discover from the known sequence A,B,C only.
    model = miner.discover_inductive_miner(
        lens="workday",
        until="2026-01-04T00:00:00Z",
    )
    # Conform against the *full* log including the alien day.
    result = miner.conformance_token_replay(model, lens="workday")

    assert result.trace_count == 4
    assert result.aggregate_fitness < 1.0
    # The alien trace must score strictly less than 1.
    assert result.trace_fitness_per_case["2026-01-04"] < 1.0


def test_conformance_returns_per_trace_fitness_dict(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    model = miner.discover_inductive_miner(lens="workday")

    result = miner.conformance_token_replay(model, lens="workday")

    assert set(result.trace_fitness_per_case.keys()) == {
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    }
    for v in result.trace_fitness_per_case.values():
        assert isinstance(v, float)


def test_conformance_empty_log_returns_zeroed_result(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    miner = ProcessMiner(repo)
    model = miner.discover_inductive_miner(lens="workday")

    result = miner.conformance_token_replay(model, lens="workday")

    assert result.trace_count == 0
    assert result.fitting_traces == 0
    assert result.aggregate_fitness == 0.0
    assert result.trace_fitness_per_case == {}


def test_conformance_rehydration_fallback_flagged(tmp_path: Path) -> None:
    """When model window events are redacted, fallback flag must be True."""
    import sqlite3

    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)

    # Discover from a narrow window (only 2026-01-01).
    model = miner.discover_inductive_miner(
        lens="workday",
        since="2026-01-01T00:00:00Z",
        until="2026-01-02T00:00:00Z",
    )

    # Redact all events that fall inside the model's originating window by
    # deleting them directly from the database (simulates GC / redaction).
    db_path = tmp_path / "tm.db"
    with sqlite3.connect(str(db_path)) as conn:
        conn.execute(
            "DELETE FROM events WHERE timestamp >= '2026-01-01T00:00:00Z'"
            " AND timestamp < '2026-01-02T00:00:00Z'"
        )
        conn.commit()

    # Days 2 and 3 are still in the DB; they form the replay target.
    # The model's window is empty → fallback fires.
    result = miner.conformance_token_replay(model, lens="workday")

    assert result.extractor_metadata["rehydration_fallback_used"] is True


def test_conformance_no_fallback_flag_false(tmp_path: Path) -> None:
    """When model window events are intact, fallback flag must be False."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)

    model = miner.discover_inductive_miner(lens="workday")
    result = miner.conformance_token_replay(model, lens="workday")

    assert result.extractor_metadata["rehydration_fallback_used"] is False


# ---------------------------------------------------------------------------
# Variants
# ---------------------------------------------------------------------------


def test_variants_groups_identical_sequences(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)

    analysis = miner.analyze_variants(lens="workday")

    assert isinstance(analysis, VariantAnalysis)
    assert analysis.total_cases == 3
    assert analysis.distinct_variants == 1
    assert len(analysis.variants) == 1
    only_variant = analysis.variants[0]
    assert isinstance(only_variant, Variant)
    assert only_variant.sequence == ("A", "B", "C")
    assert only_variant.case_count == 3
    assert only_variant.case_ids == (
        "2026-01-01",
        "2026-01-02",
        "2026-01-03",
    )


def test_variants_orders_by_frequency_desc(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # 2 cases with A, B, C; 1 case with A, C.
    plan = [
        ("2026-04-01", ["A", "B", "C"]),
        ("2026-04-02", ["A", "B", "C"]),
        ("2026-04-03", ["A", "C"]),
    ]
    eid = 0
    for date, seq in plan:
        for hour, activity in enumerate(seq, start=10):
            eid += 1
            repo.append_event(
                event_id=f"VAR{eid:04d}",
                case_id=f"VC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
            )
    miner = ProcessMiner(repo)
    analysis = miner.analyze_variants(lens="workday")

    assert analysis.distinct_variants == 2
    assert analysis.variants[0].sequence == ("A", "B", "C")
    assert analysis.variants[0].case_count == 2
    assert analysis.variants[1].sequence == ("A", "C")
    assert analysis.variants[1].case_count == 1


def test_variants_top_n_truncates(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    plan = [
        ("2026-05-01", ["A"]),
        ("2026-05-02", ["B"]),
        ("2026-05-03", ["C"]),
    ]
    eid = 0
    for date, seq in plan:
        for hour, activity in enumerate(seq, start=10):
            eid += 1
            repo.append_event(
                event_id=f"TOP{eid:04d}",
                case_id=f"TC{eid}",
                activity=activity,
                timestamp=f"{date}T{hour:02d}:00:00Z",
                lifecycle="complete",
            )
    miner = ProcessMiner(repo)
    analysis = miner.analyze_variants(lens="workday", top_n=2)
    assert len(analysis.variants) == 2
    # distinct_variants is the *true* total even when truncated.
    assert analysis.distinct_variants == 3
    assert analysis.total_cases == 3


def test_variants_empty_log_returns_zero_variants(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    miner = ProcessMiner(repo)
    analysis = miner.analyze_variants(lens="workday")
    assert analysis.variants == ()
    assert analysis.total_cases == 0
    assert analysis.distinct_variants == 0


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------


def test_performance_returns_per_activity_durations(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)  # 1-hour gaps between A, B, C.
    miner = ProcessMiner(repo)

    perf = miner.analyze_performance(lens="workday")

    assert isinstance(perf, PerformanceAnalysis)
    by_act = {m.activity: m for m in perf.activities}
    assert "A" in by_act and "B" in by_act and "C" in by_act
    # A precedes B by 3600s; B precedes C by 3600s; both should report 3600 avg.
    assert by_act["A"].avg_duration_seconds == pytest.approx(3600.0)
    assert by_act["B"].avg_duration_seconds == pytest.approx(3600.0)
    # C is the last activity in every case; no successor -> no recorded duration.
    assert by_act["C"].avg_duration_seconds is None
    assert by_act["C"].occurrence_count == 0
    # Total throughput per case is 2 hours.
    assert perf.total_throughput_seconds_avg == pytest.approx(7200.0)


def test_performance_handles_single_case(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    repo.append_event(
        event_id="P1",
        case_id="P1",
        activity="A",
        timestamp="2026-06-01T10:00:00Z",
        lifecycle="complete",
    )
    repo.append_event(
        event_id="P2",
        case_id="P2",
        activity="B",
        timestamp="2026-06-01T10:30:00Z",
        lifecycle="complete",
    )
    miner = ProcessMiner(repo)
    perf = miner.analyze_performance(lens="workday")

    assert perf.total_throughput_seconds_avg == pytest.approx(1800.0)
    by_act = {m.activity: m for m in perf.activities}
    assert by_act["A"].avg_duration_seconds == pytest.approx(1800.0)


def test_performance_orders_by_avg_duration_desc(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # case 1: A (10:00) -> B (10:10) -> C (11:10) — A=600s, B=3600s.
    seq = [
        ("A", "10:00:00"),
        ("B", "10:10:00"),
        ("C", "11:10:00"),
    ]
    for i, (activity, hms) in enumerate(seq, start=1):
        repo.append_event(
            event_id=f"OD{i}",
            case_id=f"O{i}",
            activity=activity,
            timestamp=f"2026-07-01T{hms}Z",
            lifecycle="complete",
        )

    miner = ProcessMiner(repo)
    perf = miner.analyze_performance(lens="workday")

    activities_with_durs = [m for m in perf.activities if m.avg_duration_seconds]
    # B (3600s) should rank above A (600s).
    names_in_order = [m.activity for m in activities_with_durs]
    assert names_in_order.index("B") < names_in_order.index("A")


def test_performance_empty_log(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    miner = ProcessMiner(repo)
    perf = miner.analyze_performance(lens="workday")
    assert perf.activities == ()
    assert perf.edges == ()
    assert perf.total_throughput_seconds_avg is None


# ---------------------------------------------------------------------------
# Conversion (helper)
# ---------------------------------------------------------------------------


def test_workday_conversion_skips_empty_case_date() -> None:
    events = [
        {
            "event_id": "E1",
            "case_id": "X",
            "activity": "A",
            "timestamp": "2026-01-01T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "",  # sentinel from T-PM-01 — must be excluded
            "case_goal_id": None,
        },
        {
            "event_id": "E2",
            "case_id": "Y",
            "activity": "B",
            "timestamp": "2026-01-02T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-01-02",
            "case_goal_id": None,
        },
    ]
    df = _events_to_dataframe(events, lens="workday")
    assert len(df) == 1
    assert df["case:concept:name"].iloc[0] == "2026-01-02"


def test_goal_pursuit_conversion_skips_null_case_goal_id() -> None:
    events = [
        {
            "event_id": "E1",
            "case_id": "X",
            "activity": "A",
            "timestamp": "2026-01-01T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-01-01",
            "case_goal_id": None,
        },
        {
            "event_id": "E2",
            "case_id": "Y",
            "activity": "B",
            "timestamp": "2026-01-02T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-01-02",
            "case_goal_id": "GOAL01",
        },
    ]
    df = _events_to_dataframe(events, lens="goal_pursuit")
    assert len(df) == 1
    assert df["case:concept:name"].iloc[0] == "GOAL01"


def test_conversion_sorts_by_case_id_and_timestamp() -> None:
    events = [
        # Inserted out of order on purpose.
        {
            "event_id": "E2",
            "case_id": "X",
            "activity": "B",
            "timestamp": "2026-01-02T11:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-01-02",
            "case_goal_id": None,
        },
        {
            "event_id": "E1",
            "case_id": "X",
            "activity": "A",
            "timestamp": "2026-01-01T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-01-01",
            "case_goal_id": None,
        },
        {
            "event_id": "E0",
            "case_id": "X",
            "activity": "A",
            "timestamp": "2026-01-02T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-01-02",
            "case_goal_id": None,
        },
    ]
    df = _events_to_dataframe(events, lens="workday")
    # Within '2026-01-02', A (10:00) precedes B (11:00).  And '2026-01-01'
    # comes before '2026-01-02' in case ordering.
    case_seq = df["case:concept:name"].tolist()
    activity_seq = df["concept:name"].tolist()
    assert case_seq == ["2026-01-01", "2026-01-02", "2026-01-02"]
    assert activity_seq == ["A", "A", "B"]


def test_conversion_uses_iso_t_z_timestamps() -> None:
    events = [
        {
            "event_id": "E1",
            "case_id": "X",
            "activity": "A",
            "timestamp": "2026-08-01T10:00:00Z",
            "lifecycle": "complete",
            "case_date": "2026-08-01",
            "case_goal_id": None,
        },
    ]
    df = _events_to_dataframe(events, lens="workday")
    ts = df["time:timestamp"].iloc[0]
    # Must be tz-aware UTC.
    assert ts.tzinfo is not None
    assert str(ts.tz).upper() == "UTC", f"unexpected tz: {ts.tz}"


def test_conversion_unsupported_lens_raises() -> None:
    with pytest.raises(ValueError, match="unsupported lens"):
        _events_to_dataframe([], lens="bogus")  # type: ignore[arg-type]


def test_conversion_invalid_timestamp_raises() -> None:
    events = [
        {
            "event_id": "E1",
            "case_id": "X",
            "activity": "A",
            "timestamp": "",
            "lifecycle": "complete",
            "case_date": "2026-01-01",
            "case_goal_id": None,
        },
    ]
    with pytest.raises(ValueError, match="invalid timestamp"):
        _events_to_dataframe(events, lens="workday")


# ---------------------------------------------------------------------------
# Extra coverage: PerformanceMetric / Variant frozen-ness
# ---------------------------------------------------------------------------


def test_dataclasses_are_frozen() -> None:
    # Each public dataclass is frozen so callers cannot mutate engine output.
    from dataclasses import FrozenInstanceError

    pm = PerformanceMetric(
        activity="A",
        avg_duration_seconds=1.0,
        median_duration_seconds=1.0,
        occurrence_count=1,
    )
    with pytest.raises(FrozenInstanceError):
        pm.activity = "B"  # type: ignore[misc]

    v = Variant(sequence=("A",), case_count=1, case_ids=("C1",))
    with pytest.raises(FrozenInstanceError):
        v.case_count = 2  # type: ignore[misc]
