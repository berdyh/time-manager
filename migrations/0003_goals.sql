-- Goals table for first-class goal pursuit.
CREATE TABLE goals (
    goal_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','completed','abandoned')),
    priority INTEGER CHECK (priority IS NULL OR priority BETWEEN 1 AND 3),
    target_completion_at TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now')),
    completed_at TEXT,
    abandoned_at TEXT,
    abandon_reason TEXT
);

CREATE INDEX idx_goals_status ON goals(status);
CREATE INDEX idx_goals_created_at ON goals(created_at DESC);
