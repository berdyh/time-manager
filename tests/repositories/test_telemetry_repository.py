"""Tests for SuggestionTelemetryRepository (tm.repositories.telemetry)."""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.repositories.telemetry import SuggestionRecord, SuggestionTelemetryRepository

REPO_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


def _make_repo(db_path: Path) -> SuggestionTelemetryRepository:
    """Apply all migrations (including 0007) and return a repository."""
    from tm.store import Store

    store = Store(db_path, migrations_dir=MIGRATIONS_DIR)
    store.apply_pending_migrations()
    store.close()
    return SuggestionTelemetryRepository(db_path)


# ---------------------------------------------------------------------------
# log_suggestion
# ---------------------------------------------------------------------------


def test_log_suggestion_inserts_and_returns_record(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    rec = repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="block_social_media",
        predicted_outcome_with=1.5,
        predicted_outcome_without=0.8,
    )
    assert isinstance(rec, SuggestionRecord)
    # suggestion_id is auto-generated ULID: 26 chars
    assert len(rec.suggestion_id) == 26
    assert rec.suggestion_id != ""
    assert rec.case_date == "2026-05-05"
    assert rec.case_goal_id is None
    assert rec.recommended_action == "block_social_media"
    assert rec.predicted_outcome_with == 1.5
    assert rec.predicted_outcome_without == 0.8
    assert abs(rec.predicted_outcome_delta - 0.7) < 1e-9
    assert rec.conformance_deviation is None
    assert rec.actual_outcome is None
    assert rec.explicit_thumbs is None
    assert rec.llm_explanation_text is None
    assert rec.suggested_at != ""
    assert rec.created_at != ""


def test_log_suggestion_with_explicit_id(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    custom_id = "MYEXPLICITID0000000000000"
    rec = repo.log_suggestion(
        suggestion_id=custom_id,
        case_date="2026-05-05",
        recommended_action="take_break",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    assert rec.suggestion_id == custom_id


def test_log_suggestion_computes_delta(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    rec = repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="exercise",
        predicted_outcome_with=1.5,
        predicted_outcome_without=0.5,
    )
    assert abs(rec.predicted_outcome_delta - 1.0) < 1e-9


def test_log_suggestion_validates_outcome_range(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    with pytest.raises(ValueError, match="predicted_outcome_with"):
        repo.log_suggestion(
            case_date="2026-05-05",
            recommended_action="exercise",
            predicted_outcome_with=2.5,
            predicted_outcome_without=0.5,
        )


def test_log_suggestion_validates_recommended_action_non_empty(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    with pytest.raises(ValueError, match="recommended_action"):
        repo.log_suggestion(
            case_date="2026-05-05",
            recommended_action="",
            predicted_outcome_with=1.0,
            predicted_outcome_without=0.5,
        )


# ---------------------------------------------------------------------------
# record_actual_outcome
# ---------------------------------------------------------------------------


def test_record_actual_outcome_updates_row(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    rec = repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="meditate",
        predicted_outcome_with=1.2,
        predicted_outcome_without=0.6,
    )
    assert rec.actual_outcome is None
    repo.record_actual_outcome(rec.suggestion_id, 2)
    updated = repo.get(rec.suggestion_id)
    assert updated is not None
    assert updated.actual_outcome == 2


def test_record_actual_outcome_validates_value(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    rec = repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="meditate",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    with pytest.raises(ValueError):
        repo.record_actual_outcome(rec.suggestion_id, 3)


def test_record_actual_outcome_unknown_id_raises(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    with pytest.raises(ValueError, match="unknown suggestion_id"):
        repo.record_actual_outcome("NONEXISTENT00000000000000", 1)


# ---------------------------------------------------------------------------
# record_thumbs
# ---------------------------------------------------------------------------


def test_record_thumbs_stores_0_or_1(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    rec = repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="walk",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.4,
    )
    # thumbs-up stores 1
    repo.record_thumbs(rec.suggestion_id, True)
    updated = repo.get(rec.suggestion_id)
    assert updated is not None
    assert updated.explicit_thumbs == 1

    # thumbs-down stores 0
    repo.record_thumbs(rec.suggestion_id, False)
    updated2 = repo.get(rec.suggestion_id)
    assert updated2 is not None
    assert updated2.explicit_thumbs == 0


def test_record_thumbs_unknown_id_raises(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    with pytest.raises(ValueError, match="unknown suggestion_id"):
        repo.record_thumbs("NONEXISTENT00000000000000", True)


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


def test_get_returns_none_for_unknown(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    result = repo.get("DOESNOTEXIST0000000000000")
    assert result is None


# ---------------------------------------------------------------------------
# list_recent
# ---------------------------------------------------------------------------


def test_list_recent_orders_by_suggested_at_desc(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    # Insert three suggestions; we don't control exact timestamps but
    # suggestion_id tie-break (DESC) will order them deterministically.
    r1 = repo.log_suggestion(
        case_date="2026-05-01",
        recommended_action="action_a",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    r2 = repo.log_suggestion(
        case_date="2026-05-02",
        recommended_action="action_b",
        predicted_outcome_with=1.1,
        predicted_outcome_without=0.4,
    )
    r3 = repo.log_suggestion(
        case_date="2026-05-03",
        recommended_action="action_c",
        predicted_outcome_with=1.2,
        predicted_outcome_without=0.3,
    )
    records = repo.list_recent()
    assert len(records) == 3
    ids = [r.suggestion_id for r in records]
    # All IDs present
    assert set(ids) == {r1.suggestion_id, r2.suggestion_id, r3.suggestion_id}
    # Verify descending order (suggested_at DESC, suggestion_id DESC)
    for i in range(len(records) - 1):
        assert (records[i].suggested_at, records[i].suggestion_id) >= (
            records[i + 1].suggested_at,
            records[i + 1].suggestion_id,
        )


def test_list_recent_respects_window(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    # Manually insert rows with fixed suggested_at values
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "tm.db"))
    conn.execute(
        "INSERT INTO suggestion_telemetry "
        "(suggestion_id, suggested_at, case_date, recommended_action, "
        "predicted_outcome_with, predicted_outcome_without, predicted_outcome_delta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("ID_OLD", "2026-01-01T00:00:00Z", "2026-01-01", "action_old", 1.0, 0.5, 0.5),
    )
    conn.execute(
        "INSERT INTO suggestion_telemetry "
        "(suggestion_id, suggested_at, case_date, recommended_action, "
        "predicted_outcome_with, predicted_outcome_without, predicted_outcome_delta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "ID_NEW",
            "2026-05-05T10:00:00Z",
            "2026-05-05",
            "action_new",
            1.5,
            0.8,
            0.7,
        ),
    )
    conn.commit()
    conn.close()

    results = repo.list_recent(since="2026-03-01T00:00:00Z")
    assert len(results) == 1
    assert results[0].suggestion_id == "ID_NEW"

    results_all = repo.list_recent(until="2026-12-31T00:00:00Z")
    assert len(results_all) == 2


def test_list_recent_respects_limit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    for i in range(5):
        repo.log_suggestion(
            case_date="2026-05-05",
            recommended_action=f"action_{i}",
            predicted_outcome_with=1.0,
            predicted_outcome_without=0.5,
        )
    results = repo.list_recent(limit=3)
    assert len(results) == 3


# ---------------------------------------------------------------------------
# delta_outcome_summary
# ---------------------------------------------------------------------------


def test_delta_outcome_summary_zero_rows(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    summary = repo.delta_outcome_summary(since="2026-01-01T00:00:00Z")
    assert summary["count"] == 0
    assert summary["count_with_actual"] == 0
    assert summary["mean_predicted_delta"] is None
    assert summary["mean_actual_minus_predicted"] is None


def test_delta_outcome_summary_some_rows_with_actual_others_without(
    tmp_path: Path,
) -> None:
    repo = _make_repo(tmp_path / "tm.db")

    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "tm.db"))
    # Row 1: has actual_outcome=2, predicted_delta=1.0
    conn.execute(
        "INSERT INTO suggestion_telemetry "
        "(suggestion_id, suggested_at, case_date, recommended_action, "
        "predicted_outcome_with, predicted_outcome_without, predicted_outcome_delta, "
        "actual_outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("SUM_01", "2026-05-05T08:00:00Z", "2026-05-05", "act1", 1.5, 0.5, 1.0, 2),
    )
    # Row 2: has actual_outcome=0, predicted_delta=0.5
    conn.execute(
        "INSERT INTO suggestion_telemetry "
        "(suggestion_id, suggested_at, case_date, recommended_action, "
        "predicted_outcome_with, predicted_outcome_without, predicted_outcome_delta, "
        "actual_outcome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("SUM_02", "2026-05-05T09:00:00Z", "2026-05-05", "act2", 1.0, 0.5, 0.5, 0),
    )
    # Row 3: no actual_outcome
    conn.execute(
        "INSERT INTO suggestion_telemetry "
        "(suggestion_id, suggested_at, case_date, recommended_action, "
        "predicted_outcome_with, predicted_outcome_without, predicted_outcome_delta) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        ("SUM_03", "2026-05-05T10:00:00Z", "2026-05-05", "act3", 1.2, 0.8, 0.4),
    )
    conn.commit()
    conn.close()

    summary = repo.delta_outcome_summary(since="2026-05-01T00:00:00Z")
    assert summary["count"] == 3
    assert summary["count_with_actual"] == 2
    # mean predicted delta over rows with actual: (1.0 + 0.5) / 2 = 0.75
    assert abs(summary["mean_predicted_delta"] - 0.75) < 1e-9
    # mean actual minus predicted: ((2-1.0) + (0-0.5)) / 2 = 0.25
    assert abs(summary["mean_actual_minus_predicted"] - 0.25) < 1e-9


# ---------------------------------------------------------------------------
# count_for_case_date
# ---------------------------------------------------------------------------


def test_count_for_case_date_returns_zero_for_missing_date(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")

    assert repo.count_for_case_date("2026-05-05") == 0


def test_count_for_case_date_counts_only_matching_date(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path / "tm.db")
    repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="action_a",
        predicted_outcome_with=1.0,
        predicted_outcome_without=0.5,
    )
    repo.log_suggestion(
        case_date="2026-05-05",
        recommended_action="action_b",
        predicted_outcome_with=1.2,
        predicted_outcome_without=0.4,
    )
    repo.log_suggestion(
        case_date="2026-05-06",
        recommended_action="action_c",
        predicted_outcome_with=1.1,
        predicted_outcome_without=0.6,
    )

    assert repo.count_for_case_date("2026-05-05") == 2
    assert repo.count_for_case_date("2026-05-06") == 1
