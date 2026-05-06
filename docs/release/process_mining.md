# Release: Process mining

**Deliverable:** Process mining (Inductive Miner + conformance + variants + performance + Kuzu projection + CLI)
**Status:** done
**Source plan reference:** `docs/designs/temporal-knowledge-graph.md` (Locked v1 Scope §process-mining; AreaDigest §1)
**Released:** 2026-05-06
**Cluster review:** T-PM-05 (`pass_with_findings` → `promote_to_done`)

---

## Summary

Process-mining engine integration covering the four canonical operations on XES-shaped event logs sourced from the existing `EventsRepository`: Inductive Miner discovery, token-based-replay conformance checking, variant frequency analysis, and per-activity sojourn + DFG performance analysis. Two case lenses are supported (`workday` keyed on `case_date`; `goal_pursuit` keyed on `case_goal_id`) backed by additive XES columns on the events table (migration 0006). Discovered Petri nets project deterministically into a Kuzu graph with full structural round-trip (places, transitions, arcs, initial/final markings, activity vocabulary). Three operator-facing Typer sub-commands (`tm discover`, `tm bottlenecks`, `tm variants`) expose the engine on read-only paths with friendly empty-log handling and no PM4Py type leakage. **Conformance prefers `model.petri_net` and skips re-mining when present** (T-PM-02-v2, commit `2659e64`); falls back to re-mining the originating window only when `petri_net is None`. The `extractor_metadata["rehydration_source"]` field reports which path was taken (`"petri_net_data"` / `"originating_window_remine"` / `"replay_log_fallback"`).

---

## Subtasks rolled up

| Task | Title | Status | Key commits |
|---|---|---|---|
| T-PM-01 | XES-compatible events log columns + case lenses | done | mig `0006_events_xes_columns.sql`, `1f5637a` (sentinel docstring) |
| T-PM-02 | PM4Py wrapper (discover/conformance/variants/performance) | done | `a6ee20d` `0a5b81c` `2bc91d8` `03b213f` `c472a34` |
| T-PM-03 | Kuzu projection of discovered Petri nets | done | `e6ca78a` `436ebc4` `0362b79` `499c946` `7111fbe` |
| T-PM-04 | Operator CLI (tm discover/bottlenecks/variants) | done | `a27c1f5` `92ca615` `ffcfe46` `e089f4e` `18a2d27` |
| T-PM-05 | parent-task review (cluster signoff) | done | (review only) |

---

## Public surface added

### CLI commands
- `tm discover [--lens workday|goal_pursuit] [--db-path] [--since] [--until] [--case-id]` — Inductive Miner discovery; prints process-tree summary + Petri-net counts + fitness/precision.
- `tm bottlenecks [--lens] [--db-path] [--since] [--until]` — performance DFG; per-activity sojourn durations + edge throughput.
- `tm variants [--lens] [--db-path] [--since] [--until] [--top-n]` — distinct activity sequences sorted by frequency.

### APIs (importable)
- `tm.engines.process_mining.ProcessMiner` — `discover_inductive_miner`, `conformance_token_replay`, `analyze_variants`, `analyze_performance`
- Frozen result dataclasses: `DiscoveredModel` (now carries `petri_net: PetriNetData | None`), `ConformanceResult`, `Variant`, `VariantAnalysis`, `PerformanceMetric`, `PerformanceAnalysis`
- `tm.engines.petri_net.{PlaceData, TransitionData, ArcData, MarkingData, PetriNetData, petri_net_data_from_pm4py, petri_net_data_to_pm4py}` — round-trip pm4py ↔ frozen-dataclass converters (canonical home as of commit `09cca89`; reverse converter added in `2659e64`)
- `tm.stores.kuzu_store.KuzuStore` — `persist_model`, `get_model`, `list_models`, `get_petri_net`, `delete_model`
- `tm.stores.kuzu_store.PersistedModel` (frozen dataclass)
- `tm.stores.kuzu_projection.project_discovered_model_to_kuzu`, `rebuild_kuzu_projection`, `compute_model_id`

### Schema migrations
- `migrations/0006_events_xes_columns.sql` — additive: `case_date TEXT NOT NULL DEFAULT ''`, `case_goal_id TEXT`, `vocab_version TEXT`, `schema_version TEXT NOT NULL DEFAULT 'v1'` + 3 partial indexes.

### Runtime dependencies
- `pm4py >= 2.7, < 3` (process mining engine; ~50MB transitive footprint with pandas/numpy/scipy/networkx)
- `kuzu >= 0.11, < 1` (graph DB for persisted Petri nets; ~25MB; pin widened from original 0.3-0.5 because those versions had no wheels for current Python and source builds failed in uv's isolated env)

---

## Tests added

| Path | Coverage |
|---|---|
| `tests/engines/test_process_mining.py` | discovery, variant grouping, performance metrics, conformance with rehydration-fallback flag, empty-log paths |
| `tests/stores/test_kuzu_store.py` | schema idempotency, model CRUD, round-trip preservation, multi-model coexistence via namespacing |
| `tests/stores/test_kuzu_projection.py` | end-to-end ProcessMiner→Kuzu, idempotency on derived model_id, both lenses |
| `tests/commands/test_discover_cli.py` | arg validation, lens errors, empty-log paths, fitness/precision rendering |
| `tests/commands/test_bottlenecks_cli.py` | per-activity output, duration humanizer edge cases, top-N edges |
| `tests/commands/test_variants_cli.py` | top-N truncation, frequency-desc ordering, empty log |

**Project total:** 394 passed + 1 skipped (suite green, ruff clean, mypy clean across 38 source files).

---

## Key decisions and rationale

1. **Frozen dataclasses on every engine return** — keeps PM4Py types from leaking across module boundaries; lets downstream Kuzu projection and CLI treat results as plain serializable Python.
2. **Two named case lenses** (`workday` / `goal_pursuit`) instead of free-form case keys — aligns the events table's two new XES columns (`case_date`, `case_goal_id`) with explicit, type-checked entry points.
3. **Conformance v1 re-mines from the originating window** — avoids a persisted-model dependency for engine-only consumers; gracefully degrades when no rehydration source exists. The `rehydration_fallback_used` flag in `extractor_metadata` surfaces the degradation. T-PM-02 v2 follow-up will rehydrate from KuzuStore.
4. **Kuzu projection re-runs Inductive Miner** instead of reading the net off `DiscoveredModel` — wasteful and pinned for T-PM-02 follow-up; chosen over expanding T-PM-02's public dataclass surface mid-flight.
5. **Model IDs are deterministic** from `(lens, since, until)` — re-projecting the same window replaces the stored model rather than producing duplicates.
6. **CLI is read-only on Kuzu** — persistence is daemon-owned by convention; the operator surface only consumes ProcessMiner outputs. Daemon wiring of `rebuild_kuzu_projection` is deferred to T-INT-02.
7. **Kuzu version pin widened** from the original `>= 0.3, < 0.5` to `>= 0.11, < 1` — those original ranges shipped no wheels for current Python and source builds failed in uv's isolated environment; 0.11.x ships prebuilt wheels with stable API. Documented in `pyproject.toml` inline comment + the `e6ca78a` commit message.

---

## Risks and known limitations

| Risk | Severity | Resolution path |
|---|---|---|
| ~~**Conformance does not yet rehydrate from Kuzu**~~ | ~~non-blocking~~ | **RESOLVED** in commit `2659e64` — conformance prefers `model.petri_net` (populated automatically by `discover_inductive_miner`); reverse converter `petri_net_data_to_pm4py` enables operator-side rehydration via `KuzuStore.get_petri_net(model_id)`. `extractor_metadata["rehydration_source"]` reports which path was taken. |
| ~~**Kuzu projection is currently dead code at the operator level**~~ | ~~non-blocking~~ | **RESOLVED** in commit `d234e26` — `rebuild_kuzu_projection` exposed as daemon RPC handler invokable from cron/systemd timer. |
| ~~**`kuzu_projection` borrows `ProcessMiner._load_dataframe`** (private, with `# noqa: SLF001`)~~ | ~~non-blocking~~ | **RESOLVED** in Tier-A bundle (commit `09cca89`): `PetriNetData` now first-class on `DiscoveredModel` via new `tm/engines/petri_net.py` module; projection consumes `model.petri_net` directly, no re-mine, `# noqa: SLF001` removed. |
| **Kuzu writer ownership is by convention only** — no cross-process locking | non-blocking | Safe for single-daemon v1 deployments; revisit if multiple writers ever land |
| **Performance output for terminal activities reports `avg=n/a, count=0`** because sojourn time is credited to non-terminal events; visually surprising for short cases | informational | Algorithmically correct; document in operator-facing readme if confusion emerges |
| **Empty-string `case_date` sentinel** (`''` default in migration 0006) | informational | Documented in `tm/repositories/events.py` module docstring; partial index on `case_date <> ''` keeps it from bloating queries |

---

## Migration / upgrade notes

Run `apply_pending_migrations()` to install **migration 0006** (XES columns: `case_date`, `case_goal_id`, `vocab_version`, `schema_version`) before invoking any process-mining command. Existing rows back-fill to `''` (`case_date`), `NULL` (`case_goal_id`, `vocab_version`), and `'v1'` (`schema_version`). Indexes on `case_date` and `case_goal_id` are partial — filtered to non-empty / non-null — so they don't bloat from back-filled defaults.

**Runtime dep additions:** `pm4py >= 2.7, < 3` (~50MB transitive) and `kuzu >= 0.11, < 1` (~25MB). Cold `pip install` will take ~30-60s longer than baseline.

---

## Next-step pointers

- ~~**T-PM-02 follow-up:** surface `PetriNetData` on `DiscoveredModel` directly~~ — **DONE** (Tier-A bundle, commit `09cca89`); `tm/engines/petri_net.py` is the canonical home.
- ~~**T-PM-02 v2 follow-up:** rehydrate conformance from cached `PetriNetData` instead of re-mining~~ — **DONE** (commit `2659e64`); `petri_net_data_to_pm4py` reverse converter + `model.petri_net` fast-path; `extractor_metadata["rehydration_source"]` exposes the path taken.
- ~~**T-INT-02 daemon wiring:** call `rebuild_kuzu_projection` from the daemon~~ — **DONE** (commit `d234e26`); RPC handler invokable from external cron/systemd timer.
- ~~**T-INT-02 scheduler agent:** consume `ProcessMiner` outputs as scheduling signals~~ — **DONE** earlier in v1; SchedulerAgent at `tm/agents/scheduler.py` uses `analyze_variants`, `analyze_performance`, and `conformance_token_replay`.
- **Optional:** weighted-arc regression test (current tests use default `weight=1`).
- **Open (Tier S):** multiday variant clustering — scheduler windows 14 days but `VariantClusterer` only labels single cases.
