# Release: Goals (first-class goal pursuit)

**Deliverable:** Goals (first-class goal pursuit)
**Status:** done
**Source plan reference:** `docs/designs/temporal-knowledge-graph.md` (Locked v1 Scope §goals; Engineering Details Goals)
**Released:** 2026-05-05
**Cluster review:** T-GOAL-04 (`pass_with_findings` → `promote_to_done`)

---

## Summary

Shipped a first-class Goals subsystem with persistent storage, a five-command CLI, and event-level goal-tagging plumbing. The `goals` SQLite table (migration 0003) carries status lifecycle (active/completed/abandoned), priority (1–3), description, and target-completion timestamps. The `tm goal` typer sub-command exposes `add/list/complete/abandon/show` against the standard DB at `~/.local/share/tm/tm.db` (overridable via `--db-path` or `TM_DB`). Events gained a nullable `advances_goal` soft-FK column (migration 0005) with a partial index, and `EventsRepository.append_event` enforces existence application-side until the formal `REFERENCES` lands in T-PM-01's events rebuild. All timestamps persist in the project-canonical `YYYY-MM-DDTHH:MM:SSZ` ISO-8601 UTC format; bare-ISO-date targets are normalized on input. 49 cluster tests pass within the 112-test full suite (1 skipped, 0 failures).

---

## Subtasks rolled up

| Task | Title | Status | Key commits |
|---|---|---|---|
| T-GOAL-01 | goals migration + dataclass + repo | done | `cf7cc7e` `eb7ddbe` `0d9a6ea` `aac199c` |
| T-GOAL-02 | `tm goal` add/list/complete/abandon/show CLI | done | `0103814` `87a807a` `f73504a` |
| T-GOAL-03 | events.advances_goal soft-FK + EventsRepository + timestamp drift fix | done | `411a0d7` `5eeae04` `2e73f15` `5124844` `db5cd33` |
| T-GOAL-04 | parent-task review (cluster signoff) | done | (review only — no commits) |

> **Provenance note:** commit `cf7cc7e` ("feat(migrations): add 0003_goals.sql") was authored by T-GOAL-01 but accidentally swept in `tm/llm/cost_meter.py` (T-FND-04's file) via a shared-index `git add` during parallel execution. Content is correct and unchanged at HEAD. Cosmetic git-history blemish only; no remediation. Future parallel waves enforce explicit `git add <paths>` to prevent recurrence.

---

## Public surface added

### CLI commands
- `tm goal add NAME [--description TEXT] [--priority {1,2,3}] [--target ISO_DATE_OR_DATETIME]`
- `tm goal list [--status {active,completed,abandoned,all}]`
- `tm goal complete <GOAL_ID>`
- `tm goal abandon <GOAL_ID> [--reason TEXT]`
- `tm goal show <GOAL_ID>`

`<GOAL_ID>` is the full 26-char ULID; truncated 12-char display prefixes (used in `list` rendering) are not accepted by `complete`/`abandon`/`show` — explicit choice for v1 to avoid ambiguous-prefix matching.

### APIs (importable)
- `tm.models.goals.Goal` (dataclass; `goal_id, name, description, status, priority, target_completion_at, created_at, completed_at, abandoned_at, abandon_reason`)
- `tm.models.goals.ulid()` — Crockford-base32 26-char IDs; sortable across milliseconds (same-ms collisions are RNG-ordered)
- `tm.repositories.goals.GoalsRepository` — `add`, `complete`, `abandon`, `get`, `list`
- `tm.repositories.events.EventsRepository` — `append_event`, `query_events`, `count_advancing_goal` (the goal-side counter the outcome scorer will consume)
- `tm._paths.default_data_dir()`, `tm._paths.default_db_path()` — XDG-aware filesystem helpers

### Schema migrations
- `migrations/0003_goals.sql` — `goals` table (PK + 9 columns + 2 indexes on `status` and `created_at DESC`)
- `migrations/0005_events_advances_goal.sql` — `events.advances_goal TEXT NULL` + partial index `idx_events_advances_goal WHERE advances_goal IS NOT NULL`

The formal `REFERENCES goals(goal_id)` constraint on `advances_goal` is **deferred to T-PM-01**, which is rebuilding the events table for XES extension and will add the FK as part of that rebuild.

---

## Tests added

| Path | Coverage |
|---|---|
| `tests/models/test_goals.py` | 9 tests — Goal dataclass construction, status validation, priority range, ULID sortability + length |
| `tests/repositories/test_goals_repository.py` | 15 tests — CRUD, lifecycle errors (terminal/unknown), priority + target persistence, sort order, status filtering |
| `tests/repositories/test_goals_timestamp_fix.py` | 1 test — regression: raw `created_at` matches `^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$` (catches SQLite default drift) |
| `tests/repositories/test_events_repository.py` | 11 tests — append_event happy path, attributes JSON round-trip, advances_goal soft-FK validation (positive + negative + None bypass), query filters, count helper |
| `tests/commands/test_goal_cli.py` | 13 tests — full lifecycle via `typer.testing.CliRunner`, error paths (unknown ID, terminal-state guard), invalid priority, target ISO parsing |

**Full suite:** 112 passed + 1 skipped, runtime ~1.0s.

---

## Key decisions and rationale

1. **Soft FK over formal `REFERENCES`** — SQLite's `ALTER TABLE ADD COLUMN` cannot carry a `REFERENCES` constraint, and a full table rebuild would need `PRAGMA foreign_keys=OFF` outside any transaction. The migration runner from T-FND-02 wraps each migration in `BEGIN IMMEDIATE`, which silently no-ops the PRAGMA. Adding rebuild support requires modifying the migration runner — out of scope for the Goals cluster. Instead, `EventsRepository.append_event` enforces existence application-side (`SELECT 1 FROM goals WHERE goal_id = ?` before INSERT). T-PM-01 is already rebuilding events for XES, so it lands the formal FK there.
2. **ULID over UUIDv4 for goal_id** — time-sortable IDs make `ORDER BY created_at, goal_id DESC` a reliable secondary sort and human-readable prefixes work for log grepping. 26-char Crockford-base32, no Pydantic dep.
3. **Plain dataclasses + manual validation over Pydantic v2** — keeps the surface narrow. Anthropic SDK's transitive Pydantic is present, but we don't depend on it explicitly. A future task can convert once the Pydantic pin is project-stable.
4. **Per-call `sqlite3.Connection` in repositories** — avoids long-lived connection state; lets tests pass `tmp_path` DBs without a fixture-managed Store. `PRAGMA foreign_keys=ON` + `journal_mode=WAL` re-applied on every open (mostly idempotent).
5. **Full-ULID required by CLI lookup commands** — explicit choice to avoid ambiguous-prefix matching code in v1. Truncated 12-char IDs in `list` output are display-only.
6. **Timestamp format pinned to `'%Y-%m-%dT%H:%M:%SZ'` across goals and events** — `GoalsRepository.add` now passes `_now_iso()` explicitly rather than relying on SQLite's `datetime('now')` default (which emits `'2026-05-05 18:49:46'` — space separator, no Z). Pinning this prevents downstream string-comparison bugs in `CostMeter.monthly_total`, T-OUT-01's per-case aggregator, and T-INT-01 debrief grouping.

---

## Risks and known limitations

| Risk | Severity | Resolution path |
|---|---|---|
| **Soft-FK TOCTOU** — `SELECT goals` then `INSERT events` is not atomic; a goal abandoned between the two statements would leave an event pointing at an abandoned-but-still-existent goal | low (single-user CLI) | Closes when T-PM-01 lands the formal `REFERENCES` constraint via table rebuild |
| **`goals.name` schema/Python validation drift** — SQL accepts `''` and `'   '` (only NOT NULL); `Goal.__post_init__` rejects them. A direct-SQL bypass insert lands a row that crashes `repo.get()` with ValueError. Confirmed live in T-GOAL-04 review | low (only `GoalsRepository.add` writes the table; goes through `__post_init__`) | New task **T-GOAL-05** (small polish): add `CHECK (length(trim(name)) > 0)` via separate migration OR fold into a future schema-rebuild task |
| **ULID same-ms collisions are RNG-ordered** — within a single millisecond, lexical sort depends on the 80-bit random component | acknowledged | No remediation; same-ms collisions are extremely improbable |
| **Migration log noise** — `SQLiteStore.apply_pending_migrations` prints `applied migration NNNN` lines on every CLI invocation when the DB has pending migrations (one-time per fresh DB; silent on already-up-to-date) | informational | None; acceptable for v1 |
| **Three-write-path overlap** — `tm/store.py` (legacy facade), `tm/stores/sqlite_store.py` (concrete), `tm/repositories/events.py` (new canonical for events) all open their own connections | medium | Coordinate write paths once T-PM-01 + T-INT-01 land — surface for design at that point |

---

## Migration / upgrade notes

First time landing — **no breaking changes**. Existing development databases (if any) pick up migrations 0003 and 0005 idempotently on next CLI invocation; `schema_migrations` records the checksum and refuses to re-apply on file mutation.

---

## Next-step pointers

- **T-PM-01** rebuilds the events table for XES extension and adds the formal `REFERENCES goals(goal_id)` FK on `advances_goal`. This closes the soft-FK TOCTOU and the no-FK-probe-test gap. (Currently in flight in Wave 3b.)
- **T-INT-01** integrates LLM-driven goal tagging during the debrief agent. The integration point is `EventsRepository.append_event(advances_goal=...)`.
- **T-OUT-01** (outcome scoring) consumes `EventsRepository.count_advancing_goal()` to compute the any-goal-step component of the daily outcome score.
- **T-GOAL-05** (optional polish) — add the `goals.name` SQL CHECK constraint to close the schema/Python validation drift.
