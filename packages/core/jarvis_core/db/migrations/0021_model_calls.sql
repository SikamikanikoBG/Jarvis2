-- Jarvis V2 schema, migration 21: model_calls, one row per model call (idea-to-uat/context-usage).
--
-- Written by the context ledger when a call ends (models/ledger.py). Each row keeps what was true
-- at call time - endpoint, model, window, who it was for - so the history stays right after the
-- settings change or the conversation is deleted. Times are epoch milliseconds. Pruned after
-- settings.context_usage_retention_days.
CREATE TABLE model_calls (
    id                TEXT PRIMARY KEY,
    queued_at         INTEGER NOT NULL,           -- asked for its endpoint's slot
    started_at        INTEGER,                    -- got the slot (NULL: failed while waiting)
    ended_at          INTEGER NOT NULL,
    endpoint          TEXT NOT NULL,
    model             TEXT NOT NULL,
    role              TEXT,
    source            TEXT NOT NULL,              -- chat | voice | scheduled | task | triage | mail | meeting | collab | system | housekeeping
    label             TEXT,                       -- schedule name / housekeeping purpose
    run_id            TEXT,
    conversation_id   TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    estimated         INTEGER NOT NULL DEFAULT 0, -- 1: the server reported no usage, chars/3.2
    ctx_window        INTEGER,                    -- the window the call was sent into; NULL unknown
    outcome           TEXT NOT NULL,              -- ok | failed | cancelled
    trimmed_tokens    INTEGER NOT NULL DEFAULT 0, -- history dropped to fit, before the call
    shrunk            INTEGER NOT NULL DEFAULT 0, -- output room cut down after a too-long 400
    rejected          INTEGER NOT NULL DEFAULT 0, -- refused: prompt too long for the context
    cut               INTEGER NOT NULL DEFAULT 0, -- the answer stopped at the output limit
    error             TEXT
);
CREATE INDEX model_calls_time ON model_calls(queued_at);
