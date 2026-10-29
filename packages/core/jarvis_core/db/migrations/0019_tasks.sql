-- Jarvis V2 schema, migration 19: delegated tasks (docs/stories/13_delegated_tasks.md).
--
-- A task ties three things together: the chat that asked, the task's own conversation, and the
-- run working in it. Additive only: an older core boots on this database and never reads it.
CREATE TABLE tasks (
    id                     TEXT PRIMARY KEY,
    origin_conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    origin_run_id          TEXT,
    -- "<run id>:<tool call id>" of the call that made it: a resumed run cannot make it twice.
    origin_key             TEXT NOT NULL UNIQUE,
    call_id                TEXT,
    conversation_id        TEXT UNIQUE REFERENCES conversations(id) ON DELETE SET NULL,
    run_id                 TEXT,
    depth                  INTEGER NOT NULL DEFAULT 0,
    title                  TEXT NOT NULL,
    brief                  TEXT NOT NULL,    -- JSON TaskBrief
    state                  TEXT NOT NULL,
    outcome                TEXT,             -- JSON TaskOutcome
    waiting                TEXT,             -- JSON TaskWaiting while a step waits for approval
    -- Set exactly once per round of work: whoever claims it writes the report.
    report_message_id      TEXT,
    incognito              INTEGER NOT NULL DEFAULT 0,
    note                   TEXT,
    created_at             TEXT NOT NULL,
    started_at             TEXT,
    finished_at            TEXT,
    updated_at             TEXT NOT NULL
);
CREATE INDEX tasks_origin ON tasks(origin_conversation_id, created_at);
CREATE INDEX tasks_state ON tasks(state);

-- The report in the chat that asked, and the core's note before it.
ALTER TABLE messages ADD COLUMN task_id TEXT;

-- Started by Arsen's own message: only such a run may hand work to a task.
ALTER TABLE runs ADD COLUMN from_owner INTEGER NOT NULL DEFAULT 0;
-- The call a voice run is a turn of: tasks noted on it start when it ends.
ALTER TABLE runs ADD COLUMN call_id TEXT;
