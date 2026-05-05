-- !pre-txn: PRAGMA foreign_keys = OFF;
-- !post-txn: PRAGMA foreign_keys = ON;

-- Tighten goals.name CHECK to reject ANY whitespace-only name (tabs, newlines,
-- carriage returns, spaces). Mirrors Python str.strip() which handles all of
-- these. SQLite's default trim() only strips ASCII 0x20.
--
-- Same table-rebuild dance as 0008.

CREATE TABLE goals_new (
    goal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL CHECK (length(trim(name, char(9)||char(10)||char(13)||char(32))) > 0),
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
