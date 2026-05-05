-- XES-compatible additive columns on events: case lens (workday vs goal-pursuit) and version tags.
-- The formal REFERENCES goals(goal_id) FK constraint and full table rebuild are deferred to a
-- future infrastructure task that modifies the migration runner to support pre-txn pragmas.
ALTER TABLE events ADD COLUMN vocab_version TEXT;
ALTER TABLE events ADD COLUMN schema_version TEXT NOT NULL DEFAULT 'v1';
ALTER TABLE events ADD COLUMN case_date TEXT NOT NULL DEFAULT '';
ALTER TABLE events ADD COLUMN case_goal_id TEXT;
CREATE INDEX idx_events_case_date ON events(case_date) WHERE case_date <> '';
CREATE INDEX idx_events_case_goal_id ON events(case_goal_id) WHERE case_goal_id IS NOT NULL;
CREATE INDEX idx_events_schema_version ON events(schema_version);
