# Release: Vocabulary governance

**Deliverable:** Vocabulary governance
**Status:** done
**Source plan reference:** `docs/designs/temporal-knowledge-graph.md` (Locked v1 Scope §vocabulary; Engineering Details Vocabulary)
**Released:** 2026-05-05
**Cluster review:** T-VOC-04 (`pass_with_findings` → `promote_to_done`)

---

## Summary

Shipped the vocabulary governance subsystem: a 16-activity canonical starter set plus 5 seed aliases persisted via migration `0004_vocab.sql` in two tables (`vocabulary`, `aliases`). The `VocabAligner` soft-aligns free-text labels to canonical activities with a repo-hit fast path (no LLM call) and an LLM fallback that enforces a JSON schema and rejects hallucinated canonicals via active-vocabulary lookup. It also computes a novelty rate over an event window and surfaces drifted canonicals (no events for N days). The operator-facing surface is `tm vocab review` (interactive merge / create / ignore / skip-rest loop over recent novel labels) and `tm vocab list`. No live LLM is required by the v1 CLI; LLM alignment is surfaced via the in-process `VocabAligner` and consumed by the debrief flow (T-INT-01, future). 42 cluster tests pass within the 204-test full suite.

---

## Subtasks rolled up

| Task | Title | Status | Key commits |
|---|---|---|---|
| T-VOC-01 | VocabularyRepository + 0004_vocab.sql + starter seed data | done | `bfa1cfb` `3ab5ff5` `cea5a94` |
| T-VOC-02 | VocabAligner with align/novelty/drift detection | done | `f19b31e` `fb1d867` |
| T-VOC-03 | tm vocab CLI (review + list) + VocabularyRepository.add_canonical | done | `14849ea` `887c74c` `b1908e0` `99b6abe` |
| T-VOC-04 | parent-task review (cluster signoff) | done | (review only — no commits) |

---

## Public surface added

### CLI commands
- `tm vocab review [--db-path PATH] [--limit N] [--since DATE]` — interactive: surfaces top-N novel labels from recent events; per label `[m]erge / [c]reate / [i]gnore / [s]kip rest`. Skips LLM in v1 (the `--no-llm` flag is reserved scaffolding for a future LLM-assisted variant). Polish landed in commit `d51fbf6`: m-branch abort path, c-branch failure counter, auto-seed on `tm vocab list`, weak test disjunct tightened.
- `tm vocab list [--include-archived]` — prints vocabulary entries; auto-seeds STARTER_VOCABULARY/STARTER_ALIASES on empty DB (commit `d51fbf6`).
- `tm vocab drift [--db-path PATH] [--idle-days N] [--since DATE] [--until DATE]` — operator surface for `VocabAligner.find_drifted_activities` + `compute_novelty_rate` (commit `2558fae`). Lists drifted canonicals (no events in idle window) with last-seen dates + novelty rate over the events window. Works without `TM_LLM_API_KEY` (pure DB query).

### APIs (importable)
- `tm.repositories.vocabulary.VocabularyEntry` (dataclass)
- `tm.repositories.vocabulary.VocabularyRepository` — `seed_starter_vocabulary`, `seed_starter_aliases`, `list_active`, `list_all`, `get`, `resolve`, `add_alias`, `add_canonical`, `archive`
- `tm.vocab_alignment.VocabAligner` — `align`, `compute_novelty_rate`, `find_drifted_activities`
- `tm.vocab_alignment.AlignmentResult` (frozen dataclass)
- `tm.vocab_alignment.lower_normalize`
- `tm.vocab_alignment_errors.AlignmentError`

### Schema migrations
- `migrations/0004_vocab.sql` — `vocabulary` table (PK `activity_name`, status enum active|archived) + `aliases` table (`free_text_variant` PK → `canonical_activity` FK to `vocabulary.activity_name` with `ON DELETE RESTRICT`).

### Starter data
- **STARTER_VOCABULARY (16):** `deep_work`, `shallow_work`, `meeting`, `email`, `code_review`, `exercise`, `meal`, `commute`, `errand`, `family_time`, `social`, `solo_recreation`, `learning`, `rest`, `sleep`, `interruption`
- **STARTER_ALIASES (5):** `workout→exercise`, `lunch→meal`, `breakfast→meal`, `dinner→meal`, `standup→meeting`

---

## Tests added

| Path | Coverage |
|---|---|
| `tests/repositories/test_vocabulary_repository.py` | 19 tests — seed idempotency, list/get/resolve, archive, alias FK semantics, `add_canonical` happy/duplicate/uppercase/empty paths |
| `tests/test_vocab_alignment.py` | 13 tests — repo-hit fast path (asserts LLM NOT called), LLM fallback, hallucination guard, schema validation (missing keys, out-of-range confidence, wrong types), novelty edge cases (zero/all-known/mixed), drift across `as_of` boundary |
| `tests/commands/test_vocab_cli.py` | 10 tests — interactive prompts via `CliRunner.invoke(input=...)`, persistence verified post-invoke, reprompt path exercised, list rendering (active vs include-archived) |

**Cluster total:** 42 tests; full project suite **204/204 + 1 skipped**.

---

## Key decisions and rationale

1. **Lowercase canonicals only** — `add_canonical` rejects mixed-case via `ValueError`; keeps `resolve()`'s contract simple (one lowercase lookup path). Clean invariant for the vocabulary table.
2. **Repo-hit fast path returns `confidence=1.0` and never consults the LLM** — keeps the cost meter quiet on the common path; LLM fires only when neither the canonical nor an alias matches.
3. **Hallucination guard rejects LLM-proposed canonicals not in the active vocabulary** — prevents phantom canonical names from leaking through the alignment surface; tested via `test_align_rejects_hallucinated_canonical`.
4. **`INSERT OR IGNORE` seeding is idempotent** — `seed_starter_vocabulary()` and `seed_starter_aliases()` can be called repeatedly; safe from migration paths or warm-start.
5. **`tm vocab review` surfaces only repo-resolution misses in v1** — no LLM calls in the CLI path. The `--no-llm` flag is reserved scaffolding for a future LLM-assisted variant.
6. **dataclasses + manual validation, no Pydantic** — consistent with the rest of v1.

---

## Risks and known limitations

| Risk | Severity | Resolution path |
|---|---|---|
| ~~**Case-asymmetry**~~ | ~~medium~~ | **RESOLVED** in T-VOC-01-polish bundle (commit `12c463f`) — `add_alias` lowercases + strips inputs; regression test added. |
| ~~**Vocabulary timestamp format diverges from project ISO-T-Z**~~ | ~~low~~ | **RESOLVED** in T-VOC-01-polish bundle. |
| `breakfast/lunch/dinner→meal` collapses meal-of-day context | informational | Product call: remove rows if meal-of-day analysis becomes a goal |
| ~~`_db_path` private-attr access in `VocabAligner`~~ | ~~informational~~ | **RESOLVED** — `VocabularyRepository.db_path` exposed as `@property` (`tm/repositories/vocabulary.py:135`); no `noqa` remaining. |
| ~~`tm vocab list` on empty DB shows "no vocabulary entries" rather than auto-seeding~~ | ~~UX gap~~ | **RESOLVED** in commit `d51fbf6` — auto-seeds STARTER_VOCABULARY/STARTER_ALIASES idempotently on empty DB. |
| ~~`tm vocab review` `m`-branch has no abort path; `c`-branch failure silently drops the label~~ | ~~UX polish~~ | **RESOLVED** in commit `d51fbf6` (T-VOC-03b) — m-branch abort path added, c-branch failure counter increments skipped, weak test disjunct tightened. |
| `VocabAligner.__init__` `llm` param now `LLMClient | None` | informational | Relaxed in commit `2558fae` to allow LLM-free construction for the `tm vocab drift` CLI; existing align/extract paths still raise `AlignmentError` if `llm is None`. |

---

## Migration / upgrade notes

First time landing — **no breaking changes**. Existing development databases pick up `0004_vocab.sql` idempotently on next CLI invocation.

---

## Next-step pointers

All T-VOC follow-ups closed:

- ~~**T-VOC-01c**~~ — DONE (commit `12c463f`): `add_alias` lowercases + strips inputs; regression test added.
- ~~**T-VOC-01b**~~ — DONE: `VocabularyRepository.db_path` is `@property` (`tm/repositories/vocabulary.py:135`).
- ~~**T-VOC-01d**~~ — DONE in T-VOC-01-polish bundle.
- ~~**T-INT-01** debrief agent~~ — DONE earlier in v1; `tm/agents/debrief.py` wires `VocabAligner.align` into the debrief flow.
- ~~**T-VOC-03b** polish~~ — DONE (commit `d51fbf6`).
- **T-VOC-05** `tm vocab drift` CLI — DONE (commit `2558fae`) — operator surface over `VocabAligner.find_drifted_activities` + `compute_novelty_rate`.
