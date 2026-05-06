# tm

`tm` is a personal process-mining and outcome-scoring CLI for builders who
already reflect on their day in free prose, but want a durable event log and a
small amount of decision support from it. An end-of-day debrief becomes
structured events in SQLite, process-mining engines find repeated patterns,
days are scored as `{0, 1, 2}`, and the scheduler suggests at most one concrete
action per day.

## Status

![status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)

Pre-alpha. v1 shipped on 2026-05-06; original caveats are closed and the
operator-surface gap (no `tm debrief` / `tm suggest`) was closed in the same
window. The end-to-end CLI path is now reachable: `tm init → goal add →
debrief transcript.txt → discover → suggest`. Expect rough edges before using
it as a daily tool, but the first-user pipeline is no longer programmatic-only.

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
  applied 9 migrations
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
calls `DebriefAgent.extract_and_persist`, and persists structured events. It
prints `events_persisted`, novel labels, summary counts, and estimated +
actual cost. `TM_LLM_API_KEY` must be set.

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
- `tm variants`, list distinct activity sequences ordered by frequency.
- `tm debrief --transcript-file PATH | --from-stdin`, run the LLM-backed
  debrief extractor against a transcript and persist events.
- `tm suggest [--case-goal-id ULID]`, run the scheduler agent for a case date
  and render the suggestion (or skip reason).

## Environment Variables

- `TM_DB`, path to the SQLite database. Default:
  `~/.local/share/tm/tm.db`.
- `TM_LLM_API_KEY`, Anthropic API key. Required for debrief extraction and
  scheduler suggestions.
- `TM_LLM_BACKEND`, backend selector from the design. v1 effectively supports
  only `anthropic`, which is the default backend.
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
- `tm/repositories/{events,goals,vocabulary,telemetry}.py`, per-call `sqlite3`
  repositories for the durable product records.
- `tm/stores/{sqlite_store,kuzu_store,kuzu_projection}.py`, persistence layer
  for migrations, SQLite storage, Kuzu Petri-net storage, and graph projection.
- `tm/llm/`, provider-neutral `LLMClient` protocol, `AnthropicAdapter`,
  `CostMeter`, and typed LLM errors.
- `tm/daemon.py`, Unix-socket single-writer scaffold for future multi-writer
  scenarios.
- `migrations/`, 9 numbered SQL migrations. They are idempotent and
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
- **Concurrency carry-forward:** the daemon's `run_debrief` and
  `propose_suggestion` handlers hold the single-writer lock for the duration
  of the LLM call (seconds). Acceptable for single-operator v1 traffic;
  future fix releases the lock around the LLM round-trip.
- **Multiday variant clustering** is open. The scheduler windows 14 days, but
  `VariantClusterer` only labels single cases.

## License / Contributing

License: no license file has been chosen yet.

Contributing: keep changes focused, include tests for behavior changes, and use
the release docs under `docs/release/` as the current project record.
