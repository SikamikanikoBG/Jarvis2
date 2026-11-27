-- Long-term memory, step 3 (memory-track PROPOSAL §6.3): the days before episodes existed.
-- One row per conversation and day the back-fill has worked on: how far it got (the last run it
-- wrote up) and whether the day is finished. A routine day leaves no episode, so without this
-- row it would be asked about again every night. Additive only: an older core never reads it.
CREATE TABLE episode_backfill (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,              -- YYYY-MM-DD, his time zone
    last_run_id     TEXT,                       -- the runs up to this one are written up
    tries           INTEGER NOT NULL DEFAULT 0, -- answers that could not be read, for the next batch
    done            INTEGER NOT NULL DEFAULT 0,
    updated_at      TEXT NOT NULL,
    PRIMARY KEY (conversation_id, day)
);
