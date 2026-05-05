"""Bridge :class:`tm.engines.process_mining.ProcessMiner` output into Kuzu.

T-PM-02 leaves the discovered Petri net inside PM4Py and only surfaces a
summary count via :class:`DiscoveredModel.petri_net_summary`.  T-PM-03 needs
the full structure (places + transitions + arcs + initial/final markings)
persisted into Kuzu so downstream consumers can replay conformance, walk
the graph, and skip re-mining.

Rather than reach back into T-PM-02 territory (which is locked from this
worker), the bridge re-runs PM4Py's Inductive Miner against the same
``EventsRepository`` window the model was discovered from and extracts the
structure here.  This is wasteful — two discoveries for one persisted
model — and is documented as such; a future T-PM-02 follow-up can expose
the net directly on :class:`DiscoveredModel` and let this module skip the
re-mine entirely.

Public surface
--------------
- :func:`project_discovered_model_to_kuzu` — given an already-discovered
  :class:`DiscoveredModel`, re-build its net + persist it.
- :func:`rebuild_kuzu_projection` — convenience: discover + project in one
  call.  Intended for the daemon's nightly batch and the operator-triggered
  CLI path that lands later (T-PM-04).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Literal

import pm4py

from tm.engines.process_mining import DiscoveredModel, ProcessMiner
from tm.repositories.events import EventsRepository
from tm.stores.kuzu_store import (
    ArcData,
    KuzuStore,
    MarkingData,
    PersistedModel,
    PetriNetData,
    PlaceData,
    TransitionData,
    compute_model_id,
)

if TYPE_CHECKING:  # pragma: no cover - import guard for type checker only
    import pandas as pd

__all__ = [
    "PersistedModel",
    "PetriNetData",
    "project_discovered_model_to_kuzu",
    "rebuild_kuzu_projection",
]


# ---------------------------------------------------------------------------
# PM4Py extraction helpers (private)
# ---------------------------------------------------------------------------


def _pm4py_petri_net_to_data(
    net: Any, im: Any, fm: Any, *, model_id: str
) -> PetriNetData:
    """Translate a PM4Py ``(net, im, fm)`` triple into a :class:`PetriNetData`.

    Place and transition identifiers are taken from PM4Py's stable string
    repr (``Place.name`` / ``Transition.name``) so the round-trip is
    deterministic across runs.  Silent transitions (those PM4Py marks with a
    ``None`` label) are flagged ``is_invisible=True`` and given an empty
    label string in the dataclass — a single representation across both
    visible and invisible transitions keeps the schema simple.

    The two ``model_id`` arguments to :func:`MarkingData` use the marking's
    ``kind`` ("initial" / "final") plus the model id for namespacing, but
    that namespacing is owned by :class:`KuzuStore`; here we only fill in
    the ``kind`` so the store can resolve the rest.
    """
    places = tuple(
        sorted(
            (PlaceData(place_id=str(p.name), label=str(p.name)) for p in net.places),
            key=lambda p: p.place_id,
        )
    )

    transitions: list[TransitionData] = []
    for t in net.transitions:
        label = t.label
        is_invisible = label is None
        transitions.append(
            TransitionData(
                transition_id=str(t.name),
                label=str(label) if label is not None else "",
                is_invisible=is_invisible,
            )
        )
    transitions_t = tuple(sorted(transitions, key=lambda t: t.transition_id))

    arcs: list[ArcData] = []
    for arc in net.arcs:
        src = arc.source
        tgt = arc.target
        # PM4Py distinguishes Place vs Transition by class name; both have
        # ``.name`` available.  Use type-name introspection so we don't have
        # to import the underlying PM4Py classes here.
        src_kind = "place" if type(src).__name__ == "Place" else "transition"
        tgt_kind = "place" if type(tgt).__name__ == "Place" else "transition"
        weight = int(getattr(arc, "weight", 1) or 1)
        arcs.append(
            ArcData(
                source_id=str(src.name),
                source_kind=src_kind,
                target_id=str(tgt.name),
                target_kind=tgt_kind,
                weight=weight,
            )
        )
    arcs.sort(key=lambda a: (a.source_kind, a.source_id, a.target_kind, a.target_id))

    initial_marking = MarkingData(
        marking_id=f"{model_id}::marking::initial",
        kind="initial",
        place_tokens={str(p.name): int(n) for p, n in im.items()},
    )
    final_marking = MarkingData(
        marking_id=f"{model_id}::marking::final",
        kind="final",
        place_tokens={str(p.name): int(n) for p, n in fm.items()},
    )

    activities = tuple(
        sorted({t.label for t in transitions_t if not t.is_invisible and t.label})
    )

    return PetriNetData(
        places=places,
        transitions=transitions_t,
        arcs=tuple(arcs),
        initial_marking=initial_marking,
        final_marking=final_marking,
        activities=activities,
    )


def _load_window_dataframe(
    *, events_repo: EventsRepository, model: DiscoveredModel
) -> pd.DataFrame:
    """Reconstruct the same DataFrame the model was discovered from.

    We borrow :class:`ProcessMiner` (stateless) and ask it to load the
    window described by the model's ``extractor_metadata``.  This keeps the
    re-mine semantics aligned with T-PM-02's lens/window conventions
    without duplicating the dataframe-construction logic.
    """
    miner = ProcessMiner(events_repo)
    meta = model.extractor_metadata or {}
    lens = meta.get("lens")
    if lens not in ("workday", "goal_pursuit"):
        raise ValueError(
            f"DiscoveredModel.extractor_metadata.lens missing or unsupported: {lens!r}"
        )
    return miner._load_dataframe(  # noqa: SLF001 — internal hook by design
        lens=lens,
        since=meta.get("since"),
        until=meta.get("until"),
        case_id=meta.get("case_id"),
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def project_discovered_model_to_kuzu(
    *,
    model: DiscoveredModel,
    events_repo: EventsRepository,
    kuzu_store: KuzuStore,
    model_id: str | None = None,
) -> PersistedModel:
    """Persist the Petri net behind a :class:`DiscoveredModel` into Kuzu.

    Re-mines the same window so the actual net structure can be extracted
    (T-PM-02 only surfaces a summary).  When ``model_id`` is omitted, a
    deterministic id is generated from the model's window metadata via
    :func:`compute_model_id`.

    Empty windows (no events under the lens) are handled gracefully: an
    empty :class:`PetriNetData` is persisted so callers can still find the
    model row via :meth:`KuzuStore.list_models`.
    """
    meta = model.extractor_metadata or {}
    lens = str(meta.get("lens") or "")
    since = meta.get("since")
    until = meta.get("until")

    if model_id is None:
        model_id = compute_model_id(lens=lens, since=since, until=until)

    df = _load_window_dataframe(events_repo=events_repo, model=model)

    if df.empty:
        net_data = _empty_net(model_id)
    else:
        net, im, fm = pm4py.discover_petri_net_inductive(df)
        net_data = _pm4py_petri_net_to_data(net, im, fm, model_id=model_id)

    return kuzu_store.persist_model(
        model_id=model_id,
        net_data=net_data,
        lens=lens,
        since=since,
        until=until,
        fitness=model.fitness,
        precision=model.precision,
        case_count=model.case_count,
        activity_count=model.activity_count,
    )


def rebuild_kuzu_projection(
    *,
    events_repo: EventsRepository,
    kuzu_store: KuzuStore,
    process_miner: ProcessMiner,
    lens: str,
    since: str | None = None,
    until: str | None = None,
) -> PersistedModel:
    """Discover and persist a model in one call.

    Idempotent: subsequent calls with the same window replace any existing
    persisted model derived from that window (the underlying ``model_id``
    is deterministic by :func:`compute_model_id`).
    """
    if lens not in ("workday", "goal_pursuit"):
        raise ValueError(f"unsupported lens: {lens!r}")

    # Narrow the literal so mypy is satisfied with ProcessMiner.lens=.
    typed_lens: Literal["workday", "goal_pursuit"] = (
        "workday" if lens == "workday" else "goal_pursuit"
    )
    model = process_miner.discover_inductive_miner(
        lens=typed_lens, since=since, until=until
    )
    return project_discovered_model_to_kuzu(
        model=model,
        events_repo=events_repo,
        kuzu_store=kuzu_store,
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _empty_net(model_id: str) -> PetriNetData:
    """Build a structurally-valid empty :class:`PetriNetData`.

    Used when the window is empty so :func:`project_discovered_model_to_kuzu`
    still inserts a Model row (callers can later detect the empty state via
    ``activity_count == 0`` or by reading back :meth:`KuzuStore.get_petri_net`
    and observing the empty places/transitions tuples).
    """
    return PetriNetData(
        places=(),
        transitions=(),
        arcs=(),
        initial_marking=MarkingData(
            marking_id=f"{model_id}::marking::initial",
            kind="initial",
            place_tokens={},
        ),
        final_marking=MarkingData(
            marking_id=f"{model_id}::marking::final",
            kind="final",
            place_tokens={},
        ),
        activities=(),
    )
