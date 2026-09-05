-- Jarvis V2 schema, migration 3: meeting auto-RSVP ledger (docs/stories/08_meeting_rsvp.md).

-- One row per answered invite occurrence. The key is organizer|subject|start, NOT the EntryID:
-- Outlook re-explodes recurring occurrences with fresh ids on every poll, and a responded
-- occurrence does not reliably flip its ResponseStatus, so an id-keyed ledger answered the
-- same series again and again in V1. This key makes "answer once, ever" a database fact.
CREATE TABLE rsvp_decisions (
    key       TEXT PRIMARY KEY,
    account   TEXT NOT NULL,
    subject   TEXT NOT NULL,
    organizer TEXT NOT NULL,
    start     TEXT NOT NULL,
    decision  TEXT NOT NULL,   -- accept | decline | accept_vip_conflict | left_external | failed
    detail    TEXT,
    at        TEXT NOT NULL
);

CREATE TABLE rsvp_state (
    account                TEXT PRIMARY KEY,
    last_run_at            TEXT,
    last_error             TEXT,
    answered_total         INTEGER NOT NULL DEFAULT 0,
    removed_canceled_total INTEGER NOT NULL DEFAULT 0
);
