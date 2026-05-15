-- 0010_debrief_summary_uniqueness.sql
--
-- T-PM-DEBRIEF-UNIQUE — enforce the single-summary-per-case_date invariant at
-- the DB layer via a partial UNIQUE index.
--
-- Background:
--   /simplify (commit 350a2b1) added `_LLM_METHODS = {"run_debrief", ...}` to
--   tm/daemon.py and changed dispatch so LLM-backed handlers bypass the
--   daemon's coarse write lock. That's the right call for latency — a long
--   running cron-driven debrief no longer blocks a sibling `tm goal add` —
--   but it removes the serialization that previously prevented two concurrent
--   `run_debrief` calls (same case_date) from both passing the "no existing
--   summary" SELECT check and then both INSERTing a `debrief_summary` event.
--   With two rows for the same case_date, `OutcomeAggregator`'s MAX-across-
--   events semantics could silently combine `planned_tasks_completed` from
--   one call with `planned_tasks_total` from another → wrong outcome score.
--
-- Fix (this migration): push the invariant to the storage layer.
--   1. Pre-clean any pre-existing duplicate `debrief_summary` rows (e.g.
--      created in production before this migration shipped). Keep the row
--      with MAX(event_id) per case_date — event_id is a ULID, so MAX picks
--      the most-recently inserted row.
--   2. Create a partial UNIQUE index on `case_date` restricted to
--      `activity = 'debrief_summary'` and `case_date <> ''` (the empty-string
--      "unset" sentinel must not collide with itself).
--
-- After this index exists, concurrent INSERTs of `debrief_summary` for the
-- same case_date will surface as `sqlite3.IntegrityError`. The application
-- layer (DebriefAgent → CLI / daemon RPC envelope) translates that into a
-- typed `DuplicateSummaryError` so callers can render a friendly message.
--
-- Safety notes:
--   * Safe on databases with NO debrief_summary rows at all — the DELETE is a
--     no-op and the CREATE INDEX trivially succeeds.
--   * Safe on databases with pre-existing duplicates — the DELETE prunes them
--     before the UNIQUE index is materialised, so index creation cannot fail
--     with a constraint violation.
--   * Safe on databases with exactly one summary per case_date already — the
--     DELETE leaves every row in place (each row IS its own MAX(event_id)
--     within its case_date group).

DELETE FROM events
WHERE activity = 'debrief_summary'
  AND event_id NOT IN (
    SELECT MAX(event_id) FROM events
    WHERE activity = 'debrief_summary'
    GROUP BY case_date
  );

CREATE UNIQUE INDEX uq_events_summary_per_case_date
ON events (case_date)
WHERE activity = 'debrief_summary' AND case_date <> '';
