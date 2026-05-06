# Temporal Knowledge Graph v1 — Task Registry

Source plan: `docs/designs/temporal-knowledge-graph.md`
Orchestrator role: execution director (no implementation in main session)
Daily token cap: 4,000,000 tokens; 150% per-task halt; 3-cycle review-revision halt.

## Lifecycle states

`pending → assigned → in_progress → self_tested → cross_review → revision_needed | integration_validation → done`
Any task may transition to `halted` on kill-switch or plan-drift; resume only on human authorization.

## Live state

| ID | Title | Lane | Risk | Deps | Token Budget | Status | Owner | Notes |
|---|---|---|---|---|---|---|---|---|
| T-FND-01 | Project skeleton & tooling | codex-eq (sonnet) | L | — | 60K | **done** | sonnet worker (24K) + opus reviewer (14K) | Wave 0; subsumed T-FND-03; deviation tm/__main__.py accepted as justified |
| T-FND-02 | SQLite Store facade + migration runner | opus | H | T-FND-01 ✓ | 220K | **done** | opus worker (28K) + opus reviewer (32K) | Wave 1; pass_with_findings; 8 non-blocking flagged |
| T-FND-03 | ~~CLI scaffold (typer)~~ | — | — | — | — | **dropped** | — | Subsumed by T-FND-01 (entrypoint already shipped) |
| T-FND-04 | LLM client protocol + Anthropic adapter + cost ledger | opus | M | T-FND-02 ✓ | 220K | **done** | opus worker (65K) + opus reviewer (38K) = 103K | pass_with_findings; 5 non-blocking findings tracked; provenance smell cosmetic only |
| T-FND-05 | Test harness (smoke + types/probe.py) | sonnet | L | T-FND-01 ✓ | 60K | **done** | sonnet worker (18K) + opus reviewer (24.5K) = 42.5K | pass_with_findings; tests/__init__.py deviation judged smallest correct fix; 3 optional polish items |
| T-FND-02b | Fix migration tests after parallel migrations land | codex-eq (sonnet) | L | T-FND-04 ✓, T-GOAL-01 ✓, T-VOC-01 ✓ | 40K | **done** | sonnet worker (8.5K) + opus reviewer (12.5K) = 21K | clean **pass** (zero findings) — drift fully resolved |
| T-GOAL-02 | tm goal add/list/complete/abandon/show CLI | sonnet | L | T-GOAL-01 ✓ | 60K | **done** | sonnet worker (28K) + opus reviewer (24K) = 52K | clean **pass**; typer 0.25.1 confirmed real; 4 non-blocking polish items |
| T-GOAL-03 | events.advances_goal soft-FK + EventsRepository + timestamp drift fix | sonnet | M | T-GOAL-01 ✓, T-FND-02 ✓ | 80K | **done** | sonnet worker (28K) + opus reviewer (22K) = 50K | clean **pass**; surgical per-commit boundaries; 3 future-proofing notes |
| T-FND-06 | Daemon skeleton (Unix-socket single-writer) | opus | H | T-FND-02 ✓, T-FND-04 (recommended) | 280K | pending | — | Hold until T-FND-04 done (wants CostMeter for daemon-side gating) |
| T-GOAL-01 | goals migration + dataclass + repo | sonnet | L | T-FND-02 ✓ | 120K | **done** | sonnet worker (45K) + opus reviewer (28K) = 73K | pass_with_findings; real bug (timestamp format drift) folded into T-GOAL-03; provenance smell logged |
| T-GOAL-02 | `tm goal add/list/complete/abandon/show` CLI | codex | L | T-GOAL-01, T-FND-03 | 60K | pending | — | Phase 1 |
| T-GOAL-03 | events.advances_goal FK migration | sonnet | M | T-GOAL-01 | 80K | pending | — | Phase 1 |
| T-GOAL-04 | Goals cluster cross-review | opus | — | T-GOAL-01..03 | 50K | pending | — | Review |
| T-VOC-01 | vocabulary table + 16-activity seed + repo | sonnet | L | T-FND-02 ✓ | 100K | **done** | sonnet worker (28K) + opus reviewer (22K) = 50K | pass_with_findings; 5 non-blocking carry-forward to T-VOC-02 |
| T-VOC-02 | soft-alignment + novelty + drift detector | opus | M | T-VOC-01 | 180K | pending | — | Phase 1 |
| T-VOC-03 | `tm vocab review` CLI + cross-review | codex | L | T-VOC-01, T-FND-03 | 60K | pending | — | Phase 1 |
| T-PM-01 | events XES extension migration | opus | M | T-FND-02, T-VOC-01 | 160K | pending | — | Phase 1 |
| T-PM-02 | PM4Py wrapper (discover/conformance/variants/perf) | opus | H | T-PM-01 | 350K | pending | — | Phase 1 |
| T-PM-03 | Kuzu projection + rebuild | opus | H | T-PM-01, T-FND-06 | 280K | pending | — | Phase 1 |
| T-PM-04 | `tm discover` + `tm bottlenecks` CLI + review | sonnet | M | T-PM-02, T-PM-03 | 140K | pending | — | Phase 1 |
| T-OUT-01 | outcome_score module + per-case aggregator | opus | M | T-GOAL-03, T-PM-01 | 200K | pending | — | Phase 1 |
| T-OUT-02 | suggestion telemetry table + recorder | sonnet | L | T-FND-02 | 100K | pending | — | Phase 1 |
| T-OUT-03 | variant cluster labels + scheduler success metric + review | opus | H | T-OUT-01, T-PM-02, T-OUT-02 | 320K | pending | — | Phase 1 |
| T-INT-01 | debrief agent (vocab + goal-tag + emit events) | opus | H | T-VOC-02, T-GOAL-03, T-FND-04 | 380K | pending (stub) | — | Phase 2 |
| T-INT-02 | scheduler agent + PrPM guardrails | opus | H | T-OUT-03, T-PM-02 | 380K | pending (stub) | — | Phase 2 |
| T-INT-03 | E2E integration tests + eval harness | opus | M | T-INT-01, T-INT-02 | 320K | pending (stub) | — | Phase 2 |
| T-INT-04 | v1 parent-task review (codex) | codex | — | T-INT-03 | 80K | pending (stub) | — | Phase 2 |
| T-VOC-03 | tm vocab review CLI + add_canonical extension | sonnet | L | T-VOC-01 ✓, T-VOC-02 ✓ | 60K | **done** | sonnet worker (28K) + opus reviewer (19.5K) = 47.5K | pass_with_findings → promote; 6 non-blocking polish items |
| T-OUT-02 | suggestion telemetry table + repo | sonnet | L | T-FND-02 ✓ | 100K | **done** | sonnet worker (28K) + opus reviewer (18K) = 46K | clean **pass** (3 non-blocking notes) |
| T-OUT-01 | outcome score module + per-case aggregator | opus | M | T-GOAL-03 ✓, T-PM-01 ✓ | 200K | **done** | opus worker (38K) + opus reviewer (38K) = 76K | pass_with_findings → promote; 2 non-blocking deferred to T-OUT-03 |
| T-DOC-01 | /document-release: Goals | orchestrator | — | T-GOAL-04 ✓ | inline | **done** | orchestrator | Released as commit ca31a2b → docs/release/goals.md |
| T-DOC-02..05 | /document-release: Vocab/PM/Outcome/v1 | orchestrator | — | per parent done | inline | pending | orchestrator | Fire on each parent completion |
| T-GOAL-04 | Goals parent-task review | opus reviewer | L | T-GOAL-01..03 ✓ | 50K | **done** | opus reviewer (28K) | pass_with_findings → promote; cluster cohesion verified |
| T-GOAL-05 | Polish: goals.name CHECK + T-GOAL-02 follow-ups | codex-eq | L | none (prefer after T-PM-01) | 50K | pending | — | Optional; surfaced by T-GOAL-04 |
| T-VOC-02 | LLM soft-alignment + novelty + drift detector | opus | M | T-VOC-01 ✓, T-FND-04 ✓ | 180K | **done** | opus worker (60K) + opus reviewer (18K) = 78K | clean **pass**; 4 non-blocking notes; T-VOC-01b follow-up created |
| T-VOC-01b | Expose VocabularyRepository.db_path as @property | codex-eq | L | T-VOC-01 ✓, T-VOC-02 ✓ | 15K | pending | — | Tiny polish surfaced by T-VOC-02 review |
| T-PM-01 | Events XES additive columns + EventsRepository extension | opus | M | T-FND-02 ✓, T-VOC-01 ✓, T-GOAL-03 ✓ | 160K | **done** | opus worker (50K) + opus reviewer (24K) = 74K | pass_with_findings; 4 commits; formal FK deferred to T-FND-07 |
| T-PM-01b | Polish: sentinel doc + _derive_case_date tightening | codex-eq | L | T-PM-01 ✓ | 30K | pending | — | Surfaced by T-PM-01 review |
| T-PM-02 | PM4Py wrapper (discover/conformance/variants/performance) | opus | H | T-PM-01 ✓ | 350K | **done** | opus worker (95K) + opus reviewer (38K) = 133K | pass_with_findings → promote; 4 polish items folded into T-PM-02b; pm4py 2.7.22.2 (~250MB install) |
| Wave 3e (polish) | T-VOC-01-polish + T-GOAL-05a + T-PM-01b + T-PM-02b | sonnet × 4 + opus reviewer | L | various | 140K | **all done** | 50.5K worker + 23K reviewer = 73.5K total | clean **pass**; case-asymmetry CLOSED; tree green 244/244 + 1 skipped |
| T-VOC-04 | Vocabulary parent-task review | opus reviewer | L | T-VOC-01..03 ✓ | 50K | **done** | opus reviewer (32K) | pass_with_findings → promote; case-asymmetry now reachable via CLI (T-VOC-01c queued); timestamp drift (T-VOC-01d queued); release doc committed 0348379 |
| T-DOC-02 | /document-release: Vocabulary | orchestrator | — | T-VOC-04 ✓ | inline | **done** | orchestrator | Released as 0348379 → docs/release/vocabulary.md |
| T-FND-07 | Migration runner extension for pre-txn pragmas | opus | M | T-FND-02 ✓ | 200K | pending | — | Unblocks formal FK rebuild + goals.name CHECK + future schema changes; surfaced by T-GOAL-03, T-GOAL-04, T-PM-01 reviews |
| Wave T-A (Tier-A polish bundle) | T-INT-02b + T-PM-02-followup-a + T-VOC-03b + T-INT-04-events-until-doc | codex (xhigh, single bundled session) | L | various ✓ | 155K | **all done** | codex worker (~155K) + orchestrator inline verify | Single session, 4 sub-tasks, file-isolated; 4 commits 09cca89/543d223/d51fbf6/147b138; 543/543 passed (+5 new); mypy + ruff clean; SLF001 noqa eliminated; PetriNetData now first-class on DiscoveredModel via new tm/engines/petri_net.py |

## File-lock ledger

Updated by orchestrator on each dispatch. Lock = task ID currently allowed to edit.

| Path / glob | Locked by | Released? |
|---|---|---|
| pyproject.toml, .gitignore, top-level scaffold | T-FND-01 | open until completion |
| (all other paths) | — (greenfield) | n/a |

## Drift / halt log

| When | Detected by | Drift | Disposition |
|---|---|---|---|
| 2026-05-05 | T-FND-05 worker (full pytest sweep) | T-FND-02's tests/stores/test_migrations.py hard-codes expected migration version lists (e.g. expects [1] from apply_pending_migrations on empty DB; expects checksum lookup at version 1). When T-GOAL-01 and T-VOC-01 land 0003 + 0004 in the live migrations/ dir, those tests fail because they didn't isolate the migrations directory under fixture control. | NEW task T-FND-02b (codex-eq, 40K) created; will land after all Wave 2 migration writers complete. NOT a halt — local-scope test bug, no production impact. |

## Kill-switch state

- Daily cumulative tokens: ~3.05M actual of 4M cap (~76%)
- **All four v1 deliverables SHIPPED with release docs + 3 critical caveats CLOSED + Tier-A polish bundle COMPLETE:**
  - Vocabulary governance: `0348379`
  - Goals: `ca31a2b`
  - Process mining: `d8062ff` (SLF001 carry-forward NOW RESOLVED via Wave T-A)
  - Outcome scoring: `e188119`
  - v1 integrated: `ef3fa59` (signoff = granted, no longer with caveats)
  - Critical caveats: `0541fc3` (tm init), `385030d` (README), `5869b8c` (LLM extract usage)
  - Tier-A polish: `09cca89` `543d223` `d51fbf6` `147b138`
- Halts active: none
- Retry counters: all 0
- Full suite: **543 passed + 1 skipped**, mypy clean (44 files), ruff clean (94 files)

## Dispatch log

| Wave | Time | Task | Lane | Status | Tokens |
|---|---|---|---|---|---|
| 0 | 2026-05-05 | T-FND-01 | codex-eq (sonnet via Agent) | done | 24K worker |
| 0R | 2026-05-05 | T-FND-01 review | claude-opus | pass_with_findings → promote | 14K |
| 1 | 2026-05-05 | T-FND-02 | opus | done | 28K worker |
| 1R | 2026-05-05 | T-FND-02 review | claude-opus (codex-eq) | pass_with_findings → promote | 32K |
| 2 | 2026-05-05 | T-FND-04 | opus (async) | self_tested | 65K |
| 2 | 2026-05-05 | T-FND-05 | sonnet (async) | dispatched | budget 60K |
| 2 | 2026-05-05 | T-GOAL-01 | sonnet (async) | self_tested | 45K |
| 2 | 2026-05-05 | T-VOC-01 | sonnet (async) | done | 50K |
| 2 | 2026-05-05 | T-FND-05 | sonnet (async) | done | 42.5K |
| 3a | 2026-05-05 | T-FND-02b | sonnet (async) | done | 21K |
| 3aR | 2026-05-05 | T-FND-02b review | claude-opus | clean pass | 12.5K |
| 3aR | 2026-05-05 | T-GOAL-02 review | claude-opus | clean pass | 24K |
| 3aR | 2026-05-05 | T-GOAL-03 review | claude-opus | clean pass | 22K |
| 3b | 2026-05-05 | T-GOAL-04 (cluster) | opus reviewer | pass_with_findings → promote | 28K |
| 3b | 2026-05-05 | docs(release): goals | orchestrator inline | committed ca31a2b | inline |
| 3b | 2026-05-05 | T-VOC-02 | opus (async) | dispatched | budget 180K |
| 3b | 2026-05-05 | T-PM-01 | opus (async) | dispatched (commits already landing) | budget 160K |
| 3a | 2026-05-05 | T-GOAL-02 | sonnet (async) | self_tested | 28K |
| 3a | 2026-05-05 | T-GOAL-03 | sonnet (async) | self_tested | 28K |

## Migration number ledger (prevents collisions across parallel workers)

| # | File | Owner | Status |
|---|---|---|---|
| 0001 | events + schema_migrations | T-FND-02 | landed |
| 0002 | cost_ledger | T-FND-04 | in flight |
| 0003 | goals | T-GOAL-01 | in flight |
| 0004 | vocabulary + aliases | T-VOC-01 | in flight |
| 0005 | events.advances_goal SOFT FK column + index (formal FK deferred to 0006) | T-GOAL-03 | in flight |
| 0006 | events XES extension + table rebuild adding formal REFERENCES goals(goal_id) FK | T-PM-01 | reserved |
| 0007 | suggestion_telemetry | T-OUT-02 | reserved |

## Findings carried forward (orchestration log)

From T-FND-01 review (inject into later dispatch packets):
- Subcommand registration pattern: future area tasks (T-GOAL-02, T-VOC-03, T-PM-04) should add sub-typers via `app.add_typer(<sub_app>, name='goal' | 'vocab' | 'discover' | 'bottlenecks')`.
- pre-commit mypy hook may need `additional_dependencies: [typer, ...]` once strict mode tightens.
- `tm/__init__.py` may need to re-export `app` if downstream tooling wants `from tm import app`.

From T-FND-07 reviewer (META-ORCHESTRATION — packet-writing convention):
- **When a worker introduces new exception classes in `tm/stores/sqlite_store.py` (or any sub-module that has a tm/<facade>.py re-export), the dispatch packet's allowed-files list MUST include the public facade file** (`tm/store.py`) so the re-export can be added in-scope. Otherwise the worker either (a) leaks an internal module path to all callers or (b) edits the facade out-of-scope and reviewers must judge severity. T-FND-07's tm/store.py edit was judged acceptable but the convention should be explicit going forward.

From T-OUT-01 reviewer (META-ORCHESTRATION FINDING — apply to all future review packets):
- **Reviewer cleanup commands MUST name the exact directory**, never use globs in /tmp. T-OUT-01's reviewer ran `find /tmp -maxdepth 1 -name "tmp*" -type d -mtime -1 -exec rm -rf {} +` during cleanup — the glob is broad and could delete unrelated /tmp/tmp* dirs from sibling processes/users. Future review packets must say: "Clean up `rm -rf /tmp/<exact-named-venv-dir>` only — do NOT use globs or recursive find with rm -rf". Defensive default for all worker + reviewer dispatches.

From Wave 3c parallel execution (orchestrator-verified):
- **Mutual lint-blame between parallel workers can be a snapshot-timing artifact, not real divergence.** T-VOC-03 reported lint violations in T-OUT-02 files; T-OUT-02 reported lint violations in T-VOC-03 files. Final tree state (verified at 2026-05-05): `ruff check .` clean across 55 files. Each worker likely ran ruff while the OTHER had partially-committed unformatted code; both ultimately committed clean code. Lesson: trust the FINAL tree state over individual worker self-reports when reports are mutually contradictory.

From T-GOAL-02 review (carry to T-VOC-03 + T-PM-04 CLI tasks + T-INT-01):
- **typer~=0.12 resolves to typer 0.25.1** under PEP 440. CliRunner in 0.25.1 does NOT accept `mix_stderr=False` — use plain `CliRunner()` and `result.output` (combined stdout+stderr).
- **db_path option pattern**: prefer `Annotated[Path | None, typer.Option(...)] = None` over `Annotated[Path, ...] = None # type: ignore[assignment]`.
- **`_validate_priority` is private** — when CLI commands need value validation that mirrors model invariants, either promote to public OR inline. Don't import underscored symbols.
- **Bare-ISO-date target semantic gap (real, follow-up worthy):** `datetime.fromisoformat('2026-12-31')` produces naive datetime; goals storage emits `'2026-12-31T00:00:00'` (no Z suffix), inconsistent with the "ISO 8601 UTC" convention. Either attach UTC tz at the CLI boundary OR document bare-date as midnight UTC. Carry to follow-up; does NOT block any current task.

From T-GOAL-01 review (CRITICAL bug to fix in T-GOAL-03 packet):
- **TIMESTAMP FORMAT DRIFT (real bug):** `GoalsRepository.add()` does NOT pass `created_at` to its INSERT; SQLite's `datetime('now')` default emits `'2026-05-05 18:49:46'` (space separator, no Z), but the project's `_ISO_FMT` is `'%Y-%m-%dT%H:%M:%SZ'`. Verified live: `created_at='2026-05-05 18:49:46'` came back from the DB. Downstream code that compares timestamps as strings (CostMeter.monthly_total, T-OUT-01's per-case aggregator, T-INT-01 debrief grouping) WILL silently filter incorrectly. Fix in T-GOAL-03: amend `GoalsRepository.add()` to pass `created_at=_now_iso()` explicitly. (Optionally drop the SQL DEFAULT or keep as a safety net.)
- Schema/Python validation drift: `goals.name` SQL allows `''` and `'   '`; Python `__post_init__` rejects them — `_row_to_goal` raises ValueError instead of returning the row if such data ever lands. T-GOAL-03 should add `CHECK (length(trim(name)) > 0)` to the schema (small additional migration OR fold into 0005).
- DRY: `Status` Literal and `_ISO_FMT` are duplicated across `tm/models/goals.py` and `tm/repositories/goals.py`. Status should re-export from models; ISO_FMT belongs in a shared `tm/_time.py` helper. Non-blocking; address opportunistically.
- ULID has no monotonic counter — same-ms collisions break lexical order. Test acknowledges. Document.

From T-VOC-01 review (carry to T-VOC-02 packet):
- **Case-symmetry bug:** `resolve()` lowercases input but `add_alias()` does NOT normalize `free_text_variant`. Mixed-case custom aliases (`add_alias('GymSession', 'exercise')`) then `resolve('gymsession')` → None. STARTER_ALIASES are all-lowercase so seeded data works. T-VOC-02 (or a small follow-up) should: (a) lowercase variant on insert, (b) add a regression test for mixed-case custom aliases.
- **`add_alias()` is an upsert (INSERT OR REPLACE) but docstring says "Add"** — document semantics OR change to INSERT OR ABORT with explicit error.
- **Alias-collapse hazard:** `breakfast→meal`, `dinner→meal` collapse meal-context. If the product wants meal-of-day analysis, those rows must be removed. Decision deferred — flag in T-VOC-02 packet for product-side awareness.
- **Misleading test name:** rename `test_alias_fk_blocks_canonical_archive_with_aliases` → `test_alias_fk_blocks_raw_delete_of_canonical`.
- **General**: workers' tests-passing arithmetic in return summaries is unreliable; always trust the reviewer's independent re-run.

From T-FND-05 review (spec-writing lessons for future packets):
- **Stop using `python -c "from <module> import settings; print(...)"` as a verification recipe** when the module is a pytest conftest. Conftest is only loaded by pytest. Use `pytest --collect-only -q | head -3` (header line includes hypothesis profile registration) or write the recipe as a small pytest test instead.
- **Skip-gates that test `shutil.which('mypy')` are PATH-dependent**; sandboxed CI may have mypy importable but not on PATH. Use `try: import mypy except ImportError: pytest.skip(...)` for robust gating.

From T-FND-04 review (carry to T-FND-06 / T-INT-01 / T-INT-02 packets):
- **AnthropicAdapter docstring overstates per-call model override** — chat/extract/tool_call always use self._model. Either fix docstring OR add per-call kwargs. T-INT-01/T-INT-02 will likely WANT per-call overrides (debrief on sonnet, scheduler on haiku, complex reasoning on opus); decide who implements (a follow-up T-FND-04b, or absorb into T-INT-* dispatch).
- **CostMeter._soft_alarm_fired is per-instance** — fine for the daemon (one meter), but T-FND-06's daemon must hold a single CostMeter for all gates.
- **`extract()` does not validate output against schema** — T-INT-01's debrief agent must validate extractor output itself (Pydantic-style or manual). Carry to T-INT-01 packet.
- **No retry/backoff for SDK transient errors** — T-INT-01/T-INT-02 / debrief / scheduler must wrap calls if they care; or add a follow-up task.
- **`cost_ledger.ts` relies on fixed-width ISO without CHECK constraint** — silent breakage if anyone writes a row with TZ offset. Add CHECK in a future hardening migration; not blocking.

From Wave 2 parallel execution (CRITICAL for all future parallel dispatches):
- **Workers MUST use targeted git add/commit syntax in parallel dispatch.** Use `git add <specific_paths>` and `git commit -- <specific_paths>` only. NEVER `git add .` / `git add -A` / `git commit -a` — these sweep up files staged by sibling workers via the shared git index, producing commits whose message belongs to one task but content includes another's. Evidence: cf7cc7e (T-GOAL-01 authored "feat(migrations): add 0003_goals.sql") accidentally contained T-FND-04's tm/llm/cost_meter.py.
- **Workers must verify in their return summary:** `git show --stat <each-commit-hash>` lists ONLY their allowed-files-to-edit. Provenance smell to be flagged.

From T-FND-02 review (CRITICAL to inject into specific later packets):
- **For T-PM-01 (and any future migration with triggers, block comments, or double-quoted identifiers):** the SQL splitter in `tm/stores/sqlite_store.py::_split_sql_statements` only handles single-quoted strings + `--` line comments. MUST be tightened to handle: (a) double-quoted identifiers as a separate quote state, (b) `/* */` block comments, (c) `BEGIN..END` trigger nesting. Add direct unit tests for the splitter when extending. **This is a precondition for T-PM-01.**
- **For T-FND-05 (test harness):** add `tests/types/probe.py` that intentionally calls write methods on a `StoreReader`-typed parameter, plus a mypy-driven CI hook so the Protocol enforcement is mechanically guarded. Worker's `hasattr` test only proves runtime invariant.
- **For T-FND-04 (cost ledger), T-VOC-01, T-PM-01, T-OUT-* (anyone appending to tm/store.py or tm/stores/sqlite_store.py):** add explicit extension-point comment markers (`# --- extension point: <area> read methods ---`) before appending; group by region in sqlite_store.py to avoid textual merge conflicts. Methods on the Protocol go in StoreReader; on the concrete class go in SQLiteStore.
- **For T-FND-06 (daemon):** Store uses a single per-Store connection (not pool). Daemon must serialize writes itself; busy-retry covers external-vs-Store contention but not Store-vs-Store. Document this in the daemon's threading model.
- **For any future maintenance:** `_write_txn` retry should narrow to ONLY BEGIN IMMEDIATE failures (not body exceptions). `tm/store.py::_assert_reader_is_protocol` is dead code — clean up later.

## Scope adjustments

- T-FND-03 dropped (subsumed by T-FND-01).
- T-FND-05 scope narrowed: pytest config + hypothesis + CLI smoke test only. SQLite fixture moved into T-FND-02's deliverable (the store author writes the in-mem fixture).

## Scope adjustments

- T-FND-03 dropped (subsumed by T-FND-01).
- T-FND-05 scope narrowed: pytest config + hypothesis + CLI smoke test only. SQLite fixture moved into T-FND-02's deliverable (the store author writes the in-mem fixture).
