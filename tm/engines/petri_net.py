"""Plain-data Petri net structures shared by mining and Kuzu persistence.

The dataclasses mirror the canonical persistence shape used by Kuzu while
living under ``tm.engines`` so process discovery can expose the mined net
without depending on any store implementation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

__all__ = [
    "ArcData",
    "MarkingData",
    "PetriNetData",
    "PlaceData",
    "TransitionData",
    "petri_net_data_from_pm4py",
]


@dataclass(frozen=True)
class PlaceData:
    """A Petri net place. ``place_id`` is unique within its owning model."""

    place_id: str
    label: str


@dataclass(frozen=True)
class TransitionData:
    """A Petri net transition.

    ``is_invisible`` mirrors PM4Py's convention: silent transitions have a
    ``None`` label, which we coerce to an empty ``label`` string and flag
    here.
    """

    transition_id: str
    label: str
    is_invisible: bool


@dataclass(frozen=True)
class ArcData:
    """A Petri net arc.

    Direction is implicit in ``source_kind`` / ``target_kind``; one side is
    always ``"place"`` and the other ``"transition"``.
    """

    source_id: str
    source_kind: str  # "place" or "transition"
    target_id: str
    target_kind: str  # "place" or "transition"
    weight: int = 1


@dataclass(frozen=True)
class MarkingData:
    """An initial or final marking for a Petri net.

    ``place_tokens`` maps ``place_id`` to the integer token count at that
    place under this marking.
    """

    marking_id: str
    kind: str  # "initial" or "final"
    place_tokens: dict[str, int]


@dataclass(frozen=True)
class PetriNetData:
    """A complete Petri net + initial/final markings + activity vocabulary."""

    places: tuple[PlaceData, ...]
    transitions: tuple[TransitionData, ...]
    arcs: tuple[ArcData, ...]
    initial_marking: MarkingData
    final_marking: MarkingData
    activities: tuple[str, ...]


def petri_net_data_from_pm4py(
    net: Any,
    initial_marking: Any,
    final_marking: Any,
    *,
    model_id: str | None = None,
) -> PetriNetData:
    """Translate a PM4Py ``(net, im, fm)`` triple into ``PetriNetData``.

    Place and transition identifiers are taken from PM4Py's stable string
    repr (``Place.name`` / ``Transition.name``) so the round-trip is
    deterministic across runs. Silent transitions (those PM4Py marks with a
    ``None`` label) are flagged ``is_invisible=True`` and given an empty
    label string.
    """
    places = tuple(
        sorted(
            (PlaceData(place_id=str(p.name), label=str(p.name)) for p in net.places),
            key=lambda p: p.place_id,
        )
    )

    transitions: list[TransitionData] = []
    for transition in net.transitions:
        label = transition.label
        transitions.append(
            TransitionData(
                transition_id=str(transition.name),
                label=str(label) if label is not None else "",
                is_invisible=label is None,
            )
        )
    transitions_t = tuple(sorted(transitions, key=lambda t: t.transition_id))

    arcs: list[ArcData] = []
    for arc in net.arcs:
        src = arc.source
        tgt = arc.target
        src_kind = "place" if type(src).__name__ == "Place" else "transition"
        tgt_kind = "place" if type(tgt).__name__ == "Place" else "transition"
        arcs.append(
            ArcData(
                source_id=str(src.name),
                source_kind=src_kind,
                target_id=str(tgt.name),
                target_kind=tgt_kind,
                weight=int(getattr(arc, "weight", 1) or 1),
            )
        )
    arcs.sort(key=lambda a: (a.source_kind, a.source_id, a.target_kind, a.target_id))

    prefix = f"{model_id}::" if model_id else ""
    initial = MarkingData(
        marking_id=f"{prefix}marking::initial",
        kind="initial",
        place_tokens={str(p.name): int(n) for p, n in initial_marking.items()},
    )
    final = MarkingData(
        marking_id=f"{prefix}marking::final",
        kind="final",
        place_tokens={str(p.name): int(n) for p, n in final_marking.items()},
    )

    activities = tuple(
        sorted({t.label for t in transitions_t if not t.is_invisible and t.label})
    )

    return PetriNetData(
        places=places,
        transitions=transitions_t,
        arcs=tuple(arcs),
        initial_marking=initial,
        final_marking=final,
        activities=activities,
    )
