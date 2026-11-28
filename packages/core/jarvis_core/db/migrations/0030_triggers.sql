-- Jarvis V2 schema, migration 30: triggers - something happened, so act (features/triggers.py,
-- 2026-11-28). A webhook Jarvis made for a script, arriving at a place, staying there, heading
-- home; each with a time window, a cooldown, a daily cap and a history of why it fired or not.
CREATE TABLE triggers (
    id          TEXT PRIMARY KEY,               -- trg_...
    name        TEXT NOT NULL,
    project_id  TEXT,
    enabled     INTEGER NOT NULL DEFAULT 1,
    source      TEXT NOT NULL,                  -- webhook | location
    config      TEXT NOT NULL DEFAULT '{}',     -- webhook: {secret}; location: {event, place_id, dwell_min}
    win         TEXT,                           -- {days:[0..6], from:"HH:MM", to:"HH:MM"} or NULL
    action      TEXT NOT NULL,                  -- {kind: prompt|schedule|notify, ...}
    cooldown_s  INTEGER NOT NULL,
    daily_cap   INTEGER NOT NULL,
    created_by  TEXT,                           -- NULL = Arsen; a run id = waits for his Accept
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE trigger_fires (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    trigger_id      TEXT NOT NULL,
    at              TEXT NOT NULL,
    outcome         TEXT NOT NULL,              -- fired | cooldown | cap | busy | window | chain | breaker | pending | off | failed
    why             TEXT NOT NULL DEFAULT '',
    payload         TEXT,
    run_id          TEXT,
    conversation_id TEXT
);
CREATE INDEX trigger_fires_by ON trigger_fires(trigger_id, at);

-- Small state the engine keeps between fixes and restarts: whether he is inside each place, the
-- approach being watched, the breaker. key -> JSON.
CREATE TABLE trigger_state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
