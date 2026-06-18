-- Capture, transcript retention, and privacy audit tables.

CREATE TABLE transcripts (
    case_date TEXT PRIMARY KEY,
    transcript_text TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'debrief',
    extractor_version TEXT,
    recorded_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_transcripts_recorded_at ON transcripts(recorded_at DESC);

CREATE TABLE privacy_actions (
    action_id TEXT PRIMARY KEY,
    action_type TEXT NOT NULL CHECK (action_type IN ('redact', 'forget')),
    selector TEXT NOT NULL,
    affected_events INTEGER NOT NULL,
    affected_transcripts INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_privacy_actions_created_at ON privacy_actions(created_at DESC);
