"""Tests for tm.engines.scheduler_metric.SchedulerSuccessMetric (T-OUT-03).

Uses real :class:`SuggestionTelemetryRepository` and real
:class:`OutcomeAggregator` against a tmp-path SQLite database.  Fixtures
seed deterministic suggestion + actual_outcome data so each metric is
asserted with exact expected numerics.
"""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from tm.engines.scheduler_metric import (
    SchedulerMetricSummary,
    SchedulerSuccessMetric,
)
from tm.models.outcome import OutcomeAggregator
from tm.repositories.events import EventsRepository
from tm.repositories.telemetry import SuggestionTelemetryRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_metric(
    tmp_path: Path,
) -> tuple[SchedulerSuccessMetric, SuggestionTelemetryRepository]:
    """Apply migrations on a tmp DB and return a metric + telemetry repo."""
    from tm.store import Store

    db = tmp_path / "tm.db"
    store = Store(db, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()

    telemetry = SuggestionTelemetryRepository(db)
    aggregator = OutcomeAggregator(EventsRepository(db))
    return SchedulerSuccessMetric(telemetry, aggregator), telemetry


def _seed_suggestions(
    telemetry: SuggestionTelemetryRepository,
) -> dict[str, str]:
    """Seed five suggestions; populate actuals on three; thumbs on two.

    Layout:

    +-----+------+-------+--------+--------+--------+----------+
    | row | with | wo    | delta  | actual | thumbs | hit?     |
    +-----+------+-------+--------+--------+--------+----------+
    |  1  | 2.0  | 1.0   | +1.0   |   2    | up     | yes      |
    |  2  | 1.5  | 0.5   | +1.0   |   1    | down   | yes      |
    |  3  | 1.0  | 0.5   | +0.5   |   0    | None   | no       |
    |  4  | 1.5  | 1.0   | +0.5   | None   | None   | excluded |
    |  5  | 0.5  | 0.0   | +0.5   | None   | None   | excluded |
    +-----+------+-------+--------+--------+--------+----------+

    Mean predicted_delta over rows-with-actual (1, 2, 3) = (1+1+0.5)/3 = 0.833...
    Mean actual_outcome = (2+1+0)/3 = 1.0
    Mean actual − predicted_with = ((2-2) + (1-1.5) + (0-1)) / 3 = -0.5
    Hit rate = 2/3 (rows 1, 2 are hits — actual > predicted_without)

    Returns a mapping ``{tag: suggestion_id}`` so individual tests can
    introspect specific rows by stable nickname.
    """
    rows: dict[str, str] = {}

    rec1 = telemetry.log_suggestion(
        case_date="2026-05-01",
        recommended_action="block_social",
        predicted_outcome_with=2.0,
        predicted_outcome_without=1.0,
    )
    rows["row1"] = rec1.suggestion_id
    telemetry.record_actual_outcome(rec1.suggestion_id, 2)
    telemetry.record_thumbs(rec1.suggestion_id, True)

    rec2 = telemetry.log_suggestion(
        case_date="2026-05-02",
        recommended_action="take_break",
        predicted_outcome_with=1.5,
        predicted_outcome_without=0.5,
    )
    rows["row2"] = rec2.suggestion_id
    telemetry.record_actual_outcome(rec2.suggestion_id, 1)
    telemetry.record_thumbs(rec2.suggestion_id, False)

    rec3 = telemetry.log_suggestion(
        case_date="2026-05-03",
        recommended_action="exercise",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    rows["row3"] = rec3.suggestion_id
    telemetry.record_actual_outcome(rec3.suggestion_id, 0)

    rec4 = telemetry.log_suggestion(
        case_date="2026-05-04",
        recommended_action="meditate",
        predicted_outcome_with=1.5,
        predicted_outcome_without=1.0,
    )
    rows["row4"] = rec4.suggestion_id

    rec5 = telemetry.log_suggestion(
        case_date="2026-05-05",
        recommended_action="walk",
        predicted_outcome_with=0.5,
        predicted_outcome_without=0.0,
    )
    rows["row5"] = rec5.suggestion_id

    return rows


# ---------------------------------------------------------------------------
# summarize — empty / partial windows
# ---------------------------------------------------------------------------


def test_summarize_zero_window_returns_zero_counts(tmp_path: Path) -> None:
    metric, _ = _make_metric(tmp_path)
    summary = metric.summarize()
    assert isinstance(summary, SchedulerMetricSummary)
    assert summary.total_suggestions == 0
    assert summary.suggestions_with_actual == 0
    assert summary.mean_predicted_outcome_delta is None
    assert summary.mean_actual_outcome_score is None
    assert summary.mean_actual_minus_predicted_with is None
    assert summary.hit_rate_actual_above_predicted_without is None
    assert summary.thumbs_up_count == 0
    assert summary.thumbs_down_count == 0


def test_summarize_no_actual_outcome_returns_None_for_actual_aggregates(
    tmp_path: Path,
) -> None:
    metric, telemetry = _make_metric(tmp_path)
    # Log suggestions with no actual_outcome populated.
    telemetry.log_suggestion(
        case_date="2026-05-01",
        recommended_action="x",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    telemetry.log_suggestion(
        case_date="2026-05-02",
        recommended_action="y",
        predicted_outcome_with=2.0,
        predicted_outcome_without=1.0,
    )
    summary = metric.summarize()
    assert summary.total_suggestions == 2
    assert summary.suggestions_with_actual == 0
    assert summary.mean_predicted_outcome_delta is None
    assert summary.mean_actual_outcome_score is None
    assert summary.mean_actual_minus_predicted_with is None
    assert summary.hit_rate_actual_above_predicted_without is None


# ---------------------------------------------------------------------------
# summarize — populated metrics
# ---------------------------------------------------------------------------


def test_summarize_computes_mean_predicted_delta(tmp_path: Path) -> None:
    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)

    summary = metric.summarize()
    assert summary.total_suggestions == 5
    assert summary.suggestions_with_actual == 3
    # (1.0 + 1.0 + 0.5) / 3 = 0.8333...
    assert summary.mean_predicted_outcome_delta == pytest.approx(2.5 / 3.0)


def test_summarize_computes_mean_actual_outcome(tmp_path: Path) -> None:
    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)
    summary = metric.summarize()
    # (2 + 1 + 0) / 3 = 1.0
    assert summary.mean_actual_outcome_score == pytest.approx(1.0)


def test_summarize_computes_mean_actual_minus_predicted_with(
    tmp_path: Path,
) -> None:
    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)
    summary = metric.summarize()
    # ((2-2) + (1-1.5) + (0-1.0)) / 3 = -0.5
    assert summary.mean_actual_minus_predicted_with == pytest.approx(-0.5)


def test_summarize_hit_rate_calculation(tmp_path: Path) -> None:
    """Rows 1, 2 have actual > predicted_without; row 3 does not → 2/3."""
    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)
    summary = metric.summarize()
    assert summary.hit_rate_actual_above_predicted_without == pytest.approx(2.0 / 3.0)


def test_summarize_thumbs_counts(tmp_path: Path) -> None:
    """1 thumbs up, 1 thumbs down."""
    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)
    summary = metric.summarize()
    assert summary.thumbs_up_count == 1
    assert summary.thumbs_down_count == 1


# ---------------------------------------------------------------------------
# per_suggestion_diagnostics
# ---------------------------------------------------------------------------


def test_per_suggestion_diagnostics_filters_to_rows_with_actual(
    tmp_path: Path,
) -> None:
    metric, telemetry = _make_metric(tmp_path)
    rows = _seed_suggestions(telemetry)

    diagnostics = metric.per_suggestion_diagnostics()
    assert len(diagnostics) == 3
    sids = {d["suggestion_id"] for d in diagnostics}
    assert rows["row1"] in sids
    assert rows["row2"] in sids
    assert rows["row3"] in sids
    assert rows["row4"] not in sids
    assert rows["row5"] not in sids


def test_per_suggestion_diagnostics_computes_actual_minus_predicted_with(
    tmp_path: Path,
) -> None:
    metric, telemetry = _make_metric(tmp_path)
    rows = _seed_suggestions(telemetry)

    diagnostics = metric.per_suggestion_diagnostics()
    by_id = {d["suggestion_id"]: d for d in diagnostics}

    # Row 1: actual=2, predicted_with=2.0 → 0
    assert by_id[rows["row1"]]["actual_minus_predicted_with"] == pytest.approx(0.0)
    # Row 2: actual=1, predicted_with=1.5 → -0.5
    assert by_id[rows["row2"]]["actual_minus_predicted_with"] == pytest.approx(-0.5)
    # Row 3: actual=0, predicted_with=1.0 → -1.0
    assert by_id[rows["row3"]]["actual_minus_predicted_with"] == pytest.approx(-1.0)


def test_per_suggestion_diagnostics_marks_hits(tmp_path: Path) -> None:
    metric, telemetry = _make_metric(tmp_path)
    rows = _seed_suggestions(telemetry)

    diagnostics = metric.per_suggestion_diagnostics()
    by_id = {d["suggestion_id"]: d for d in diagnostics}

    # Row 1: actual 2 > predicted_without 1.0 → hit
    assert by_id[rows["row1"]]["is_hit"] is True
    # Row 2: actual 1 > predicted_without 0.5 → hit
    assert by_id[rows["row2"]]["is_hit"] is True
    # Row 3: actual 0 > predicted_without 0.5 → False
    assert by_id[rows["row3"]]["is_hit"] is False


def test_per_suggestion_diagnostics_carries_recommended_action(
    tmp_path: Path,
) -> None:
    metric, telemetry = _make_metric(tmp_path)
    rows = _seed_suggestions(telemetry)
    diagnostics = metric.per_suggestion_diagnostics()
    by_id = {d["suggestion_id"]: d for d in diagnostics}
    assert by_id[rows["row1"]]["recommended_action"] == "block_social"
    assert by_id[rows["row2"]]["recommended_action"] == "take_break"
    assert by_id[rows["row3"]]["recommended_action"] == "exercise"


# ---------------------------------------------------------------------------
# Window filtering
# ---------------------------------------------------------------------------


def test_window_filters_by_since_until(tmp_path: Path) -> None:
    """summarize() honors since/until on suggested_at."""
    metric, telemetry = _make_metric(tmp_path)
    rec_a = telemetry.log_suggestion(
        case_date="2026-04-01",
        recommended_action="A",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    telemetry.record_actual_outcome(rec_a.suggestion_id, 2)
    # Sleep so suggested_at differs at second granularity.
    time.sleep(1.1)
    rec_b = telemetry.log_suggestion(
        case_date="2026-04-02",
        recommended_action="B",
        predicted_outcome_with=2.0,
        predicted_outcome_without=1.0,
    )
    telemetry.record_actual_outcome(rec_b.suggestion_id, 0)

    # Window covering only the older suggestion.
    until_str = rec_a.suggested_at
    summary = metric.summarize(until=until_str)
    assert summary.total_suggestions == 1
    assert summary.mean_actual_outcome_score == pytest.approx(2.0)

    # Window covering only the newer suggestion.
    summary = metric.summarize(since=rec_b.suggested_at)
    assert summary.total_suggestions == 1
    assert summary.mean_actual_outcome_score == pytest.approx(0.0)


def test_window_filter_applies_to_diagnostics(tmp_path: Path) -> None:
    metric, telemetry = _make_metric(tmp_path)
    rec_a = telemetry.log_suggestion(
        case_date="2026-04-01",
        recommended_action="A",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    telemetry.record_actual_outcome(rec_a.suggestion_id, 2)
    time.sleep(1.1)
    rec_b = telemetry.log_suggestion(
        case_date="2026-04-02",
        recommended_action="B",
        predicted_outcome_with=2.0,
        predicted_outcome_without=1.0,
    )
    telemetry.record_actual_outcome(rec_b.suggestion_id, 1)

    diagnostics = metric.per_suggestion_diagnostics(since=rec_b.suggested_at)
    assert len(diagnostics) == 1
    assert diagnostics[0]["suggestion_id"] == rec_b.suggestion_id


# ---------------------------------------------------------------------------
# Dataclass shape
# ---------------------------------------------------------------------------


def test_summary_is_frozen_dataclass(tmp_path: Path) -> None:
    import dataclasses

    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)
    summary = metric.summarize()
    with pytest.raises(dataclasses.FrozenInstanceError):
        summary.total_suggestions = 99  # type: ignore[misc]


def test_summary_metadata_records_window(tmp_path: Path) -> None:
    metric, telemetry = _make_metric(tmp_path)
    _seed_suggestions(telemetry)
    summary = metric.summarize(
        since="2026-05-01T00:00:00Z", until="2026-05-31T23:59:59Z"
    )
    md = summary.extractor_metadata
    assert md["since"] == "2026-05-01T00:00:00Z"
    assert md["until"] == "2026-05-31T23:59:59Z"
