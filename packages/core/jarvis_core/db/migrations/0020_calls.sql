-- Jarvis V2 schema, migration 20: calls, for tasks noted on them (docs/stories/13_delegated_tasks.md).
--
-- A call lives in the web page; the core learns of it from its turns, a heartbeat and the
-- hang-up. One row per call: open while ended_at is NULL. Ending it is one conditional UPDATE,
-- so a second hang-up, a reconnect or the drop sweeper finds nothing left to end.
CREATE TABLE calls (
    id                TEXT PRIMARY KEY,           -- the web's id for the call
    conversation_id   TEXT,
    started_at        TEXT NOT NULL,
    last_seen_at      TEXT NOT NULL,
    ended_at          TEXT,
    end_reason        TEXT,                       -- "hangup" | "drop"
    -- The one after-call report, set exactly once.
    report_message_id TEXT
);
CREATE INDEX calls_open ON calls(ended_at);
CREATE INDEX tasks_call ON tasks(call_id) WHERE call_id IS NOT NULL;
