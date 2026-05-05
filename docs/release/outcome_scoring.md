# Release: Outcome scoring

**Deliverable:** Outcome scoring (daily outcome + variant cluster labels + scheduler success metric)
**Status:** done
**Source plan reference:** `docs/designs/temporal-knowledge-graph.md` (Locked v1 Scope §outcome-scoring; AreaDigest §4)
**Released:** 2026-05-06
**Cluster review:** T-OUT-04 (clean `pass`)

---

## Summary

A locked-formula outcome scoring layer that turns each workday or goal-pursuit case into an integer in `{0, 1, 2}` computed as `int(did_complete_tasks) + int(did_advance_goal)`. The score is sourced from event attributes (`planned_tasks_completed / planned_tasks_total >= 0.80`) and `advances_goal` foreign keys. The score then drives two downstream surfaces: variant cluster labels (`good_day` / `mixed` / `bad_day` for the workday lens; `effective_pursuit` / `stuck` for the goal-pursuit lens) and a scheduler success metric that compares predicted-vs-actual outcomes from a new `suggestion_telemetry` table. All three pieces share a single `OutcomeAggregator` and produce identical scoring across the chain; empty inputs return well-formed zero-state objects everywhere.

---

## Subtasks rolled up

| Task | Title | Status | Key commits |
|---|---|---|---|
| T-OUT-01 | Daily outcome scorer (compute_outcome_score + OutcomeAggregator) | done | `14fe4e5` `afd9dc0` |
| T-OUT-02 | Suggestion telemetry storage + migration 0007 | done | `c49a202` `13211bc` `64789d0` |
| T-OUT-03 | Variant cluster labels + scheduler success metric | done | `01af8bb` `d3b9b39` `486eff1` `8d9c19d` `0931b93` |
| T-OUT-04 | parent-task review (cluster signoff) | done | (review only) |

---

## Public surface added

### CLI commands
*(none — outcome scoring is a library layer; the upcoming T-INT-02 scheduler agent will surface it)*

### APIs (importable)
- `tm.models.outcome.compute_outcome_score(events, *, threshold) -> int` — pure function returning 0, 1, or 2
- `tm.models.outcome.compute_outcome_breakdown(events, *, threshold) -> OutcomeBreakdown` — diagnostic breakdown
- `tm.models.outcome.OutcomeBreakdown` — frozen dataclass with `outcome_score`, `did_complete_tasks`, `did_advance_goal`, `planned_tasks_completed`, `planned_tasks_total`, `advancing_goal_event_count`, `case_event_count`
- `tm.models.outcome.OutcomeAggregator(events_repo)` — `for_case_date(d)`, `for_case_goal_id(g)`, `for_date_range(since, until)`
- `tm.models.outcome.DEFAULT_TASK_COMPLETION_THRESHOLD = 0.80`
- `tm.repositories.telemetry.SuggestionTelemetryRepository` — `log_suggestion`, `record_actual_outcome`, `record_thumbs`, `get`, `list_recent`, `delta_outcome_summary`
- `tm.repositories.telemetry.SuggestionRecord` — frozen dataclass
- `tm.engines.variant_cluster.VariantClusterer(events_repo, outcome_aggregator)` — `cluster_workday_variants`, `cluster_goal_pursuit_variants`
- `tm.engines.variant_cluster.LabeledVariant`, `VariantClustering` — frozen dataclasses
- `tm.engines.variant_cluster.GOOD_DAY_THRESHOLD=1.5`, `BAD_DAY_THRESHOLD=0.5`, `EFFECTIVE_OUTCOME_THRESHOLD=1.5`, `EFFECTIVE_THROUGHPUT_MAX=7`
- `tm.engines.scheduler_metric.SchedulerSuccessMetric(telemetry_repo, outcome_aggregator)` — `summarize`, `per_suggestion_diagnostics`
- `tm.engines.scheduler_metric.SchedulerMetricSummary` — frozen dataclass

### Schema migrations
- `migrations/0007_suggestion_telemetry.sql` — `suggestion_telemetry` table (13 columns) + 4 indexes (on `suggested_at`, `case_date`, partial on `case_goal_id`, partial on `actual_outcome`); CHECK constraints on `actual_outcome IN (0,1,2)` and `explicit_thumbs IN (0,1)`.

### Runtime dependencies
*(none added — outcome scoring is pure Python over existing repos)*

---

## Tests added

| Path | Coverage |
|---|---|
| `tests/models/test_outcome.py` | 28 tests — pure scorer + aggregator; boundary cases (mean=1.5, 0.5, 0.0, 2.0, 1.499, 0.499); MAX-across-events behavior; defensive `_coerce_int` (rejects bool/None/garbage to 0) |
| `tests/repositories/test_telemetry_repository.py` | 16 tests — CRUD round-trip, validation (predicted_outcome range, unknown id, value enums), `delta_outcome_summary` |
| `tests/engines/test_variant_cluster.py` | 17 tests — workday + goal-pursuit lens labeling, boundary inclusivity, empty inputs |
| `tests/engines/test_scheduler_metric.py` | 15 tests — `summarize` (mean predicted delta, hit rate, thumbs counts), `per_suggestion_diagnostics` filter |

**Cluster total:** 76 dedicated tests across 4 files. Project total: **394 passed + 1 skipped**, mypy clean, ruff clean.

---

## Key decisions and rationale

1. **Locked formula `int(did_complete) + int(did_advance) ∈ {0, 1, 2}`** — deliberately narrow surface that downstream LLM rationales can quote verbatim without retraining when thresholds shift.
2. **Per-attribute MAX semantics across events in a case** — assumes T-INT-01 will emit a single summary event per `case_date` carrying both `planned_tasks_completed` and `planned_tasks_total`; reduces dependency on event ordering at the cost of a cross-event data-mixing risk if that contract slips. Risk documented as carry-forward.
3. **Booleans rejected in `_coerce_int`** despite `bool` being an `int` subclass — a debrief writing `True` for `planned_tasks_total` is far more likely a bug than literal `1`.
4. **Variant-clusterer thresholds exposed as module constants AND constructor kwargs** — callers can dial them per cohort without forking the class.
5. **Scheduler success metric is engine-resident, not SQL-resident** — keeps actual-vs-predicted math testable in pure Python. The `SuggestionTelemetryRepository.delta_outcome_summary` SQL helper from T-OUT-02 is retained for backward compat but the engine method is now **canonical**.
6. **Empty-string `case_date` sentinel rejected at `OutcomeAggregator.for_case_date` entry** — "unset" must not silently match all events.
7. **Outcome score is computed at read time, not persisted** — repeated calls re-query events. For v1 daily-rollup volumes (N ≤ 30 per range query) this is fine; revisit if perf becomes hot.

---

## Risks and known limitations

| Risk | Severity | Resolution path |
|---|---|---|
| **MAX-across-events for `planned_tasks_completed/total`** can mix event A's completed with event B's total. Mitigated by the T-INT-01 contract (single summary event per day) | medium (silent if contract slips) | T-INT-01 must enforce single-summary-per-case_date emission |
| **Garbage attribute values silently coerce to 0** in `_coerce_int` with no telemetry surface | informational | Future telemetry hook to surface coercion frequency |
| **`advances_goal` uses `is not None`** rather than truthiness — empty string would falsely advance the goal | mitigated | EventsRepository FK validation rejects empty/non-existent goal IDs at the write layer; consider belt-and-braces tightening |
| **`VariantClusterer` and `SchedulerSuccessMetric` independently accept `OutcomeAggregator`** — no API enforcement that callers pass the same instance | non-blocking | Document at T-INT-02 integration site if they need to share state |
| **Two overlapping delta-outcome surfaces** (`SuggestionTelemetryRepository.delta_outcome_summary` SQL helper from T-OUT-02 + `SchedulerSuccessMetric.summarize` engine method from T-OUT-03) | informational | The engine method is canonical; repo helper retained for backward compat |

---

## Migration / upgrade notes

Migration 0007 is **forward-only** and creates a new table — no alterations to existing rows, no backfill required. Apply with the standard `SQLiteStore(db_path).apply_pending_migrations()` entry point.

**No new runtime dependencies added.** Outcome scoring is pure Python over existing repos.

---

## Next-step pointers

- **T-INT-02 scheduler agent** will consume `SchedulerSuccessMetric.summarize` and feed `record_actual_outcome` / `record_thumbs` at day-end.
- **T-INT-01 LLM extractor** must emit a single summary event per `case_date` carrying both `planned_tasks_completed` and `planned_tasks_total` to avoid the MAX-mixing risk.
- **Optional follow-up:** telemetry hook in `_coerce_int` to surface coercion-to-zero events.
- **Optional follow-up:** tighten `did_advance_goal` to a truthiness check or add an explicit non-empty-string check belt-and-braces with the FK constraint.
