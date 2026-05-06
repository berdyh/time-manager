"""Bridge :class:`tm.engines.process_mining.ProcessMiner` output into Kuzu.

T-PM-03 persists the full discovered Petri net structure (places +
transitions + arcs + initial/final markings) into Kuzu so downstream
consumers can replay conformance, walk the graph, and avoid re-mining.
The projection consumes :attr:`DiscoveredModel.petri_net` directly.

Public surface
--------------
- :func:`project_discovered_model_to_kuzu` — given an already-discovered
  :class:`DiscoveredModel`, persist its captured Petri net.
- :func:`rebuild_kuzu_projection` — convenience: discover + project in one
  call.  Intended for the daemon's nightly batch and the operator-triggered
  CLI path that lands later (T-PM-04).
"""

from __future__ import annotations

from typing import Literal

from tm.engines.petri_net import MarkingData, PetriNetData
from tm.engines.process_mining import DiscoveredModel, ProcessMiner
from tm.repositories.events import EventsRepository
from tm.stores.kuzu_store import (
    KuzuStore,
    PersistedModel,
    compute_model_id,
)

__all__ = [
    "PersistedModel",
    "PetriNetData",
    "project_discovered_model_to_kuzu",
    "rebuild_kuzu_projection",
]


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

    Uses the actual ``model.petri_net`` captured during discovery. When
    ``model_id`` is omitted, a deterministic id is generated from the model's
    window metadata via :func:`compute_model_id`.

    Empty windows (no events under the lens) are handled gracefully: an
    empty :class:`PetriNetData` is persisted so callers can still find the
    model row via :meth:`KuzuStore.list_models`.
    """
    # Kept for public API compatibility with the original projection helper.
    _ = events_repo

    meta = model.extractor_metadata or {}
    lens = str(meta.get("lens") or "")
    since = meta.get("since")
    until = meta.get("until")

    if model_id is None:
        model_id = compute_model_id(lens=lens, since=since, until=until)

    net_data = _net_data_for_model(model=model, model_id=model_id)

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


def _net_data_for_model(*, model: DiscoveredModel, model_id: str) -> PetriNetData:
    if model.petri_net is not None:
        return model.petri_net

    if (
        model.case_count > 0
        or model.activity_count > 0
        or any(model.petri_net_summary.values())
    ):
        raise ValueError("DiscoveredModel.petri_net is required for non-empty models")

    return _empty_net(model_id)


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
