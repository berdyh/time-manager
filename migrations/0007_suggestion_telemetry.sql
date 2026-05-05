-- Suggestion telemetry: scheduler PrPM logs predicted vs actual outcomes for delta-outcome learning.
CREATE TABLE suggestion_telemetry (
    suggestion_id TEXT PRIMARY KEY,
    suggested_at TEXT NOT NULL,
    case_date TEXT NOT NULL,
    case_goal_id TEXT,
    recommended_action TEXT NOT NULL,
    predicted_outcome_with REAL NOT NULL,
    predicted_outcome_without REAL NOT NULL,
    predicted_outcome_delta REAL NOT NULL,
    conformance_deviation REAL,
    actual_outcome INTEGER CHECK (actual_outcome IS NULL OR actual_outcome IN (0, 1, 2)),
    explicit_thumbs INTEGER CHECK (explicit_thumbs IS NULL OR explicit_thumbs IN (0, 1)),
    llm_explanation_text TEXT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_suggestion_telemetry_suggested_at ON suggestion_telemetry(suggested_at);
CREATE INDEX idx_suggestion_telemetry_case_date ON suggestion_telemetry(case_date);
CREATE INDEX idx_suggestion_telemetry_case_goal_id ON suggestion_telemetry(case_goal_id) WHERE case_goal_id IS NOT NULL;
CREATE INDEX idx_suggestion_telemetry_actual_outcome ON suggestion_telemetry(actual_outcome) WHERE actual_outcome IS NOT NULL;
