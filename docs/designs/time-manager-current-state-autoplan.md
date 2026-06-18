# Time Manager Current-State Autoplan Reconciliation

Generated: 2026-06-16
Branch: main
Purpose: reconcile the original temporal-knowledge-graph plan, implementation
transcripts, release docs, backlog, and current code so the next work starts
from current reality.

## Executive Summary

`tm` is no longer a greenfield plan. The repo now has a shippable v1 CLI loop:
`tm init -> goal add -> debrief -> discover -> variants/bottlenecks -> suggest`.
The current code and release docs supersede the original May 5 plan wherever
they conflict.

The highest-value output of this review is not another feature list. It is the
boundary between shipped v1, stale plan promises, and product decisions that
still need a human call. The remaining implementation work is mostly v1.1+
backlog and three strategic decisions: daemonization, variant trend/drift
labeling, and live-LLM eval calibration.

## Source Ledger

| Source | Role | Freshness | Reliability | Notes |
|---|---|---:|---|---|
| `tm/`, `tests/`, `migrations/` | Runtime truth | current checkout | highest | Determines what actually ships. |
| `git log --oneline -20` | Commit truth | current checkout | highest | Shows recent closure of multi-backend routing, daemon lock/race work, and release-doc updates. |
| `docs/release/v1.md` | v1 release record | current checkout | high | Canonical release narrative; aggregate counts were refreshed during this reconciliation. |
| `docs/release/{process_mining,vocabulary,goals,outcome_scoring}.md` | Area release records | current checkout | high | Good area-level truth, some counts intentionally frozen at cluster promotion. |
| `README.md` | Operator docs | current checkout | medium | Good quickstart; concurrency wording was refreshed during this reconciliation. |
| `docs/designs/temporal-knowledge-graph.md` | Promoted source plan | local untracked | medium | Still valuable for intent and original scope, but several v1 promises are not current code. |
| `TODOS.md` | Local backlog | local untracked | medium | Missing several current open items before this reconciliation. |
| `~/.gstack/projects/berdyh-time-manager/ceo-plans/2026-05-05-temporal-knowledge-graph.md` | Original plan source | historical | medium | Use for why decisions were made, not as current implementation truth. |
| `time-manager-mega-plan-implementation.md` | Implementation transcript | local untracked | low | Useful evidence for Tier P/M/S decisions, not a maintained plan. |
| `plan_part_for_time_manager.md` | Early review transcript | local untracked | low | Historical context only. |

## Current Shipped State

The current release surface includes:

- CLI subcommands for init, goals, vocabulary review/list/drift, daemon, process
  mining discovery, bottlenecks, variants, debrief, suggest, capture,
  dashboard, export, backup, privacy, reextract, and encryption status.
- Eleven migrations, including `0010_debrief_summary_uniqueness.sql` for the
  duplicate debrief-summary race and `0011_capture_transcripts_privacy.sql`
  for transcript retention and privacy audit rows.
- Three LLM backends through `TM_LLM_BACKEND`: `anthropic`, `codex`, and
  `claude-code`.
- Daemon RPC handlers for Kuzu projection, debrief, and suggestion generation.
- Process-mining fast-path rehydration from `PetriNetData`, with fallback to
  re-mining only when needed.
- First-user CLI path documented in the README and v1 release doc.

This means the release is implementation-complete for the CLI-first v1.1 local
operator surface. Live integrations such as Telegram bot polling, CalDAV sync,
and audio transcription remain outside this branch.

## Plan-Vs-Implementation Matrix

| Planned or claimed capability | Current status | Evidence | Decision |
|---|---|---|---|
| CLI-first debrief and suggestion loop | shipped | `tm/commands/debrief.py`, `tm/commands/suggest.py`, README quickstart | keep as v1 truth |
| Process mining over workday and goal-pursuit lenses | shipped | `tm/engines/process_mining.py`, process-mining release doc | keep |
| Kuzu projection and conformance rehydration | shipped | `tm/stores/kuzu_projection.py`, `tm/engines/petri_net.py`, process-mining release doc | keep |
| Vocabulary governance and drift CLI | shipped | `tm/commands/vocab.py`, vocabulary release doc | keep |
| Goals as first-class records | shipped | `tm/commands/goal.py`, migrations 0003/0005, release docs | keep |
| Outcome score `0..2` | shipped | `tm/models/outcome.py`, outcome release doc | keep |
| LLM backend selector | shipped with three backends | `tm/llm/factory.py` validates `anthropic`, `codex`, `claude-code` | update old "four backends" plan language before treating it as current |
| OpenAI direct backend | not shipped | absent from `VALID_BACKENDS` | backlog, not v1 |
| Ollama/OpenCode/Gemini adapters | not shipped | TODO/release follow-up only | backlog |
| Telegram capture | shipped as local import | `tm capture telegram` imports Telegram JSON exports | live bot remains v2/backlog |
| Bot whitelist/auth model | not shipped | no live bot surface | stale v1 plan promise |
| SQLCipher/keyring encryption | shipped as optional connection/key support | `tm/security.py`, `tm encryption status`, `TM_SQLCIPHER_KEY`, keyring opt-in | requires SQLCipher-enabled driver |
| Daily backup and `tm export` | shipped | `tm export`, `tm backup` | encrypted backup depends on SQLCipher-enabled driver |
| Dashboard metrics | shipped | `tm dashboard` | keep local and compact |
| `tm forget` / `tm redact` | shipped | `tm privacy forget`, `tm privacy redact` | keep audit rows in `privacy_actions` |
| First-run guided setup with framing questions | not shipped | `tm init` applies migrations and seeds starter vocabulary | stale v1 plan promise |
| Pattern confidence/decay engine | not shipped as a named surface | no current release surface beyond scheduler telemetry and variants | validate before reviving |
| Daemon proper daemonization | shipped | `tm daemon start --no-foreground` double-forks on POSIX | keep foreground default for supervisors |
| Variant trend/drift labeling | shipped | `tm variants --trend --since ... --until ...` compares against the previous equal-size window | explicit windows only |
| Live-LLM eval threshold calibration | partially shipped | eval harness now has separate live thresholds; no API key was available to rerun live calibration in this branch | refresh with real eval repo |
| Lock-during-LLM concurrency carry-forward | closed in code and docs | `tm/daemon.py` classifies LLM handlers outside `self._lock`; release doc and README now mark it closed | keep closed unless new concurrency evidence appears |

## CEO Review

Score: 8/10 for current v1 direction.

What is working:

- The shipped product chose the right first-user surface: CLI and local files
  instead of trying to finish Telegram, passive capture, and a dashboard at the
  same time.
- The process-mining reframe survived implementation. The core loop now has
  an event log, case lenses, process discovery, variant analysis, outcome
  scoring, and scheduler suggestions.
- The backlog now has a healthy shape: v1.1 adapters and workflow polish, v2
  passive capture, v3 formal causal work only if the data earns it.

Concerns:

- The promoted plan still reads like the broader v1 scope shipped. It did not.
  That is fine if the source of truth is the release doc, but dangerous if a
  future worker starts from the plan and reopens stale promises.
- This CEO review section is historical. After the local-operator branch,
  SQLCipher/keyring support, backups/export, dashboard metrics, and
  redact/forget are shipped as local CLI surfaces; Telegram bot/webhook,
  owned voice transcription, CalDAV sync, and first-run guided setup remain
  future product work.
- The biggest strategic question is not "what can be built next"; it is whether
  the CLI loop is producing real daily value before adding passive capture or
  bot surfaces.

CEO decision: treat current v1 as CLI-first and shippable. Keep the broader
temporal-knowledge-graph plan as intent, not as an acceptance checklist.

## Engineering Review

Score: 8/10 for current engineering readiness.

What is working:

- The architecture is modular enough: commands, agents, engines, repositories,
  stores, LLM adapters, and daemon are separated cleanly.
- Recent commits closed several important integration holes: daemon LLM handler
  locking, duplicate debrief summary race, and backend factory routing.
- Tests exist across commands, models, repositories, stores, engines, LLM
  adapters, and integration/eval harnesses.

Concerns:

- Release docs and README previously disagreed on daemon LLM-handler locking;
  this reconciliation refreshed README to match `tm/daemon.py` and the v1
  release doc.
- `docs/release/v1.md` previously had stale aggregate counts in the body; this
  reconciliation refreshed them to the verified 711 passed + 1 skipped, mypy
  clean over 58 source files, and ruff format clean over 117 files.
- Subprocess LLM adapters duplicate enough scaffolding that adding more
  adapters should first extract shared helpers.

Engineering decision: do not build new features until the docs/backlog are made
truthful. The next code work should be one of the explicit Tier S decisions or
the v1.1 adapter-helper extraction.

## DX Review

Score: 7/10 for first-developer and operator experience.

What is working:

- README quickstart is under five minutes for a local SQLite workspace.
- Command names are predictable and the main v1 path is spelled out.
- Environment variables for DB, LLM backend, budget, socket, and PID paths are
  documented.

Concerns:

- The project is still marked pre-alpha, which is honest, but the docs need one
  clear "what is not shipped yet" list for a new operator.
- Live LLM evals are available but not calibrated; this should stay clearly
  labeled as smoke-mode until real thresholds are measured.
- Backend selection is documented, but future adapter work should avoid adding
  copy-pasted subprocess code.

DX decision: keep the README focused on the CLI-first path. Put strategic and
unshipped surface area in release docs/TODOs, not in the quickstart.

## Open Decision Graph

```text
Current v1 CLI loop is shipped
|
+-- Tier S: Daemonization
|   +-- defer if systemd/supervisor remains the deployment model
|   +-- implement only if users need `tm daemon start` to detach itself
|
+-- Tier S: Variant trend/drift labeling
|   +-- defer if current windowed variant labels are enough for first 30 days
|   +-- implement if scheduler recommendations need trend-over-time evidence
|
+-- Tier S: Live-LLM eval calibration
|   +-- defer if live mode is only smoke-tested
|   +-- calibrate before treating live evals as a release gate
|
+-- v1.1: Adapter/helper cleanup
|   +-- extract subprocess adapter helpers before OpenCode/OpenAI/Gemini/Ollama
|
+-- v2+: Passive and chat surfaces
    +-- calendar/Telegram/voice only after CLI loop proves value
```

## Decision Audit Trail

| # | Phase | Decision | Classification | Principle | Rationale | Rejected |
|---|---|---|---|---|---|---|
| 1 | Intake | Use all artifacts as evidence, not a single plan file | mechanical | explicit over clever | The user requested all artifacts be analyzed; the repo has separate plan, release, and transcript roles. | single-file review |
| 2 | Source truth | Current code/release docs override old plan text | mechanical | pragmatic | Implementation has moved past the May 5 plan; stale plan promises should not drive acceptance. | treating promoted plan as current checklist |
| 3 | Scope | Do not edit transcript files | mechanical | DRY | Transcripts are historical evidence and not maintained docs. | rewriting captured sessions |
| 4 | Backlog | Update TODOs for verified open items | mechanical | completeness | v1 release docs mention open items absent from `TODOS.md`. | leaving backlog incomplete |
| 5 | Product | Keep Telegram/passive capture deferred | taste | bias toward action | CLI-first v1 exists and should be validated before adding high-friction surfaces. | reopening bot work immediately |

## Cross-Phase Themes

Theme: stale documentation is now the largest risk. CEO, engineering, and DX
all point to the same issue: the code is more settled than the docs that
describe it.

Theme: next work should be evidence-gated. Live LLM calibration, variant
trend labeling, Telegram, and passive capture all depend on whether the current
loop creates real value in daily use.

Theme: backend expansion should wait for adapter cleanup. The current three
backends are enough for v1; more adapters should not copy the same subprocess
logic again.

## Recommended Next Tasks

1. Decide Tier S daemonization: foreground under systemd/supervisor is probably
   enough for v1.1.
2. Decide Tier S live-LLM calibration: run only when credentials and cost budget
   are intentionally provided.
3. Before adding another LLM backend, extract shared subprocess adapter helpers.

## Verification Plan

Run:

```bash
git status --short --branch --untracked-files=all
.venv/bin/pytest -q
.venv/bin/mypy tm
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Smoke-check:

```bash
tm --help
tm debrief --help
tm suggest --help
tm daemon --help
tm vocab drift --help
```

Do not run live LLM evals without explicit credential and spend authorization.

## Verification Result

Completed on 2026-06-16 and reverified on 2026-06-17:

| Check | Result | Notes |
|---|---|---|
| `git status --short --branch --untracked-files=all` | tracked docs changed plus local untracked docs/state remain | Expected in this work-in-progress pass: `README.md`, `docs/release/v1.md`, and `docs/release/process_mining.md` are modified; this artifact, TODOs, notes, temporal plan, transcripts, `.claude/`, and `uv.lock` are still local/untracked. |
| `.venv/bin/pytest -q` | 711 passed, 1 skipped | Reverified on 2026-06-18 with 1 pytest-asyncio deprecation warning. |
| `.venv/bin/mypy tm` | no issues in 58 source files | Clean. |
| `.venv/bin/ruff check .` | all checks passed | Clean. |
| `.venv/bin/ruff format --check .` | 117 files already formatted | Clean. |
| `.venv/bin/tm --help` | exit 0 | Reverified on 2026-06-17. |
| `.venv/bin/tm debrief --help` | exit 0 | Reverified on 2026-06-17. |
| `.venv/bin/tm suggest --help` | exit 0 | Reverified on 2026-06-17. |
| `.venv/bin/tm daemon --help` | exit 0 | Reverified on 2026-06-17. |
| `.venv/bin/tm vocab drift --help` | exit 0 | Reverified on 2026-06-17. |

Docs corrected during this pass:

- README "Known Limitations" now marks the daemon LLM-handler lock issue as
  closed and points at migration 0010 for the duplicate-summary race guard.
- `docs/release/v1.md` now uses the verified aggregate counts consistently:
  711 passed + 1 skipped, 58 mypy-checked source files, and 117
  ruff-formatted files.
- README environment docs now state the current shared `TM_LLM_API_KEY` guard
  for all LLM-backed CLI/daemon requests before backend selection, including
  the `codex` backend's separate `codex login` credentials.
- `docs/release/process_mining.md` now labels its 394 passed + 1 skipped suite
  count as promotion-time evidence instead of the current aggregate total.
- `docs/release/v1.md` and README now label the CLI surface as 16 entries
  under `tm`: 15 sub-typers plus standalone `tm init`.
