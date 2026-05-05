-- 0001_init.sql
-- Bootstrap schema for the tm canonical SQLite database.
-- Adds:
--   * schema_migrations: bookkeeping for the migration runner.
--   * events: minimal XES-compatible event log. Later migrations will extend
--     this (e.g. advances_goal, vocab_version) without rewriting it.
--
-- This file is checksummed by the runner; once recorded, do NOT modify it.
-- Add a new NNNN_<slug>.sql instead.

CREATE TABLE IF NOT EXISTS schema_migrations (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL,
    checksum   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    event_id          TEXT PRIMARY KEY,
    case_id           TEXT NOT NULL,
    activity          TEXT NOT NULL,
    timestamp         TEXT NOT NULL,
    lifecycle         TEXT NOT NULL
                          CHECK (lifecycle IN ('start','complete','suspend','resume')),
    resource          TEXT,
    attributes_json   TEXT NOT NULL DEFAULT '{}',
    extractor_version TEXT NOT NULL,
    created_at        TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_events_case_timestamp
    ON events (case_id, timestamp);

CREATE INDEX IF NOT EXISTS idx_events_activity
    ON events (activity);
