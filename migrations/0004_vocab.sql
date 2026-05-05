-- Vocabulary governance: canonical activity names + free-text aliases.
CREATE TABLE vocabulary (
    activity_name TEXT PRIMARY KEY,
    description TEXT,
    vocab_version TEXT NOT NULL DEFAULT 'vocab_v1',
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','archived'))
);

CREATE INDEX idx_vocabulary_status ON vocabulary(status);
CREATE INDEX idx_vocabulary_vocab_version ON vocabulary(vocab_version);

CREATE TABLE aliases (
    free_text_variant TEXT PRIMARY KEY,
    canonical_activity TEXT NOT NULL REFERENCES vocabulary(activity_name) ON DELETE RESTRICT,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX idx_aliases_canonical ON aliases(canonical_activity);
