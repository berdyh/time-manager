# tm

`tm` is a local-first time intelligence app for builders who already leave a
trail of notes, calendars, chats, and end-of-day reflections, but want that
material turned into a usable operating record. It keeps a private SQLite event
log, connects work back to goals, scores each day as `{0, 1, 2}`, discovers
repeated patterns, and suggests one concrete next action per day.

The product has two operator surfaces:

- A CLI for capture, debrief, process mining, privacy, export, and automation.
- A local browser cockpit (`tm web`) for checking status, switching local agent
  backends, reviewing recent events, importing data, exporting or backing up
  private data, and running debrief/reextract workflows through the daemon.

## Status

![status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)

Pre-alpha. v1 shipped on 2026-05-06; original caveats are closed and the
operator-surface gap (no `tm debrief` / `tm suggest`) was closed in the same
window. The end-to-end CLI path is now reachable: `tm init → goal add →
debrief transcript.txt → discover → suggest`. Expect rough edges before using
it as a daily tool, but the first-user pipeline is no longer programmatic-only.

## What tm Does

- Turns free-prose debriefs into structured activity events with timestamps,
  lifecycle markers, optional goal links, retained transcripts, and cost
  accounting.
- Imports local source material from Telegram JSON exports, iCalendar files, and
  already-transcribed voice notes.
- Tracks goals, vocabulary drift, repeated activity variants, bottlenecks,
  process models, daily outcomes, and scheduler suggestion telemetry.
- Runs one-action-per-day suggestions behind guardrails for outcome delta,
  conformance, and per-day rate limiting.
- Keeps private data local by default, with JSON export, SQLite backup,
  redaction, forget, reextract, and SQLCipher/keyring status commands.
- Provides a local web cockpit that wraps the same CLI/daemon capabilities
  without moving data to a hosted service.

## Install

Use Python 3.11 or newer. From this repository:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[dev]"
```

Runtime dependencies are `typer`, `anthropic`, `pm4py`, and `kuzu`.

Cold installs can take a bit because `pm4py` pulls the process-mining stack and
`kuzu` pulls the graph database wheel. Together they add roughly 75 MB of
transitive packages.

## Local web cockpit

Install the optional web dependencies and run the local cockpit API:

```bash
python -m pip install -e ".[web]"
tm web --host 127.0.0.1 --port 8765
```

During frontend development, run the Vite app from `frontend/`; it proxies
`/api` to `tm web`:

```bash
cd frontend
npm install
npm run dev -- --port 5173
```

The web API binds to `127.0.0.1` by default, applies migrations on startup, and
protects private API routes with a per-process token returned by `/api/status`.
The UI stores its selected local agent in the existing tm data directory as
`web-config.json`. Local agent auth remains owned by each CLI: Codex, Claude
Code, Gemini, Kimchi, and OpenClaw are not re-authenticated by tm.

## Quickstart

This path stays under five minutes and creates a local SQLite workspace.

1. Set the Anthropic API key.

```bash
export TM_LLM_API_KEY="sk-ant-..."
```

`TM_LLM_API_KEY` is required for LLM-backed debrief extraction and scheduler
suggestions. The setup, goal, vocabulary, and process-mining commands do not
call the LLM by themselves.

2. Initialize the workspace.

```bash
tm init
```

`tm init` uses the default database at `~/.local/share/tm/tm.db`, unless
`TM_DB` is set. It applies all pending SQLite migrations, then seeds the
starter vocabulary:

```text
tm init: db=/home/you/.local/share/tm/tm.db
  applied 11 migrations
  seeded 16 starter activities
  seeded 5 starter aliases
```

The command is idempotent. On a workspace that is already initialized, the
counts may be `0`.

3. Add a first goal.

```bash
tm goal add "ship v1"
```

Example output:

```text
created goal 01HX...
```

Goals are first-class records. Events can later point at a goal when they
advance it.

4. Run a debrief.

```bash
tm debrief --transcript-file today.txt --case-date 2026-05-06
```

`tm debrief` reads a transcript from `--transcript-file PATH` or `--from-stdin`,
retains the transcript for `tm reextract`, calls
`DebriefAgent.extract_and_persist`, and persists structured events. It prints
`events_persisted`, novel labels, summary counts, and estimated + actual cost.
`TM_LLM_API_KEY` must be set.

The full pipeline is:

```text
transcript
  -> tm debrief         (DebriefAgent)
  -> events
  -> tm discover        (ProcessMiner: Inductive Miner)
  -> tm variants        (VariantClusterer)
  -> outcome score {0, 1, 2}
  -> tm suggest         (SchedulerAgent)
  -> telemetry
```

5. List goals.

```bash
tm goal list
```

Example output:

```text
ID            STATUS     NAME     PRIORITY  TARGET
--------------------------------------------------
01HX...       active     ship v1  -         -
```

6. List the starter vocabulary.

```bash
tm vocab list
```

You should see the 16 seeded canonical activities, including entries such as
`deep_work`, `meeting`, `exercise`, `learning`, `rest`, and `interruption`.

7. Confirm the events table is empty until a debrief runs.

```bash
python - <<'PY'
import os
import sqlite3
from tm._paths import default_db_path

db = os.environ.get("TM_DB") or str(default_db_path())
with sqlite3.connect(db) as conn:
    count = conn.execute("select count(*) from events").fetchone()[0]
print(f"events: {count}")
PY
```

Fresh workspace output:

```text
events: 0
```

Once a debrief is persisted, `tm discover`, `tm bottlenecks`, and `tm variants`
start reading from the events log.

## Command Index

- `tm init`, bootstrap a workspace by applying migrations and seeding starter
  vocabulary.
- `tm goal add/list/complete/abandon/show`, manage first-class goal pursuit.
- `tm vocab review/list/drift`, govern activity vocabulary. `drift` surfaces
  drifted canonicals (no events for N days) and a novelty rate over a window.
- `tm daemon start/stop/status`, run the Unix-socket single-writer daemon. It
  is optional in v1 single-user CLI use, and required once multiple writers are
  active. The daemon also exposes `run_debrief`, `propose_suggestion`, and
  `rebuild_kuzu_projection` as RPC handlers for cron-driven automation.
- `tm discover`, run Inductive Miner discovery on the events log.
- `tm bottlenecks`, analyze per-activity duration and top direct-follow edges.
- `tm variants [--trend --since YYYY-MM-DD --until YYYY-MM-DD]`, list distinct
  activity sequences ordered by frequency, optionally labeling movement versus
  the previous equal-size window.
- `tm capture telegram|calendar|voice`, import Telegram JSON exports, UTC
  single-instance `.ics` events, or already-transcribed voice notes.
- `tm dashboard [--since YYYY-MM-DD] [--until YYYY-MM-DD]`, show compact local
  event, activity, transcript, and suggestion metrics.
- `tm debrief --transcript-file PATH | --from-stdin`, run the LLM-backed
  debrief extractor against a transcript and persist events.
- `tm export [--output PATH]` and `tm backup --output PATH [--overwrite]`,
  write private JSON exports or SQLite backups.
- `tm privacy redact|forget (--case-date YYYY-MM-DD | --event-id ID)`, redact
  or delete local event/transcript/suggestion data and clear derived Kuzu
  projections.
- `tm reextract --case-date YYYY-MM-DD [--transcript-file PATH]`, replay a
  retained transcript through the current debrief extractor.
- `tm encryption status|set-key`, report SQLCipher/keyring state or store a
  SQLCipher key in keyring for an empty encrypted database.
- `tm web`, run the local browser cockpit UI.
- `tm suggest [--case-goal-id ULID]`, run the scheduler agent for a case date
  and render the suggestion (or skip reason).

## Environment Variables

- `TM_DB`, path to the SQLite database. Default:
  `~/.local/share/tm/tm.db`.
- `TM_LLM_API_KEY`, v1 guard for LLM-backed debrief extraction and scheduler
  suggestions. Current CLI and daemon request paths require this variable to
  be non-empty before backend selection. For `anthropic` and `claude-code`, it
  must be a valid Anthropic key; `claude-code` mirrors it into
  `ANTHROPIC_API_KEY` for the child subprocess only. The `codex` backend ignores
  the value after the guard and uses whatever credentials `codex login`
  configured for the actual model call.
- `TM_LLM_BACKEND`, backend selector. One of:
  - `anthropic` (default): direct Anthropic SDK; reads `TM_LLM_API_KEY`.
  - `codex`: subprocess to the OpenAI Codex CLI (`codex exec --json`); uses
    `codex login` credentials after the shared `TM_LLM_API_KEY` guard passes.
  - `claude-code`: subprocess to the Claude Code CLI
    (`claude --bare --print --output-format json`); reads `TM_LLM_API_KEY`
    (bridged to `ANTHROPIC_API_KEY` for the subprocess).
  - `gemini`: subprocess to the Gemini CLI in plan/json mode.
  - `kimchi`: subprocess to the Kimchi CLI in plan/json mode.
  Unset or empty falls back to `anthropic`. An invalid value fails fast with a
  clear error listing the valid set.
- `TM_LLM_MONTHLY_CAP_USD`, soft cap on monthly LLM spend. Default: `$20`.
- `TM_MAX_PROACTIVE_SUGGESTIONS_PER_DAY`, scheduler rate limit. Default: `1`.
- `TM_SOCKET`, daemon Unix-socket path. Default:
  `~/.local/share/tm/tm.sock`.
- `TM_PID`, daemon PID file path. Default: `~/.local/share/tm/tm.pid`.

The default data directory respects `XDG_DATA_HOME` when it is set, otherwise
it uses `~/.local/share/tm`.

## Architecture At A Glance

- `tm/agents/{debrief,scheduler}.py`, LLM-backed agents for transcript
  extraction and one-action-per-day scheduling.
- `tm/engines/{process_mining,variant_cluster,scheduler_metric,prescriptive_monitoring}.py`,
  pure-Python engines for discovery, variant labeling, scheduler metrics, and
  guardrails.
- `tm/repositories/{events,goals,vocabulary,telemetry,transcripts}.py`,
  per-call `sqlite3` repositories for the durable product records.
- `tm/stores/{sqlite_store,kuzu_store,kuzu_projection}.py`, persistence layer
  for migrations, SQLite storage, Kuzu Petri-net storage, and graph projection.
- `tm/llm/`, provider-neutral `LLMClient` protocol, direct Anthropic adapter,
  subprocess-backed Codex/Claude Code/Gemini/Kimchi adapters, `CostMeter`, and
  typed LLM errors.
- `tm/web/` and `frontend/`, the local browser cockpit API and Vite/React UI.
- `tm/daemon.py`, Unix-socket single-writer process for multi-writer local
  automation and web-triggered LLM workflows.
- `migrations/`, 11 numbered SQL migrations. They are idempotent and
  checksum-verified by the migration runner.

## Known Limitations And v1.x Backlog

See [docs/release/v1.md](docs/release/v1.md) for the canonical backlog.

- ~~Cost ledger under-reports output tokens for `LLMClient.extract`.~~ Closed
  (commit `5869b8c`).
- ~~Daemon Kuzu nightly batch wiring is not connected.~~ Closed — `tm daemon`
  exposes `rebuild_kuzu_projection` as an RPC handler (commit `d234e26`)
  invokable from cron/systemd timer.
- ~~LLM-driven debrief ingestion has no CLI command yet.~~ Closed — `tm
  debrief` ships in commit `c1674fd`; daemon RPC handlers `run_debrief` and
  `propose_suggestion` ship in commit `f2b39b1`.
- ~~Concurrency carry-forward: daemon LLM handlers held the single-writer lock
  for the duration of the LLM call.~~ Closed. The daemon now classifies
  `run_debrief` and `propose_suggestion` as LLM-backed handlers that bypass
  the coarse write lock; the duplicate-summary race this exposed is guarded by
  migration 0010.
- Capture/import surfaces now exist for Telegram JSON exports, iCalendar files,
  and already-transcribed voice notes via `tm capture`.
- `tm dashboard`, `tm export`, `tm backup`, `tm privacy redact`, `tm privacy
  forget`, `tm reextract`, `tm encryption status`, and `tm variants --trend`
  cover the v1.1 local operator surface.
- Full live Telegram bot polling, CalDAV sync, and audio transcription remain
  separate integrations; this branch ships local import/capture commands.

## License / Contributing

License: no license file has been chosen yet.

Contributing: keep changes focused, include tests for behavior changes, and use
the release docs under `docs/release/` as the current project record.
