"""Tests for tm.vocab_alignment.VocabAligner (T-VOC-02).

All LLM calls are mocked via :class:`unittest.mock.Mock` — no network. Each
test uses an isolated SQLite database under ``tmp_path`` with the full
migration chain applied via the :class:`tm.store.Store` facade.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import Mock

import pytest

from tm.repositories.events import EventsRepository
from tm.repositories.vocabulary import VocabularyRepository
from tm.store import Store
from tm.vocab_alignment import (
    ALIGNMENT_SCHEMA,
    AlignmentResult,
    VocabAligner,
)
from tm.vocab_alignment_errors import AlignmentError

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS_DIR = REPO_ROOT / "migrations"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> Path:
    path = tmp_path / "vocab_align.db"
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
def events_repo(db_path: Path) -> EventsRepository:
    return EventsRepository(db_path)


def _make_aligner(
    vocab_repo: VocabularyRepository,
    *,
    extract_return: dict[str, object] | None = None,
    extract_side_effect: object | None = None,
) -> tuple[VocabAligner, Mock]:
    """Wire a VocabAligner with a Mock LLMClient.

    Either ``extract_return`` or ``extract_side_effect`` may be supplied;
    leave both ``None`` to require tests to set ``llm.extract`` themselves.
    """
    llm = Mock()
    if extract_side_effect is not None:
        llm.extract.side_effect = extract_side_effect
    elif extract_return is not None:
        llm.extract.return_value = extract_return
    aligner = VocabAligner(vocab_repo, llm)
    return aligner, llm


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# ---------------------------------------------------------------------------
# align(): repo / alias hits skip the LLM
# ---------------------------------------------------------------------------


def test_align_repo_hit_returns_canonical(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, llm = _make_aligner(vocab_repo)

    result = aligner.align("Deep_Work")

    assert isinstance(result, AlignmentResult)
    assert result.canonical == "deep_work"
    assert result.confidence == 1.0
    assert result.is_novel is False
    assert result.reason == "vocab/alias hit"
    assert result.raw_input == "Deep_Work"
    assert result.normalized_input == "deep_work"
    llm.extract.assert_not_called()


def test_align_alias_hit_returns_canonical(
    vocab_repo: VocabularyRepository,
) -> None:
    # 'workout' → 'exercise' is in STARTER_ALIASES.
    aligner, llm = _make_aligner(vocab_repo)

    result = aligner.align("workout")

    assert result.canonical == "exercise"
    assert result.confidence == 1.0
    assert result.is_novel is False
    llm.extract.assert_not_called()


# ---------------------------------------------------------------------------
# align(): LLM fallback
# ---------------------------------------------------------------------------


def test_align_calls_llm_when_no_repo_hit(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, llm = _make_aligner(
        vocab_repo,
        extract_return={
            "canonical": "exercise",
            "confidence": 0.8,
            "is_novel": False,
            "reason": "similar to workout",
        },
    )

    result = aligner.align("jogging")

    assert result.canonical == "exercise"
    assert result.confidence == pytest.approx(0.8)
    assert result.is_novel is False
    assert result.reason == "similar to workout"
    assert result.normalized_input == "jogging"

    llm.extract.assert_called_once()
    call_kwargs = llm.extract.call_args.kwargs
    assert call_kwargs["schema"] == ALIGNMENT_SCHEMA
    # Prompt must include the active vocabulary so the LLM can pick from it.
    [msg] = call_kwargs["messages"]
    assert msg.role == "user"
    assert "exercise" in msg.content
    assert "jogging" in msg.content


def test_align_marks_novel_when_llm_says_so(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, _ = _make_aligner(
        vocab_repo,
        extract_return={
            "canonical": None,
            "confidence": 0.0,
            "is_novel": True,
            "reason": "no plausible match in vocabulary",
        },
    )

    result = aligner.align("underwater_synchrohypnosis")

    assert result.canonical is None
    assert result.is_novel is True
    assert result.confidence == 0.0
    assert "no plausible" in result.reason


def test_align_rejects_hallucinated_canonical(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, _ = _make_aligner(
        vocab_repo,
        extract_return={
            "canonical": "underwater_basket_weaving",
            "confidence": 0.95,
            "is_novel": False,
            "reason": "looks like a craft hobby",
        },
    )

    result = aligner.align("origami marathon")

    assert result.canonical is None
    assert result.is_novel is True
    # Reason should reference the hallucination context.
    assert "underwater_basket_weaving" in result.reason
    assert "unknown" in result.reason.lower() or "halluc" in result.reason.lower()


# ---------------------------------------------------------------------------
# align(): validation
# ---------------------------------------------------------------------------


def test_align_validates_llm_response_shape(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, _ = _make_aligner(
        vocab_repo,
        extract_return={"canonical": "exercise"},  # missing keys
    )
    with pytest.raises(AlignmentError, match="missing required keys"):
        aligner.align("jogging")


def test_align_validates_confidence_range(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, _ = _make_aligner(
        vocab_repo,
        extract_return={
            "canonical": "exercise",
            "confidence": 1.5,
            "is_novel": False,
            "reason": "too sure",
        },
    )
    with pytest.raises(AlignmentError, match="out of \\[0,1\\] range"):
        aligner.align("jogging")


# ---------------------------------------------------------------------------
# compute_novelty_rate
# ---------------------------------------------------------------------------


def test_compute_novelty_rate_zero_events_returns_zero(
    vocab_repo: VocabularyRepository,
) -> None:
    aligner, _ = _make_aligner(vocab_repo)
    rate = aligner.compute_novelty_rate(
        since="2026-01-01T00:00:00Z",
        until="2026-12-31T23:59:59Z",
    )
    assert rate == 0.0


def test_compute_novelty_rate_all_known(
    vocab_repo: VocabularyRepository,
    events_repo: EventsRepository,
) -> None:
    base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    for i in range(5):
        events_repo.append_event(
            event_id=f"EVT-K-{i:04d}",
            case_id="case-known",
            activity="deep_work",
            timestamp=_iso(base + timedelta(hours=i)),
            lifecycle="complete",
        )

    aligner, _ = _make_aligner(vocab_repo)
    rate = aligner.compute_novelty_rate(
        since="2026-04-01T00:00:00Z",
        until="2026-06-01T00:00:00Z",
    )
    assert rate == 0.0


def test_compute_novelty_rate_mixed(
    vocab_repo: VocabularyRepository,
    events_repo: EventsRepository,
) -> None:
    base = datetime(2026, 5, 1, 9, 0, tzinfo=UTC)
    knowns = ["deep_work", "exercise", "meeting"]
    unknowns = ["scuba_diving", "ferret_grooming"]
    for i, act in enumerate(knowns + unknowns):
        events_repo.append_event(
            event_id=f"EVT-MIX-{i:04d}",
            case_id="case-mix",
            activity=act,
            timestamp=_iso(base + timedelta(hours=i)),
            lifecycle="complete",
        )

    aligner, _ = _make_aligner(vocab_repo)
    rate = aligner.compute_novelty_rate(
        since="2026-04-01T00:00:00Z",
        until="2026-06-01T00:00:00Z",
    )
    assert rate == pytest.approx(2 / 5)


# ---------------------------------------------------------------------------
# find_drifted_activities
# ---------------------------------------------------------------------------


def test_find_drifted_activities_returns_canonicals_with_no_recent_events(
    vocab_repo: VocabularyRepository,
    events_repo: EventsRepository,
) -> None:
    # 'deep_work' has a recent event; 'exercise' does not.
    now = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    events_repo.append_event(
        event_id="EVT-RECENT",
        case_id="case-r",
        activity="deep_work",
        timestamp=_iso(now - timedelta(days=1)),
        lifecycle="complete",
    )

    aligner, _ = _make_aligner(vocab_repo)
    drifted = aligner.find_drifted_activities(idle_days=30, as_of=_iso(now))

    assert "exercise" in drifted
    assert "deep_work" not in drifted


def test_find_drifted_activities_excludes_archived(
    vocab_repo: VocabularyRepository,
) -> None:
    # Archive a canonical with no events; should not appear in drift output.
    vocab_repo.archive("interruption")
    aligner, _ = _make_aligner(vocab_repo)
    drifted = aligner.find_drifted_activities(
        idle_days=30,
        as_of="2026-05-05T12:00:00Z",
    )
    assert "interruption" not in drifted


def test_find_drifted_activities_respects_as_of(
    vocab_repo: VocabularyRepository,
    events_repo: EventsRepository,
) -> None:
    # Events only for two specific canonicals, 60 days before the reference.
    reference = datetime(2026, 5, 5, 12, 0, tzinfo=UTC)
    old_ts = _iso(reference - timedelta(days=60))
    events_repo.append_event(
        event_id="EVT-OLD-DW",
        case_id="case-old",
        activity="deep_work",
        timestamp=old_ts,
        lifecycle="complete",
    )
    events_repo.append_event(
        event_id="EVT-OLD-EX",
        case_id="case-old",
        activity="exercise",
        timestamp=old_ts,
        lifecycle="complete",
    )

    aligner, _ = _make_aligner(vocab_repo)

    # as_of=today: events from 60 days ago count as drifted (idle_days=30).
    drifted_today = aligner.find_drifted_activities(idle_days=30, as_of=_iso(reference))
    assert "deep_work" in drifted_today
    assert "exercise" in drifted_today

    # as_of=70-days-before-reference: cutoff = (today-70) - 30 = today-100.
    # Events at today-60 are AFTER cutoff (within the 30-day window of the
    # earlier anchor) → NOT drifted.
    earlier_anchor = _iso(reference - timedelta(days=70))
    drifted_earlier = aligner.find_drifted_activities(
        idle_days=30, as_of=earlier_anchor
    )
    assert "deep_work" not in drifted_earlier
    assert "exercise" not in drifted_earlier
