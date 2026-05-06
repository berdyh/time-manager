"""Tests for tm.engines.petri_net converters.

The converters bridge PM4Py's mutable Petri net objects with the frozen
``PetriNetData`` dataclass used by Kuzu persistence and process-mining
output. The round-trip must preserve place/transition/arc identity and
marking weights so cached nets can be replayed without re-mining.
"""

from __future__ import annotations

import warnings

from tm.engines.petri_net import (
    ArcData,
    MarkingData,
    PetriNetData,
    PlaceData,
    TransitionData,
    petri_net_data_from_pm4py,
    petri_net_data_to_pm4py,
)

# PM4Py emits noisy DeprecationWarnings; keep test output clean.
warnings.filterwarnings("ignore", category=DeprecationWarning)
warnings.filterwarnings("ignore", category=FutureWarning)


def _build_simple_petri_net_data() -> PetriNetData:
    """A tiny hand-rolled Petri net: source -> t_a -> p1 -> t_b -> sink."""
    places = (
        PlaceData(place_id="source", label="source"),
        PlaceData(place_id="p1", label="p1"),
        PlaceData(place_id="sink", label="sink"),
    )
    transitions = (
        TransitionData(transition_id="t_a", label="A", is_invisible=False),
        TransitionData(transition_id="t_b", label="B", is_invisible=False),
        TransitionData(transition_id="t_tau", label="", is_invisible=True),
    )
    arcs = (
        ArcData(
            source_id="source",
            source_kind="place",
            target_id="t_a",
            target_kind="transition",
            weight=1,
        ),
        ArcData(
            source_id="t_a",
            source_kind="transition",
            target_id="p1",
            target_kind="place",
            weight=1,
        ),
        ArcData(
            source_id="p1",
            source_kind="place",
            target_id="t_b",
            target_kind="transition",
            weight=1,
        ),
        ArcData(
            source_id="t_b",
            source_kind="transition",
            target_id="sink",
            target_kind="place",
            weight=1,
        ),
    )
    initial = MarkingData(
        marking_id="marking::initial",
        kind="initial",
        place_tokens={"source": 1},
    )
    final = MarkingData(
        marking_id="marking::final",
        kind="final",
        place_tokens={"sink": 1},
    )
    return PetriNetData(
        places=places,
        transitions=transitions,
        arcs=arcs,
        initial_marking=initial,
        final_marking=final,
        activities=("A", "B"),
    )


def test_to_pm4py_returns_pm4py_objects() -> None:
    """Reverse converter returns native pm4py PetriNet + Marking instances."""
    from pm4py.objects.petri_net.obj import Marking, PetriNet

    data = _build_simple_petri_net_data()
    net, im, fm = petri_net_data_to_pm4py(data)

    assert isinstance(net, PetriNet)
    assert isinstance(im, Marking)
    assert isinstance(fm, Marking)
    assert {p.name for p in net.places} == {"source", "p1", "sink"}
    assert {t.name for t in net.transitions} == {"t_a", "t_b", "t_tau"}


def test_to_pm4py_preserves_silent_transitions() -> None:
    """Silent transitions round-trip with ``label=None`` per PM4Py convention."""
    data = _build_simple_petri_net_data()
    net, _im, _fm = petri_net_data_to_pm4py(data)
    label_by_name = {t.name: t.label for t in net.transitions}
    assert label_by_name["t_a"] == "A"
    assert label_by_name["t_b"] == "B"
    assert label_by_name["t_tau"] is None


def test_to_pm4py_preserves_arc_weights() -> None:
    """Arc weights survive the reverse conversion."""
    data = _build_simple_petri_net_data()
    weighted_arcs = (
        *data.arcs[:1],
        ArcData(
            source_id=data.arcs[1].source_id,
            source_kind=data.arcs[1].source_kind,
            target_id=data.arcs[1].target_id,
            target_kind=data.arcs[1].target_kind,
            weight=3,
        ),
        *data.arcs[2:],
    )
    weighted = PetriNetData(
        places=data.places,
        transitions=data.transitions,
        arcs=weighted_arcs,
        initial_marking=data.initial_marking,
        final_marking=data.final_marking,
        activities=data.activities,
    )

    net, _im, _fm = petri_net_data_to_pm4py(weighted)
    arc_weights = {(arc.source.name, arc.target.name): arc.weight for arc in net.arcs}
    assert arc_weights[("t_a", "p1")] == 3
    # Unweighted arcs default to 1.
    assert arc_weights[("source", "t_a")] == 1


def test_petri_net_round_trip() -> None:
    """reverse → forward should structurally match the canonical (sorted) form."""
    data = _build_simple_petri_net_data()
    # The forward converter normalises ordering; canonicalise the hand-built
    # fixture the same way before comparing.
    canonical = PetriNetData(
        places=tuple(sorted(data.places, key=lambda p: p.place_id)),
        transitions=tuple(sorted(data.transitions, key=lambda t: t.transition_id)),
        arcs=tuple(
            sorted(
                data.arcs,
                key=lambda a: (
                    a.source_kind,
                    a.source_id,
                    a.target_kind,
                    a.target_id,
                ),
            )
        ),
        initial_marking=data.initial_marking,
        final_marking=data.final_marking,
        activities=data.activities,
    )

    back_net, back_im, back_fm = petri_net_data_to_pm4py(canonical)
    re_forwarded = petri_net_data_from_pm4py(back_net, back_im, back_fm)

    assert re_forwarded == canonical


def test_round_trip_against_pm4py_discovered_net() -> None:
    """Discover via pm4py then forward → reverse → forward should be stable."""
    import pandas as pd
    import pm4py

    df = pd.DataFrame(
        [
            {
                "case:concept:name": f"C{i}",
                "concept:name": act,
                "time:timestamp": pd.Timestamp(f"2026-01-{day:02d}T{hour:02d}:00:00Z"),
                "lifecycle:transition": "complete",
            }
            for day, i in enumerate(range(1, 4), start=1)
            for hour, act in enumerate(("A", "B", "C"), start=10)
        ]
    )
    net, im, fm = pm4py.discover_petri_net_inductive(df)

    forward = petri_net_data_from_pm4py(net, im, fm)
    rebuilt_net, rebuilt_im, rebuilt_fm = petri_net_data_to_pm4py(forward)
    forward2 = petri_net_data_from_pm4py(rebuilt_net, rebuilt_im, rebuilt_fm)

    assert forward == forward2


def test_round_trip_preserves_markings() -> None:
    """Initial and final marking token counts survive the round-trip."""
    data = _build_simple_petri_net_data()
    net, im, fm = petri_net_data_to_pm4py(data)

    im_by_name = {p.name: count for p, count in im.items()}
    fm_by_name = {p.name: count for p, count in fm.items()}
    assert im_by_name == {"source": 1}
    assert fm_by_name == {"sink": 1}
