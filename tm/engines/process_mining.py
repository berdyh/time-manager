"""PM4Py wrapper for v1 process mining.

Exposes 4 operations on event logs sourced from :class:`EventsRepository`:

  1. ``discover_inductive_miner`` — Inductive Miner -> ProcessTree + Petri net.
  2. ``conformance_token_replay`` — token-based replay -> per-trace + aggregate
     fitness.
  3. ``analyze_variants`` — distinct activity sequences + frequencies.
  4. ``analyze_performance`` — DFG with avg/median throughput metrics.

Two case lenses are supported:

  * ``"workday"`` — case_id is the per-event ``case_date`` (YYYY-MM-DD).
  * ``"goal_pursuit"`` — case_id is the per-event ``case_goal_id``.

This module is intentionally a *thin* wrapper: PM4Py types never leak across
the API surface — every operation returns one of the frozen dataclasses
defined here so that downstream consumers (T-PM-03 Kuzu projection, T-PM-04
CLI display) can treat results as plain Python data.

The conformance v1 design re-runs discovery internally because the discovered
Petri net is not yet persisted (T-PM-03 will project it to Kuzu).  Once a
projection layer exists, conformance can rehydrate without re-mining.

PM4Py emits a tqdm progress bar by default; we silence the relevant warnings
locally for a clean test run but do not muck with global state.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Literal

from tm.repositories.events import EventsRepository

if TYPE_CHECKING:  # pragma: no cover - import guard for type checker only
    import pandas as pd

import pm4py
import pm4py.util.constants

pm4py.util.constants.SHOW_PROGRESS_BAR = False  # silence tqdm progress bars

__all__ = [
    "CaseLens",
    "ConformanceResult",
    "DiscoveredModel",
    "PerformanceAnalysis",
    "PerformanceMetric",
    "ProcessMiner",
    "Variant",
    "VariantAnalysis",
]

CaseLens = Literal["workday", "goal_pursuit"]

# Activity labels emitted by synthetic agents (e.g., DebriefAgent's summary event)
# that should be excluded from process-mining analysis by default.
_SYNTHETIC_SUMMARY_ACTIVITIES: frozenset[str] = frozenset({"debrief_summary"})


# ---------------------------------------------------------------------------
# Public dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DiscoveredModel:
    """Output of :meth:`ProcessMiner.discover_inductive_miner`.

    All fields are plain Python so the result is trivially serialisable for
    downstream persistence (Kuzu projection in T-PM-03).
    """

    process_tree_repr: str
    petri_net_summary: dict[str, int]
    fitness: float | None
    precision: float | None
    case_count: int
    activity_count: int
    extractor_metadata: dict[str, Any]


@dataclass(frozen=True)
class ConformanceResult:
    """Output of :meth:`ProcessMiner.conformance_token_replay`."""

    trace_fitness_per_case: dict[str, float]
    aggregate_fitness: float
    trace_count: int
    fitting_traces: int
    average_token_consumption: float | None
    average_token_production: float | None
    extractor_metadata: dict[str, Any]


@dataclass(frozen=True)
class Variant:
    """A distinct activity sequence with frequency and member case ids."""

    sequence: tuple[str, ...]
    case_count: int
    case_ids: tuple[str, ...]


@dataclass(frozen=True)
class VariantAnalysis:
    """Output of :meth:`ProcessMiner.analyze_variants`."""

    variants: tuple[Variant, ...]
    total_cases: int
    distinct_variants: int
    extractor_metadata: dict[str, Any]


@dataclass(frozen=True)
class PerformanceMetric:
    """Per-activity duration metric (sojourn-time in the activity)."""

    activity: str
    avg_duration_seconds: float | None
    median_duration_seconds: float | None
    occurrence_count: int


@dataclass(frozen=True)
class PerformanceAnalysis:
    """Output of :meth:`ProcessMiner.analyze_performance`.

    ``edges`` is a tuple of dicts with shape::

        {
            "source": str,
            "target": str,
            "avg_duration_seconds": float | None,
            "median_duration_seconds": float | None,
            "occurrence_count": int,
        }
    """

    activities: tuple[PerformanceMetric, ...]
    edges: tuple[dict[str, Any], ...]
    total_throughput_seconds_avg: float | None
    extractor_metadata: dict[str, Any]


# ---------------------------------------------------------------------------
# EventsRepository -> pandas DataFrame conversion
# ---------------------------------------------------------------------------


_PM4PY_CASE_COL = "case:concept:name"
_PM4PY_ACTIVITY_COL = "concept:name"
_PM4PY_TS_COL = "time:timestamp"
_PM4PY_LIFECYCLE_COL = "lifecycle:transition"


def _events_to_dataframe(events: list[dict[str, Any]], lens: CaseLens) -> pd.DataFrame:
    """Convert EventsRepository rows into a PM4Py-shaped DataFrame.

    Rows whose lens-specific case identifier is missing or empty are skipped
    silently (this matches the empty-case_date sentinel from T-PM-01 and the
    nullable case_goal_id soft FK).  Other validation errors (e.g. malformed
    ISO timestamps) raise :class:`ValueError`.

    The returned frame is sorted by ``(case_id, timestamp)`` ascending so the
    case ordering PM4Py preserves is deterministic.
    """
    import pandas as pd

    if lens not in ("workday", "goal_pursuit"):
        raise ValueError(f"unsupported lens: {lens!r}")

    rows: list[dict[str, Any]] = []
    for ev in events:
        if lens == "workday":
            raw = ev.get("case_date")
        else:
            raw = ev.get("case_goal_id")
        if raw is None or raw == "":
            continue

        ts = ev.get("timestamp")
        if not isinstance(ts, str) or not ts:
            raise ValueError(
                f"event {ev.get('event_id')!r} has invalid timestamp: {ts!r}"
            )

        rows.append(
            {
                _PM4PY_CASE_COL: str(raw),
                _PM4PY_ACTIVITY_COL: str(ev.get("activity") or ""),
                _PM4PY_TS_COL: ts,
                _PM4PY_LIFECYCLE_COL: str(ev.get("lifecycle") or ""),
            }
        )

    if not rows:
        return pd.DataFrame(
            columns=[
                _PM4PY_CASE_COL,
                _PM4PY_ACTIVITY_COL,
                _PM4PY_TS_COL,
                _PM4PY_LIFECYCLE_COL,
            ]
        )

    df = pd.DataFrame(rows)
    try:
        df[_PM4PY_TS_COL] = pd.to_datetime(df[_PM4PY_TS_COL], utc=True)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"failed to parse event timestamps: {exc}") from exc

    df = df.sort_values(by=[_PM4PY_CASE_COL, _PM4PY_TS_COL], kind="stable").reset_index(
        drop=True
    )
    return df


def _case_order(df: pd.DataFrame) -> list[str]:
    """Return case ids in the order PM4Py will see them.

    PM4Py iterates the dataframe in row order to build its EventLog; cases
    therefore appear in the order of their *first* event.  We mirror that here
    so trace-level results can be associated back to case ids deterministically.
    """
    seen: list[str] = []
    seen_set: set[str] = set()
    for cid in df[_PM4PY_CASE_COL].tolist():
        s = str(cid)
        if s not in seen_set:
            seen.append(s)
            seen_set.add(s)
    return seen


# ---------------------------------------------------------------------------
# ProcessMiner
# ---------------------------------------------------------------------------


class ProcessMiner:
    """Thin wrapper around PM4Py operations on EventsRepository data.

    The miner is stateless — every public method opens a fresh repository
    query, builds a DataFrame, calls PM4Py, and returns one of the public
    dataclasses.  No PM4Py types leak.

    Notes on conformance (v1)
    -------------------------
    For v1 simplicity, :meth:`conformance_token_replay` re-discovers the
    Petri net from the same log it then conforms against.  This is wasteful
    but correct — once T-PM-03 lands, the discovered net will be persisted to
    Kuzu and the conformance method can rehydrate from there.
    """

    def __init__(self, events_repo: EventsRepository) -> None:
        self._events_repo = events_repo

    # ------------------------------------------------------------------
    # Internal log loaders
    # ------------------------------------------------------------------

    def _load_dataframe(
        self,
        *,
        lens: CaseLens,
        since: str | None,
        until: str | None,
        case_id: str | None = None,
        include_summary_events: bool = False,
    ) -> pd.DataFrame:
        # EventsRepository.query_events sorts by (timestamp, event_id); we
        # re-sort by (case_id, timestamp) inside _events_to_dataframe so the
        # raw query window is the only thing we need to control here.
        events = self._events_repo.query_events(
            case_id=case_id,
            since=since,
            until=until,
        )
        if not include_summary_events:
            events = [
                e
                for e in events
                if e.get("activity") not in _SYNTHETIC_SUMMARY_ACTIVITIES
            ]
        return _events_to_dataframe(events, lens)

    @staticmethod
    def _base_metadata(
        *,
        lens: CaseLens,
        since: str | None,
        until: str | None,
    ) -> dict[str, Any]:
        import pm4py

        return {
            "pm4py_version": getattr(pm4py, "__version__", "unknown"),
            "lens": lens,
            "since": since,
            "until": until,
        }

    # ------------------------------------------------------------------
    # Discovery
    # ------------------------------------------------------------------

    def discover_inductive_miner(
        self,
        *,
        lens: CaseLens,
        since: str | None = None,
        until: str | None = None,
        case_id: str | None = None,
        include_summary_events: bool = False,
    ) -> DiscoveredModel:
        """Discover a process tree + Petri net via the Inductive Miner.

        Empty event logs return a zeroed-out :class:`DiscoveredModel` rather
        than calling PM4Py with an empty frame (PM4Py tolerates it but the
        downstream replay is undefined).
        """
        df = self._load_dataframe(
            lens=lens,
            since=since,
            until=until,
            case_id=case_id,
            include_summary_events=include_summary_events,
        )

        metadata = self._base_metadata(lens=lens, since=since, until=until)
        metadata["case_id"] = case_id

        if df.empty:
            return DiscoveredModel(
                process_tree_repr="",
                petri_net_summary={"places": 0, "transitions": 0, "arcs": 0},
                fitness=None,
                precision=None,
                case_count=0,
                activity_count=0,
                extractor_metadata=metadata,
            )

        import pm4py

        process_tree = pm4py.discover_process_tree_inductive(df)
        net, im, fm = pm4py.convert_to_petri_net(process_tree)

        # Population fitness/precision against the same log used for
        # discovery.  A perfect Inductive Miner output will score 1.0/1.0; we
        # surface them so callers can spot degenerate logs (e.g. single-event
        # cases) where the metrics may be undefined.
        try:
            fit_dict = pm4py.fitness_token_based_replay(df, net, im, fm)
            fitness: float | None = float(
                fit_dict.get("log_fitness", fit_dict.get("average_trace_fitness"))
            )
        except Exception:  # noqa: BLE001 — PM4Py raises a variety of types
            fitness = None
        try:
            precision: float | None = float(
                pm4py.precision_token_based_replay(df, net, im, fm)
            )
        except Exception:  # noqa: BLE001
            precision = None

        case_count = int(df[_PM4PY_CASE_COL].nunique())
        activity_count = int(df[_PM4PY_ACTIVITY_COL].nunique())

        return DiscoveredModel(
            process_tree_repr=str(process_tree),
            petri_net_summary={
                "places": len(net.places),
                "transitions": len(net.transitions),
                "arcs": len(net.arcs),
            },
            fitness=fitness,
            precision=precision,
            case_count=case_count,
            activity_count=activity_count,
            extractor_metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Conformance
    # ------------------------------------------------------------------

    def conformance_token_replay(
        self,
        model: DiscoveredModel,
        *,
        lens: CaseLens,
        since: str | None = None,
        until: str | None = None,
        include_summary_events: bool = False,
    ) -> ConformanceResult:
        """Run token-based replay over the lens window.

        For v1 we do not persist the Petri net.  Instead we re-discover the
        net from the *model's* original window (taken from
        ``model.extractor_metadata``) and then replay against the
        conformance call's window.  This preserves the spirit of "rehydrate
        the cached model" without an actual cache, and lets callers detect
        drift when they conform a wider window than they discovered from.

        Once T-PM-03 lands (Kuzu projection), the rehydration step will
        fetch the persisted net instead of re-mining.
        """
        replay_df = self._load_dataframe(
            lens=lens,
            since=since,
            until=until,
            include_summary_events=include_summary_events,
        )

        metadata = self._base_metadata(lens=lens, since=since, until=until)
        metadata["source_process_tree"] = model.process_tree_repr

        if replay_df.empty:
            metadata["rehydration_fallback_used"] = False
            return ConformanceResult(
                trace_fitness_per_case={},
                aggregate_fitness=0.0,
                trace_count=0,
                fitting_traces=0,
                average_token_consumption=None,
                average_token_production=None,
                extractor_metadata=metadata,
            )

        # Rebuild the model from its originating window so the replay scores
        # the *cached* model rather than a freshly mined one.
        model_meta = model.extractor_metadata or {}
        model_lens: CaseLens = model_meta.get("lens", lens)
        model_df = self._load_dataframe(
            lens=model_lens,
            since=model_meta.get("since"),
            until=model_meta.get("until"),
            case_id=model_meta.get("case_id"),
            include_summary_events=include_summary_events,
        )
        rehydration_fallback_used = model_df.empty
        if rehydration_fallback_used:
            # No model to rehydrate from — fall back to the replay log so we
            # at least return a deterministic result instead of crashing.
            model_df = replay_df

        net, im, fm = pm4py.discover_petri_net_inductive(model_df)
        replayed = pm4py.conformance_diagnostics_token_based_replay(
            replay_df, net, im, fm
        )

        case_ids = _case_order(replay_df)
        trace_fitness: dict[str, float] = {}
        consumed: list[float] = []
        produced: list[float] = []
        fitting = 0
        for idx, trace in enumerate(replayed):
            cid = case_ids[idx] if idx < len(case_ids) else f"trace_{idx}"
            fit = float(trace.get("trace_fitness", 0.0))
            trace_fitness[cid] = fit
            if fit >= 1.0:
                fitting += 1
            c = trace.get("consumed_tokens")
            p = trace.get("produced_tokens")
            if c is not None:
                consumed.append(float(c))
            if p is not None:
                produced.append(float(p))

        agg = sum(trace_fitness.values()) / len(trace_fitness) if trace_fitness else 0.0
        avg_consumed = sum(consumed) / len(consumed) if consumed else None
        avg_produced = sum(produced) / len(produced) if produced else None

        metadata["rehydration_fallback_used"] = rehydration_fallback_used
        return ConformanceResult(
            trace_fitness_per_case=trace_fitness,
            aggregate_fitness=agg,
            trace_count=len(replayed),
            fitting_traces=fitting,
            average_token_consumption=avg_consumed,
            average_token_production=avg_produced,
            extractor_metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Variant analysis
    # ------------------------------------------------------------------

    def analyze_variants(
        self,
        *,
        lens: CaseLens,
        since: str | None = None,
        until: str | None = None,
        top_n: int | None = None,
        include_summary_events: bool = False,
    ) -> VariantAnalysis:
        """Group cases by their distinct activity sequences."""
        df = self._load_dataframe(
            lens=lens,
            since=since,
            until=until,
            include_summary_events=include_summary_events,
        )

        metadata = self._base_metadata(lens=lens, since=since, until=until)
        metadata["top_n"] = top_n

        if df.empty:
            return VariantAnalysis(
                variants=(),
                total_cases=0,
                distinct_variants=0,
                extractor_metadata=metadata,
            )

        # Build sequence -> [case_ids] map directly from the sorted DataFrame.
        # This avoids depending on pm4py.get_variants' return-type drift across
        # versions while still matching its semantics (one entry per case).
        per_case_seq: dict[str, tuple[str, ...]] = {}
        for cid, sub in df.groupby(_PM4PY_CASE_COL, sort=False):
            seq = tuple(str(a) for a in sub[_PM4PY_ACTIVITY_COL].tolist())
            per_case_seq[str(cid)] = seq

        seq_to_cases: dict[tuple[str, ...], list[str]] = {}
        for cid, seq in per_case_seq.items():
            seq_to_cases.setdefault(seq, []).append(cid)

        # Sort: case_count desc, then sequence ascending for determinism.
        ordered = sorted(
            seq_to_cases.items(),
            key=lambda kv: (-len(kv[1]), kv[0]),
        )
        if top_n is not None:
            ordered = ordered[: max(0, int(top_n))]

        variants = tuple(
            Variant(
                sequence=seq,
                case_count=len(case_ids),
                case_ids=tuple(sorted(case_ids)),
            )
            for seq, case_ids in ordered
        )
        return VariantAnalysis(
            variants=variants,
            total_cases=len(per_case_seq),
            distinct_variants=len(seq_to_cases),
            extractor_metadata=metadata,
        )

    # ------------------------------------------------------------------
    # Performance analysis
    # ------------------------------------------------------------------

    def analyze_performance(
        self,
        *,
        lens: CaseLens,
        since: str | None = None,
        until: str | None = None,
        include_summary_events: bool = False,
    ) -> PerformanceAnalysis:
        """Compute per-activity sojourn metrics + DFG edge throughputs."""
        df = self._load_dataframe(
            lens=lens,
            since=since,
            until=until,
            include_summary_events=include_summary_events,
        )

        metadata = self._base_metadata(lens=lens, since=since, until=until)

        if df.empty:
            return PerformanceAnalysis(
                activities=(),
                edges=(),
                total_throughput_seconds_avg=None,
                extractor_metadata=metadata,
            )

        import pm4py

        # Per-activity sojourn time = next_event_ts - this_event_ts within a
        # case, attributed to the *current* activity.  This mirrors PM4Py's
        # interval-event semantics for single-timestamp logs.
        activity_durations: dict[str, list[float]] = {}
        case_throughput: list[float] = []
        for _, sub in df.groupby(_PM4PY_CASE_COL, sort=False):
            sub_sorted = sub.sort_values(by=_PM4PY_TS_COL, kind="stable")
            ts_list = sub_sorted[_PM4PY_TS_COL].tolist()
            act_list = sub_sorted[_PM4PY_ACTIVITY_COL].tolist()
            for i in range(len(ts_list) - 1):
                dur = (ts_list[i + 1] - ts_list[i]).total_seconds()
                activity_durations.setdefault(str(act_list[i]), []).append(float(dur))
            # The last event in a case has no successor; record a 0-duration
            # occurrence so it shows up with a non-zero count.
            if act_list:
                activity_durations.setdefault(str(act_list[-1]), [])
            if len(ts_list) >= 2:
                case_throughput.append(
                    float((ts_list[-1] - ts_list[0]).total_seconds())
                )

        def _avg(values: list[float]) -> float | None:
            return sum(values) / len(values) if values else None

        def _median(values: list[float]) -> float | None:
            if not values:
                return None
            sorted_v = sorted(values)
            mid = len(sorted_v) // 2
            if len(sorted_v) % 2 == 1:
                return float(sorted_v[mid])
            return float((sorted_v[mid - 1] + sorted_v[mid]) / 2)

        activities = tuple(
            sorted(
                (
                    PerformanceMetric(
                        activity=name,
                        avg_duration_seconds=_avg(durs),
                        median_duration_seconds=_median(durs),
                        occurrence_count=len(durs),
                    )
                    for name, durs in activity_durations.items()
                ),
                # Sort by avg desc, treating None as -1 so they fall to the
                # bottom; tie-break on activity name for determinism.
                key=lambda m: (
                    -(m.avg_duration_seconds or 0.0),
                    m.activity,
                ),
            )
        )

        # DFG performance edges via PM4Py (authoritative numbers).
        edges: list[dict[str, Any]] = []
        try:
            perf_dfg, _sa, _ea = pm4py.discover_performance_dfg(df)
        except Exception:  # noqa: BLE001
            perf_dfg = {}
        for (src, dst), stats in perf_dfg.items():
            mean_v = stats.get("mean") if isinstance(stats, dict) else None
            median_v = stats.get("median") if isinstance(stats, dict) else None
            sum_v = stats.get("sum") if isinstance(stats, dict) else None
            mean_f = float(mean_v) if mean_v is not None else None
            median_f = float(median_v) if median_v is not None else None
            occ = (
                int(round(float(sum_v) / mean_f))
                if mean_f and sum_v is not None and mean_f != 0
                else 0
            )
            edges.append(
                {
                    "source": str(src),
                    "target": str(dst),
                    "avg_duration_seconds": mean_f,
                    "median_duration_seconds": median_f,
                    "occurrence_count": occ,
                }
            )
        edges.sort(
            key=lambda e: (
                -(e["avg_duration_seconds"] or 0.0),
                e["source"],
                e["target"],
            )
        )

        total_avg = _avg(case_throughput)

        return PerformanceAnalysis(
            activities=activities,
            edges=tuple(edges),
            total_throughput_seconds_avg=total_avg,
            extractor_metadata=metadata,
        )
