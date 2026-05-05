-- 0002_cost_ledger.sql
-- Cost ledger for LLM API calls. Pre-call gate sums monthly est_cost_usd
-- to enforce the configured monthly_cap. Single-quoted string literals and
-- -- line comments only -- the T-FND-02 SQL splitter does not handle
-- block comments, double-quoted identifiers, or BEGIN..END trigger bodies.

CREATE TABLE cost_ledger (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    model TEXT NOT NULL,
    input_tokens INTEGER NOT NULL,
    output_tokens INTEGER NOT NULL,
    est_cost_usd REAL NOT NULL,
    request_kind TEXT NOT NULL CHECK (request_kind IN ('chat','extract','tool_call'))
);

CREATE INDEX idx_cost_ledger_ts ON cost_ledger(ts);
CREATE INDEX idx_cost_ledger_model_ts ON cost_ledger(model, ts);
