-- !pre-txn: PRAGMA foreign_keys = OFF;
-- !post-txn: PRAGMA foreign_keys = ON;

-- Rebuild goals to add CHECK (length(trim(name)) > 0). SQLite ALTER TABLE
-- cannot add CHECK constraints, so we use the standard 5-step rebuild dance:
-- create _new with the constraint, copy data, drop old, rename new, recreate indexes.

CREATE TABLE goals_new (
    goal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name)) > 0),
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'completed', 'abandoned')),
    priority INTEGER CHECK (priority IS NULL OR priority BETWEEN 1 AND 3),
    target_completion_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    abandoned_at TEXT,
    abandon_reason TEXT
);

INSERT INTO goals_new (goal_id, name, description, status, priority, target_completion_at, created_at, completed_at, abandoned_at, abandon_reason)
SELECT goal_id, name, description, status, priority, target_completion_at, created_at, completed_at, abandoned_at, abandon_reason
FROM goals;

DROP TABLE goals;
ALTER TABLE goals_new RENAME TO goals;

CREATE INDEX idx_goals_status ON goals(status);
CREATE INDEX idx_goals_created_at ON goals(created_at DESC);
