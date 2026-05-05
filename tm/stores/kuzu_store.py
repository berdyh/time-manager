"""Kuzu graph DB wrapper for persisted process models.

T-PM-03 introduces a graph projection of the Petri nets discovered by
:class:`tm.engines.process_mining.ProcessMiner`.  Persisting the actual
places/transitions/arcs/markings lets conformance replay rehydrate from a
real cache (rather than re-mining from the originating window) and unlocks
graph-walk style queries downstream (T-PM-04 CLI, T-PM-05 review).

Schema (Kuzu DDL — Cypher-flavoured)::

    CREATE NODE TABLE Activity(name STRING, PRIMARY KEY(name));
    CREATE NODE TABLE Place(place_id STRING, label STRING, PRIMARY KEY(place_id));
    CREATE NODE TABLE Transition(transition_id STRING, label STRING,
        is_invisible BOOL, PRIMARY KEY(transition_id));
    CREATE NODE TABLE Marking(marking_id STRING, kind STRING, model_id STRING,
        PRIMARY KEY(marking_id));
    CREATE NODE TABLE Model(model_id STRING, lens STRING, since STRING,
        until STRING, fitness DOUBLE, precision DOUBLE, case_count INT64,
        activity_count INT64, discovered_at STRING, PRIMARY KEY(model_id));

    CREATE REL TABLE PlaceToTransition(FROM Place TO Transition, weight INT64);
    CREATE REL TABLE TransitionToPlace(FROM Transition TO Place, weight INT64);
    CREATE REL TABLE TransitionLabelsActivity(FROM Transition TO Activity);
    CREATE REL TABLE ModelHasPlace(FROM Model TO Place);
    CREATE REL TABLE ModelHasTransition(FROM Model TO Transition);
    CREATE REL TABLE ModelHasInitialMarking(FROM Model TO Marking);
    CREATE REL TABLE ModelHasFinalMarking(FROM Model TO Marking);
    CREATE REL TABLE MarkingMarksPlace(FROM Marking TO Place, tokens INT64);

Notes
-----
- Node IDs (place / transition / marking / model identifiers) are namespaced
  with the owning ``model_id`` so multiple persisted models can coexist
  without their PM4Py-generated UUIDs colliding.  See
  :func:`_namespace_place` and :func:`_namespace_transition`.
- Idempotent ``persist_model``: existing rows under a ``model_id`` are
  ``DETACH DELETE``'d before re-insert, so callers can safely re-project on
  every run.
- The wrapper deliberately avoids leaking any Kuzu types in its public API;
  consumers only see the frozen :class:`PetriNetData` family of dataclasses.
- Connections are not thread-safe.  Each test/process should construct its
  own :class:`KuzuStore`.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import kuzu

__all__ = [
    "ArcData",
    "KuzuStore",
    "MarkingData",
    "PersistedModel",
    "PetriNetData",
    "PlaceData",
    "TransitionData",
    "compute_model_id",
]


# ---------------------------------------------------------------------------
# Public dataclasses (frozen, no PM4Py types leak)
# ---------------------------------------------------------------------------


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


@dataclass(frozen=True)
class PersistedModel:
    """A ``Model`` row in Kuzu — the durable handle for a persisted Petri net."""

    model_id: str
    lens: str
    since: str | None
    until: str | None
    fitness: float | None
    precision: float | None
    case_count: int
    activity_count: int
    discovered_at: str


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def compute_model_id(*, lens: str, since: str | None, until: str | None) -> str:
    """Deterministic ``model_id`` derived from (lens, since, until).

    Used when callers don't supply an explicit id.  The format is stable
    across runs so re-projecting the same window replaces the existing model.
    """
    parts = [lens or "_", since or "_", until or "_"]
    return "model::" + "::".join(parts)


def _namespace_place(model_id: str, raw: str) -> str:
    """Namespace a PM4Py-side place identifier under its owning model."""
    return f"{model_id}::place::{raw}"


def _namespace_transition(model_id: str, raw: str) -> str:
    """Namespace a PM4Py-side transition identifier under its owning model."""
    return f"{model_id}::transition::{raw}"


def _namespace_marking(model_id: str, kind: str) -> str:
    """Namespace a marking identifier under its owning model."""
    return f"{model_id}::marking::{kind}"


# ---------------------------------------------------------------------------
# Schema DDL — broken out so tests can re-run it easily
# ---------------------------------------------------------------------------


_NODE_TABLES: tuple[str, ...] = (
    """
    CREATE NODE TABLE IF NOT EXISTS Activity(
        name STRING,
        PRIMARY KEY(name)
    );
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Place(
        place_id STRING,
        label STRING,
        PRIMARY KEY(place_id)
    );
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Transition(
        transition_id STRING,
        label STRING,
        is_invisible BOOL,
        PRIMARY KEY(transition_id)
    );
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Marking(
        marking_id STRING,
        kind STRING,
        model_id STRING,
        PRIMARY KEY(marking_id)
    );
    """,
    """
    CREATE NODE TABLE IF NOT EXISTS Model(
        model_id STRING,
        lens STRING,
        since STRING,
        until STRING,
        fitness DOUBLE,
        precision DOUBLE,
        case_count INT64,
        activity_count INT64,
        discovered_at STRING,
        PRIMARY KEY(model_id)
    );
    """,
)

_REL_TABLES: tuple[str, ...] = (
    "CREATE REL TABLE IF NOT EXISTS PlaceToTransition("
    "FROM Place TO Transition, weight INT64);",
    "CREATE REL TABLE IF NOT EXISTS TransitionToPlace("
    "FROM Transition TO Place, weight INT64);",
    "CREATE REL TABLE IF NOT EXISTS TransitionLabelsActivity("
    "FROM Transition TO Activity);",
    "CREATE REL TABLE IF NOT EXISTS ModelHasPlace(FROM Model TO Place);",
    "CREATE REL TABLE IF NOT EXISTS ModelHasTransition(FROM Model TO Transition);",
    "CREATE REL TABLE IF NOT EXISTS ModelHasInitialMarking(FROM Model TO Marking);",
    "CREATE REL TABLE IF NOT EXISTS ModelHasFinalMarking(FROM Model TO Marking);",
    "CREATE REL TABLE IF NOT EXISTS MarkingMarksPlace("
    "FROM Marking TO Place, tokens INT64);",
)


# ---------------------------------------------------------------------------
# KuzuStore
# ---------------------------------------------------------------------------


class KuzuStore:
    """Wraps a :class:`kuzu.Database` and exposes a model-centric API.

    Schema is initialised idempotently on construction; reopening an existing
    DB is a no-op for the schema layer.
    """

    def __init__(self, db_path: Path | str) -> None:
        self._db_path = Path(db_path)
        # Kuzu creates the directory itself but expects the parent to exist.
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._database = kuzu.Database(str(self._db_path))
        self._connection = kuzu.Connection(self._database)
        self._ensure_schema()

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------

    def _ensure_schema(self) -> None:
        """Run idempotent CREATE NODE/REL TABLE statements.

        Kuzu's ``IF NOT EXISTS`` handles repeat invocations cleanly; we still
        wrap the loop so any unexpected failure is reported with the
        offending DDL.
        """
        for ddl in _NODE_TABLES:
            self._execute(ddl)
        for ddl in _REL_TABLES:
            self._execute(ddl)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    @property
    def db_path(self) -> Path:
        return self._db_path

    def close(self) -> None:
        """Drop references to the connection and database.

        Kuzu < 1.0 does not expose explicit ``close`` on the Python objects;
        dropping references and letting the GC reclaim the native handles is
        the documented pattern.
        """
        self._connection = None  # type: ignore[assignment]
        self._database = None  # type: ignore[assignment]

    def persist_model(
        self,
        *,
        model_id: str,
        net_data: PetriNetData,
        lens: str,
        since: str | None,
        until: str | None,
        fitness: float | None,
        precision: float | None,
        case_count: int,
        activity_count: int,
        discovered_at: str | None = None,
    ) -> PersistedModel:
        """Persist a complete model + Petri net structure.

        Idempotent on ``model_id``: any pre-existing rows for the same model
        are removed first, then re-inserted from ``net_data``.  Returns the
        :class:`PersistedModel` handle for the newly inserted row.
        """
        if discovered_at is None:
            discovered_at = datetime.now(UTC).isoformat()

        self._delete_model_rows(model_id)

        # Activities are global (referenced from many models).  We MERGE-in
        # any new activity labels first so transitions can attach to them.
        for act in net_data.activities:
            self._execute(
                "MERGE (a:Activity {name: $name})",
                {"name": act},
            )

        # Model row.
        self._execute(
            "CREATE (m:Model {"
            "model_id: $model_id, lens: $lens, since: $since, until: $until,"
            " fitness: $fitness, precision: $precision,"
            " case_count: $case_count, activity_count: $activity_count,"
            " discovered_at: $discovered_at"
            "})",
            {
                "model_id": model_id,
                "lens": lens,
                "since": since or "",
                "until": until or "",
                "fitness": fitness,
                "precision": precision,
                "case_count": int(case_count),
                "activity_count": int(activity_count),
                "discovered_at": discovered_at,
            },
        )

        # Places.
        for place in net_data.places:
            ns = _namespace_place(model_id, place.place_id)
            self._execute(
                "CREATE (p:Place {place_id: $pid, label: $label})",
                {"pid": ns, "label": place.label},
            )
            self._execute(
                "MATCH (m:Model {model_id: $mid}), (p:Place {place_id: $pid})"
                " CREATE (m)-[:ModelHasPlace]->(p)",
                {"mid": model_id, "pid": ns},
            )

        # Transitions + label edges.
        for transition in net_data.transitions:
            ns = _namespace_transition(model_id, transition.transition_id)
            self._execute(
                "CREATE (t:Transition {"
                "transition_id: $tid, label: $label, is_invisible: $inv})",
                {
                    "tid": ns,
                    "label": transition.label,
                    "inv": bool(transition.is_invisible),
                },
            )
            self._execute(
                "MATCH (m:Model {model_id: $mid}),"
                " (t:Transition {transition_id: $tid})"
                " CREATE (m)-[:ModelHasTransition]->(t)",
                {"mid": model_id, "tid": ns},
            )
            if not transition.is_invisible and transition.label:
                self._execute(
                    "MATCH (t:Transition {transition_id: $tid}),"
                    " (a:Activity {name: $name})"
                    " CREATE (t)-[:TransitionLabelsActivity]->(a)",
                    {"tid": ns, "name": transition.label},
                )

        # Arcs.
        for arc in net_data.arcs:
            self._insert_arc(model_id=model_id, arc=arc)

        # Markings — attach via dedicated rel tables so we can distinguish
        # initial vs final without piggy-backing on the Marking.kind column
        # for retrieval.
        self._insert_marking(
            model_id=model_id,
            marking=net_data.initial_marking,
            attach_rel="ModelHasInitialMarking",
        )
        self._insert_marking(
            model_id=model_id,
            marking=net_data.final_marking,
            attach_rel="ModelHasFinalMarking",
        )

        return PersistedModel(
            model_id=model_id,
            lens=lens,
            since=since,
            until=until,
            fitness=fitness,
            precision=precision,
            case_count=int(case_count),
            activity_count=int(activity_count),
            discovered_at=discovered_at,
        )

    def get_model(self, model_id: str) -> PersistedModel | None:
        """Return the :class:`PersistedModel` row, or ``None`` if absent."""
        rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})"
            " RETURN m.model_id, m.lens, m.since, m.until,"
            " m.fitness, m.precision, m.case_count, m.activity_count,"
            " m.discovered_at",
            {"mid": model_id},
        )
        if not rows:
            return None
        return _row_to_persisted_model(rows[0])

    def list_models(
        self,
        *,
        lens: str | None = None,
        since: str | None = None,
        until: str | None = None,
    ) -> list[PersistedModel]:
        """List models with optional filters.

        Filters are AND-combined.  ``since`` / ``until`` use exact string
        equality (callers can supply normalised ISO dates) rather than range
        semantics; range-style queries are out of scope for v1.
        """
        clauses: list[str] = []
        params: dict[str, object] = {}
        if lens is not None:
            clauses.append("m.lens = $lens")
            params["lens"] = lens
        if since is not None:
            clauses.append("m.since = $since")
            params["since"] = since
        if until is not None:
            clauses.append("m.until = $until")
            params["until"] = until
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        cypher = (
            "MATCH (m:Model) "
            + where
            + " RETURN m.model_id, m.lens, m.since, m.until,"
            + " m.fitness, m.precision, m.case_count, m.activity_count,"
            + " m.discovered_at"
            + " ORDER BY m.model_id"
        )
        rows = self._fetch_all(cypher, params)
        return [_row_to_persisted_model(r) for r in rows]

    def get_petri_net(self, model_id: str) -> PetriNetData | None:
        """Reconstruct the :class:`PetriNetData` for a persisted model.

        Returns ``None`` if the model row doesn't exist.  Round-trips the
        structure faithfully (place/transition ids are de-namespaced before
        return).
        """
        if self.get_model(model_id) is None:
            return None

        # Places.
        place_rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasPlace]->(p:Place)"
            " RETURN p.place_id, p.label ORDER BY p.place_id",
            {"mid": model_id},
        )
        places = tuple(
            PlaceData(
                place_id=_strip_place_ns(model_id, row[0]),
                label=str(row[1]) if row[1] else "",
            )
            for row in place_rows
        )

        # Transitions.
        transition_rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasTransition]->(t:Transition)"
            " RETURN t.transition_id, t.label, t.is_invisible"
            " ORDER BY t.transition_id",
            {"mid": model_id},
        )
        transitions = tuple(
            TransitionData(
                transition_id=_strip_transition_ns(model_id, row[0]),
                label=str(row[1]) if row[1] else "",
                is_invisible=bool(row[2]),
            )
            for row in transition_rows
        )

        # Arcs (place -> transition).
        pt_rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasPlace]->(p:Place),"
            " (m)-[:ModelHasTransition]->(t:Transition),"
            " (p)-[r:PlaceToTransition]->(t)"
            " RETURN p.place_id, t.transition_id, r.weight",
            {"mid": model_id},
        )
        tp_rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasTransition]->(t:Transition),"
            " (m)-[:ModelHasPlace]->(p:Place),"
            " (t)-[r:TransitionToPlace]->(p)"
            " RETURN t.transition_id, p.place_id, r.weight",
            {"mid": model_id},
        )
        arcs: list[ArcData] = []
        for row in pt_rows:
            arcs.append(
                ArcData(
                    source_id=_strip_place_ns(model_id, row[0]),
                    source_kind="place",
                    target_id=_strip_transition_ns(model_id, row[1]),
                    target_kind="transition",
                    weight=_coerce_int(row[2], default=1),
                )
            )
        for row in tp_rows:
            arcs.append(
                ArcData(
                    source_id=_strip_transition_ns(model_id, row[0]),
                    source_kind="transition",
                    target_id=_strip_place_ns(model_id, row[1]),
                    target_kind="place",
                    weight=_coerce_int(row[2], default=1),
                )
            )
        arcs.sort(
            key=lambda a: (a.source_kind, a.source_id, a.target_kind, a.target_id)
        )

        # Markings.
        initial_marking = self._load_marking(
            model_id=model_id, attach_rel="ModelHasInitialMarking", kind="initial"
        )
        final_marking = self._load_marking(
            model_id=model_id, attach_rel="ModelHasFinalMarking", kind="final"
        )

        # Activities — derived from non-invisible transitions to keep them
        # tightly scoped to this model.  (Activities themselves are global in
        # the schema.)
        activity_rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasTransition]->(t:Transition)"
            " -[:TransitionLabelsActivity]->(a:Activity)"
            " RETURN DISTINCT a.name ORDER BY a.name",
            {"mid": model_id},
        )
        activities: tuple[str, ...] = tuple(str(row[0]) for row in activity_rows)

        return PetriNetData(
            places=places,
            transitions=transitions,
            arcs=tuple(arcs),
            initial_marking=initial_marking,
            final_marking=final_marking,
            activities=activities,
        )

    def delete_model(self, model_id: str) -> bool:
        """Remove every Place/Transition/Marking row owned by ``model_id``.

        Returns ``True`` if a model row existed (and was removed),
        ``False`` otherwise.  Activity nodes are intentionally retained
        because they are global across models.
        """
        existed = self.get_model(model_id) is not None
        self._delete_model_rows(model_id)
        return existed

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _insert_arc(self, *, model_id: str, arc: ArcData) -> None:
        weight = int(arc.weight)
        if arc.source_kind == "place" and arc.target_kind == "transition":
            self._execute(
                "MATCH (p:Place {place_id: $pid}),"
                " (t:Transition {transition_id: $tid})"
                " CREATE (p)-[:PlaceToTransition {weight: $w}]->(t)",
                {
                    "pid": _namespace_place(model_id, arc.source_id),
                    "tid": _namespace_transition(model_id, arc.target_id),
                    "w": weight,
                },
            )
        elif arc.source_kind == "transition" and arc.target_kind == "place":
            self._execute(
                "MATCH (t:Transition {transition_id: $tid}),"
                " (p:Place {place_id: $pid})"
                " CREATE (t)-[:TransitionToPlace {weight: $w}]->(p)",
                {
                    "tid": _namespace_transition(model_id, arc.source_id),
                    "pid": _namespace_place(model_id, arc.target_id),
                    "w": weight,
                },
            )
        else:
            raise ValueError(
                f"unsupported arc kinds: {arc.source_kind!r} -> "
                f"{arc.target_kind!r} (expected place<->transition)"
            )

    def _insert_marking(
        self,
        *,
        model_id: str,
        marking: MarkingData,
        attach_rel: str,
    ) -> None:
        marking_id = _namespace_marking(model_id, marking.kind)
        self._execute(
            "CREATE (m:Marking {marking_id: $mid, kind: $kind, model_id: $owner})",
            {
                "mid": marking_id,
                "kind": marking.kind,
                "owner": model_id,
            },
        )
        # Note: relationship type can't be parameterised in Cypher, hence the
        # f-string interpolation.  ``attach_rel`` is internal-only and never
        # touches user input.
        self._execute(
            "MATCH (mod:Model {model_id: $mid}),"
            " (mk:Marking {marking_id: $marking_id})"
            f" CREATE (mod)-[:{attach_rel}]->(mk)",
            {"mid": model_id, "marking_id": marking_id},
        )
        for place_id, tokens in marking.place_tokens.items():
            self._execute(
                "MATCH (mk:Marking {marking_id: $marking_id}),"
                " (p:Place {place_id: $pid})"
                " CREATE (mk)-[:MarkingMarksPlace {tokens: $t}]->(p)",
                {
                    "marking_id": marking_id,
                    "pid": _namespace_place(model_id, place_id),
                    "t": int(tokens),
                },
            )

    def _load_marking(
        self,
        *,
        model_id: str,
        attach_rel: str,
        kind: str,
    ) -> MarkingData:
        # Find the marking node attached via the given relationship type.
        marking_rows = self._fetch_all(
            "MATCH (m:Model {model_id: $mid})"
            f" -[:{attach_rel}]->(mk:Marking)"
            " RETURN mk.marking_id, mk.kind",
            {"mid": model_id},
        )
        if not marking_rows:
            return MarkingData(
                marking_id=_namespace_marking(model_id, kind),
                kind=kind,
                place_tokens={},
            )
        marking_id = str(marking_rows[0][0])
        marking_kind = str(marking_rows[0][1]) if marking_rows[0][1] else kind
        token_rows = self._fetch_all(
            "MATCH (mk:Marking {marking_id: $marking_id})"
            "-[r:MarkingMarksPlace]->(p:Place)"
            " RETURN p.place_id, r.tokens",
            {"marking_id": marking_id},
        )
        place_tokens: dict[str, int] = {}
        for row in token_rows:
            raw_pid = row[0]
            tokens = _coerce_int(row[1], default=0)
            place_tokens[_strip_place_ns(model_id, raw_pid)] = tokens
        return MarkingData(
            marking_id=marking_id,
            kind=marking_kind,
            place_tokens=place_tokens,
        )

    def _delete_model_rows(self, model_id: str) -> None:
        """Best-effort cleanup of all rows owned by ``model_id``.

        We delete in dependency-friendly order using ``DETACH DELETE`` so
        relationships are cleaned up automatically.  Activity nodes are
        global and intentionally untouched.
        """
        # Markings first (they hold rels to places).
        self._execute(
            "MATCH (mk:Marking {model_id: $mid}) DETACH DELETE mk",
            {"mid": model_id},
        )
        # Transitions (and any TransitionLabelsActivity edges).
        self._execute(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasTransition]->(t:Transition)"
            " DETACH DELETE t",
            {"mid": model_id},
        )
        # Places.
        self._execute(
            "MATCH (m:Model {model_id: $mid})-[:ModelHasPlace]->(p:Place)"
            " DETACH DELETE p",
            {"mid": model_id},
        )
        # Finally the model row itself.
        self._execute(
            "MATCH (m:Model {model_id: $mid}) DETACH DELETE m",
            {"mid": model_id},
        )

    def _execute(self, cypher: str, params: dict[str, object] | None = None) -> None:
        """Run a write/DDL statement, discarding the QueryResult."""
        if params is None:
            self._connection.execute(cypher)
        else:
            self._connection.execute(cypher, params)

    def _fetch_all(
        self, cypher: str, params: dict[str, object] | None = None
    ) -> list[list[object]]:
        """Run a read query and materialise its rows into plain lists."""
        if params is None:
            result = self._connection.execute(cypher)
        else:
            result = self._connection.execute(cypher, params)
        # Kuzu's ``execute`` is typed as ``QueryResult | list[QueryResult]``
        # in the stub; for our single-statement queries we always get the
        # scalar variant.  Narrow with an assert so mypy stops complaining.
        assert not isinstance(result, list)
        rows: list[list[object]] = []
        while result.has_next():
            rows.append(list(result.get_next()))
        return rows


# ---------------------------------------------------------------------------
# Row -> dataclass helpers
# ---------------------------------------------------------------------------


def _unblank(value: object) -> str | None:
    """Map the ``""`` sentinel we use for missing ``since/until`` back to None."""
    if value is None:
        return None
    s = str(value)
    return s if s else None


def _to_float_or_none(value: object) -> float | None:
    if value is None:
        return None
    return float(value)  # type: ignore[arg-type]


def _coerce_int(value: object, *, default: int) -> int:
    """Coerce a Kuzu cell value to ``int``, falling back to ``default``.

    Kuzu hands us :class:`object` for typed cells; we know weights/tokens are
    INT64-shaped so a direct ``int()`` is safe whenever the value is non-null.
    """
    if value is None:
        return default
    return int(value)  # type: ignore[call-overload]


def _row_to_persisted_model(row: list[object]) -> PersistedModel:
    return PersistedModel(
        model_id=str(row[0]),
        lens=str(row[1]),
        since=_unblank(row[2]),
        until=_unblank(row[3]),
        fitness=_to_float_or_none(row[4]),
        precision=_to_float_or_none(row[5]),
        case_count=_coerce_int(row[6], default=0),
        activity_count=_coerce_int(row[7], default=0),
        discovered_at=str(row[8]) if row[8] is not None else "",
    )


def _strip_place_ns(model_id: str, namespaced: object) -> str:
    """Reverse :func:`_namespace_place`, returning the original id."""
    s = str(namespaced)
    prefix = f"{model_id}::place::"
    return s[len(prefix) :] if s.startswith(prefix) else s


def _strip_transition_ns(model_id: str, namespaced: object) -> str:
    """Reverse :func:`_namespace_transition`, returning the original id."""
    s = str(namespaced)
    prefix = f"{model_id}::transition::"
    return s[len(prefix) :] if s.startswith(prefix) else s
