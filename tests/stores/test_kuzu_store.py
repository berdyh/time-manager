"""Tests for tm.stores.kuzu_store.KuzuStore.

The tests run against a real Kuzu database under :func:`tmp_path` so
schema initialisation, persistence, and round-trip behaviour are all
exercised against the actual library — no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tm.stores.kuzu_store import (
    ArcData,
    KuzuStore,
    MarkingData,
    PetriNetData,
    PlaceData,
    TransitionData,
    compute_model_id,
)

# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _trivial_net() -> PetriNetData:
    """A handcrafted 2-place / 2-transition / 4-arc Petri net.

    Topology::

        source -> tA -> sink
        source -> tB -> sink

    Initial marking: 1 token at ``source``; final marking: 1 token at ``sink``.
    """
    return PetriNetData(
        places=(
            PlaceData(place_id="p_source", label="source"),
            PlaceData(place_id="p_sink", label="sink"),
        ),
        transitions=(
            TransitionData(transition_id="tA", label="A", is_invisible=False),
            TransitionData(transition_id="tB", label="B", is_invisible=False),
        ),
        arcs=(
            ArcData("p_source", "place", "tA", "transition"),
            ArcData("tA", "transition", "p_sink", "place"),
            ArcData("p_source", "place", "tB", "transition"),
            ArcData("tB", "transition", "p_sink", "place"),
        ),
        initial_marking=MarkingData(
            marking_id="im", kind="initial", place_tokens={"p_source": 1}
        ),
        final_marking=MarkingData(
            marking_id="fm", kind="final", place_tokens={"p_sink": 1}
        ),
        activities=("A", "B"),
    )


def _persist_trivial(
    store: KuzuStore,
    *,
    model_id: str = "m1",
    lens: str = "workday",
    since: str | None = "2026-01-01",
    until: str | None = "2026-01-31",
    fitness: float | None = 0.95,
    precision: float | None = 0.85,
    case_count: int = 5,
    activity_count: int = 2,
) -> str:
    """Persist a trivial net under the given metadata; return the model_id."""
    store.persist_model(
        model_id=model_id,
        net_data=_trivial_net(),
        lens=lens,
        since=since,
        until=until,
        fitness=fitness,
        precision=precision,
        case_count=case_count,
        activity_count=activity_count,
    )
    return model_id


# ---------------------------------------------------------------------------
# Schema initialisation
# ---------------------------------------------------------------------------


def test_kuzu_schema_initializes_idempotently(tmp_path: Path) -> None:
    """Opening + reopening the same DB must not error or duplicate schema."""
    db_path = tmp_path / "kg"
    store = KuzuStore(db_path)
    assert store.db_path == db_path
    store.close()

    # Reopen — _ensure_schema runs again with CREATE TABLE IF NOT EXISTS.
    store2 = KuzuStore(db_path)
    # Sanity: a write goes through cleanly.
    _persist_trivial(store2)
    store2.close()


def test_kuzu_store_creates_parent_directory(tmp_path: Path) -> None:
    """The store auto-creates a missing parent dir for the DB path."""
    deep = tmp_path / "nested" / "kuzu"
    store = KuzuStore(deep)
    assert deep.parent.exists()
    store.close()


# ---------------------------------------------------------------------------
# persist_model + read-back
# ---------------------------------------------------------------------------


def test_persist_model_inserts_places_transitions_arcs(tmp_path: Path) -> None:
    """Row counts after persist match the structure of the input net."""
    store = KuzuStore(tmp_path / "kg")
    mid = _persist_trivial(store)

    pn = store.get_petri_net(mid)
    assert pn is not None
    assert len(pn.places) == 2
    assert len(pn.transitions) == 2
    assert len(pn.arcs) == 4


def test_persist_model_returns_persisted_handle(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    persisted = store.persist_model(
        model_id="m1",
        net_data=_trivial_net(),
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
        fitness=1.0,
        precision=0.9,
        case_count=10,
        activity_count=2,
    )
    assert persisted.model_id == "m1"
    assert persisted.lens == "workday"
    assert persisted.since == "2026-01-01"
    assert persisted.until == "2026-01-31"
    assert persisted.fitness == pytest.approx(1.0)
    assert persisted.precision == pytest.approx(0.9)
    assert persisted.case_count == 10
    assert persisted.activity_count == 2
    assert persisted.discovered_at  # ISO timestamp set automatically


def test_persist_model_idempotent(tmp_path: Path) -> None:
    """Re-persisting the same model_id replaces existing rows; counts stay same."""
    store = KuzuStore(tmp_path / "kg")
    mid = _persist_trivial(store)

    # Second call — same id, same net.
    store.persist_model(
        model_id=mid,
        net_data=_trivial_net(),
        lens="workday",
        since="2026-01-01",
        until="2026-01-31",
        fitness=0.99,  # different metric, same id
        precision=0.88,
        case_count=7,
        activity_count=2,
    )

    pn = store.get_petri_net(mid)
    assert pn is not None
    assert len(pn.places) == 2
    assert len(pn.transitions) == 2
    assert len(pn.arcs) == 4

    # Updated metric reflects the second persist.
    persisted = store.get_model(mid)
    assert persisted is not None
    assert persisted.fitness == pytest.approx(0.99)
    assert persisted.case_count == 7


def test_get_petri_net_round_trips(tmp_path: Path) -> None:
    """Full round-trip preserves all structural fields."""
    store = KuzuStore(tmp_path / "kg")
    mid = _persist_trivial(store)
    pn = store.get_petri_net(mid)
    assert pn is not None

    place_ids = sorted(p.place_id for p in pn.places)
    assert place_ids == ["p_sink", "p_source"]
    place_labels = {p.place_id: p.label for p in pn.places}
    assert place_labels["p_sink"] == "sink"
    assert place_labels["p_source"] == "source"

    trans = {t.transition_id: t for t in pn.transitions}
    assert set(trans) == {"tA", "tB"}
    assert trans["tA"].label == "A"
    assert trans["tA"].is_invisible is False
    assert trans["tB"].label == "B"

    # Both directions present.
    arc_pairs = {(a.source_id, a.target_id, a.source_kind) for a in pn.arcs}
    assert ("p_source", "tA", "place") in arc_pairs
    assert ("tA", "p_sink", "transition") in arc_pairs
    assert ("p_source", "tB", "place") in arc_pairs
    assert ("tB", "p_sink", "transition") in arc_pairs


def test_get_petri_net_unknown_model_returns_none(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    assert store.get_petri_net("does-not-exist") is None


def test_persist_marking_round_trips(tmp_path: Path) -> None:
    """Initial / final markings round-trip place_tokens accurately."""
    store = KuzuStore(tmp_path / "kg")
    mid = _persist_trivial(store)

    pn = store.get_petri_net(mid)
    assert pn is not None
    assert pn.initial_marking.kind == "initial"
    assert pn.initial_marking.place_tokens == {"p_source": 1}
    assert pn.final_marking.kind == "final"
    assert pn.final_marking.place_tokens == {"p_sink": 1}


def test_persist_invisible_transition_round_trips(tmp_path: Path) -> None:
    """Silent transitions keep their is_invisible flag and empty label."""
    store = KuzuStore(tmp_path / "kg")
    net = PetriNetData(
        places=(PlaceData("p1", "p1"), PlaceData("p2", "p2")),
        transitions=(
            TransitionData("t_silent", "", is_invisible=True),
            TransitionData("t_visible", "X", is_invisible=False),
        ),
        arcs=(
            ArcData("p1", "place", "t_silent", "transition"),
            ArcData("t_silent", "transition", "p2", "place"),
            ArcData("p1", "place", "t_visible", "transition"),
            ArcData("t_visible", "transition", "p2", "place"),
        ),
        initial_marking=MarkingData("im", "initial", {"p1": 1}),
        final_marking=MarkingData("fm", "final", {"p2": 1}),
        activities=("X",),
    )
    store.persist_model(
        model_id="m_silent",
        net_data=net,
        lens="workday",
        since=None,
        until=None,
        fitness=None,
        precision=None,
        case_count=0,
        activity_count=1,
    )
    pn = store.get_petri_net("m_silent")
    assert pn is not None
    silent = next(t for t in pn.transitions if t.transition_id == "t_silent")
    visible = next(t for t in pn.transitions if t.transition_id == "t_visible")
    assert silent.is_invisible is True
    assert silent.label == ""
    assert visible.is_invisible is False
    # Activities tuple only includes visible labels.
    assert pn.activities == ("X",)


# ---------------------------------------------------------------------------
# list_models
# ---------------------------------------------------------------------------


def test_list_models_returns_all(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    _persist_trivial(store, model_id="m1", lens="workday")
    _persist_trivial(store, model_id="m2", lens="goal_pursuit")
    models = store.list_models()
    assert {m.model_id for m in models} == {"m1", "m2"}


def test_list_models_filters_by_lens(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    _persist_trivial(store, model_id="m_workday", lens="workday")
    _persist_trivial(store, model_id="m_goal", lens="goal_pursuit")

    workday_only = store.list_models(lens="workday")
    assert [m.model_id for m in workday_only] == ["m_workday"]

    goal_only = store.list_models(lens="goal_pursuit")
    assert [m.model_id for m in goal_only] == ["m_goal"]


def test_list_models_filters_by_window(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    _persist_trivial(store, model_id="m_jan", since="2026-01-01", until="2026-01-31")
    _persist_trivial(store, model_id="m_feb", since="2026-02-01", until="2026-02-28")

    jan = store.list_models(since="2026-01-01", until="2026-01-31")
    assert [m.model_id for m in jan] == ["m_jan"]

    feb = store.list_models(since="2026-02-01")
    assert [m.model_id for m in feb] == ["m_feb"]


def test_list_models_empty_store(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    assert store.list_models() == []


# ---------------------------------------------------------------------------
# delete_model
# ---------------------------------------------------------------------------


def test_delete_model_removes_all_traces(tmp_path: Path) -> None:
    """After delete, the model row + its places/transitions/markings vanish."""
    store = KuzuStore(tmp_path / "kg")
    mid = _persist_trivial(store)
    assert store.get_model(mid) is not None

    deleted = store.delete_model(mid)
    assert deleted is True
    assert store.get_model(mid) is None
    assert store.get_petri_net(mid) is None

    # A second delete is a no-op (returns False).
    assert store.delete_model(mid) is False


def test_delete_model_leaves_other_models_intact(tmp_path: Path) -> None:
    store = KuzuStore(tmp_path / "kg")
    _persist_trivial(store, model_id="keep")
    _persist_trivial(store, model_id="discard")

    assert store.delete_model("discard") is True
    remaining = store.list_models()
    assert [m.model_id for m in remaining] == ["keep"]
    pn = store.get_petri_net("keep")
    assert pn is not None
    assert len(pn.places) == 2


# ---------------------------------------------------------------------------
# compute_model_id
# ---------------------------------------------------------------------------


def test_compute_model_id_is_deterministic() -> None:
    a = compute_model_id(lens="workday", since="2026-01-01", until="2026-01-31")
    b = compute_model_id(lens="workday", since="2026-01-01", until="2026-01-31")
    assert a == b
    c = compute_model_id(lens="workday", since="2026-02-01", until="2026-02-28")
    assert a != c


def test_compute_model_id_handles_none_window() -> None:
    mid = compute_model_id(lens="workday", since=None, until=None)
    assert "workday" in mid
