"""Tests for tm.stores.kuzu_projection — bridges ProcessMiner to KuzuStore.

These run real PM4Py against a tiny tmp-path SQLite log and a real Kuzu DB
on tmp_path so the full discovery -> projection -> persistence pipeline is
exercised end-to-end.
"""

from __future__ import annotations

import warnings
from pathlib import Path

from tm.engines.process_mining import ProcessMiner
from tm.repositories.events import EventsRepository
from tm.repositories.goals import GoalsRepository
from tm.stores.kuzu_projection import (
    project_discovered_model_to_kuzu,
    rebuild_kuzu_projection,
)
from tm.stores.kuzu_store import KuzuStore, compute_model_id

# Silence PM4Py's noisy DeprecationWarnings + tqdm bars.
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
    """Three workdays each with sequence A, B, C — same pattern as T-PM-02."""
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
    g1 = goals_repo.add(name="Goal Alpha").goal_id
    g2 = goals_repo.add(name="Goal Beta").goal_id
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
# project_discovered_model_to_kuzu
# ---------------------------------------------------------------------------


def test_project_discovered_model_persists_to_kuzu(tmp_path: Path) -> None:
    """End-to-end: discover -> project -> verify model is listed in Kuzu."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    model = miner.discover_inductive_miner(
        lens="workday", since="2026-01-01", until="2026-01-31"
    )
    persisted = project_discovered_model_to_kuzu(
        model=model, events_repo=repo, kuzu_store=kuzu
    )
    assert persisted.model_id in {m.model_id for m in kuzu.list_models()}
    assert persisted.lens == "workday"


def test_project_uses_compute_model_id_when_unspecified(tmp_path: Path) -> None:
    """Deterministic model_id derived from the model's window metadata."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    model = miner.discover_inductive_miner(
        lens="workday", since="2026-01-01", until="2026-01-31"
    )
    persisted = project_discovered_model_to_kuzu(
        model=model, events_repo=repo, kuzu_store=kuzu
    )
    expected = compute_model_id(lens="workday", since="2026-01-01", until="2026-01-31")
    assert persisted.model_id == expected


def test_project_explicit_model_id_is_honoured(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    model = miner.discover_inductive_miner(
        lens="workday", since="2026-01-01", until="2026-01-31"
    )
    persisted = project_discovered_model_to_kuzu(
        model=model,
        events_repo=repo,
        kuzu_store=kuzu,
        model_id="explicit-id-001",
    )
    assert persisted.model_id == "explicit-id-001"
    assert kuzu.get_model("explicit-id-001") is not None


def test_project_empty_window_persists_empty_net(tmp_path: Path) -> None:
    """Empty windows still produce a Model row (with zero places/transitions)."""
    repo = _make_repo(tmp_path)  # no events seeded
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    model = miner.discover_inductive_miner(
        lens="workday", since="2026-01-01", until="2026-01-31"
    )
    persisted = project_discovered_model_to_kuzu(
        model=model, events_repo=repo, kuzu_store=kuzu
    )
    pn = kuzu.get_petri_net(persisted.model_id)
    assert pn is not None
    assert pn.places == ()
    assert pn.transitions == ()
    assert pn.arcs == ()
    assert pn.activities == ()


# ---------------------------------------------------------------------------
# rebuild_kuzu_projection
# ---------------------------------------------------------------------------


def test_rebuild_kuzu_projection_idempotent(tmp_path: Path) -> None:
    """Calling rebuild twice on the same window leaves a single model row."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    first = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
    )
    second = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
    )
    # Same id, same row count.
    assert first.model_id == second.model_id
    models = kuzu.list_models()
    assert [m.model_id for m in models] == [first.model_id]


def test_rebuild_rejects_unsupported_lens(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    import pytest

    with pytest.raises(ValueError):
        rebuild_kuzu_projection(
            events_repo=repo,
            kuzu_store=kuzu,
            process_miner=miner,
            lens="bogus",  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# Lens-specific projections
# ---------------------------------------------------------------------------


def test_project_workday_lens(tmp_path: Path) -> None:
    """Workday lens produces a non-empty Petri net for a 3-day log."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    persisted = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
    )
    pn = kuzu.get_petri_net(persisted.model_id)
    assert pn is not None
    # 3 visible activities + at least source & sink places.
    assert len(pn.places) >= 2
    visible_labels = {t.label for t in pn.transitions if not t.is_invisible}
    assert visible_labels == {"A", "B", "C"}
    assert set(pn.activities) == {"A", "B", "C"}


def test_project_goal_pursuit_lens(tmp_path: Path) -> None:
    """Goal-pursuit lens carries through to the persisted model row."""
    repo, goals_repo = _make_repo_with_goals(tmp_path)
    _seed_goal_log(repo, goals_repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    persisted = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="goal_pursuit",
        since="2026-02-01",
        until="2026-02-28",
    )
    assert persisted.lens == "goal_pursuit"
    pn = kuzu.get_petri_net(persisted.model_id)
    assert pn is not None
    visible = {t.label for t in pn.transitions if not t.is_invisible}
    assert visible == {"X", "Y"}


def test_get_petri_net_after_project_returns_real_structure(
    tmp_path: Path,
) -> None:
    """The persisted net carries non-trivial places/transitions/arcs counts."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    persisted = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
    )
    pn = kuzu.get_petri_net(persisted.model_id)
    assert pn is not None
    assert len(pn.places) > 0
    assert len(pn.transitions) > 0
    assert len(pn.arcs) > 0
    # Markings reference real places.
    assert pn.initial_marking.place_tokens
    assert pn.final_marking.place_tokens
    place_ids = {p.place_id for p in pn.places}
    assert set(pn.initial_marking.place_tokens).issubset(place_ids)
    assert set(pn.final_marking.place_tokens).issubset(place_ids)


def test_project_distinct_windows_create_distinct_models(tmp_path: Path) -> None:
    """Two non-overlapping windows yield two distinct model_ids."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    # Add a second batch of events in a different window.
    eid = 1000
    for hour, act in enumerate(["P", "Q"], start=9):
        eid += 1
        repo.append_event(
            event_id=f"X{eid:04d}",
            case_id=f"D{eid}",
            activity=act,
            timestamp=f"2026-03-15T{hour:02d}:00:00Z",
            lifecycle="complete",
        )
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    a = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
    )
    b = rebuild_kuzu_projection(
        events_repo=repo,
        kuzu_store=kuzu,
        process_miner=miner,
        lens="workday",
        since="2026-03-01",
        until="2026-03-31",
    )
    assert a.model_id != b.model_id
    assert {m.model_id for m in kuzu.list_models()} >= {a.model_id, b.model_id}


def test_project_preserves_fitness_and_precision(tmp_path: Path) -> None:
    """Persisted fitness/precision match the discovered model."""
    repo = _make_repo(tmp_path)
    _seed_workday_log(repo)
    miner = ProcessMiner(repo)
    kuzu = KuzuStore(tmp_path / "kuzu")

    model = miner.discover_inductive_miner(
        lens="workday", since="2026-01-01", until="2026-01-31"
    )
    persisted = project_discovered_model_to_kuzu(
        model=model, events_repo=repo, kuzu_store=kuzu
    )
    assert persisted.fitness == model.fitness
    assert persisted.precision == model.precision
    assert persisted.case_count == model.case_count
    assert persisted.activity_count == model.activity_count
