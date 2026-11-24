-- Long-term memory, step 2 (memory-track PROPOSAL §6.2): what happened, as episodes.
-- An episode is a stretch of one conversation about one thing on one day: when, who and where,
-- what was said and decided, and what it meant to Arsen (importance, valence). It is written in the
-- background from the conversation's own messages, which stay: nothing here replaces them.
-- Additive only: an older core boots on this database and never reads it.
CREATE TABLE episodes (
    id              INTEGER PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    day             TEXT NOT NULL,              -- YYYY-MM-DD, his time zone
    started_at      TEXT NOT NULL,              -- UTC
    ended_at        TEXT NOT NULL,
    title           TEXT NOT NULL,
    what            TEXT NOT NULL,
    people          TEXT NOT NULL DEFAULT '[]', -- JSON list, each once, every spelling it was said in
    places          TEXT NOT NULL DEFAULT '[]',
    importance      INTEGER NOT NULL DEFAULT 2, -- 1 (routine) .. 5 (he would remember it for years)
    valence         TEXT NOT NULL DEFAULT 'neutral', -- threat | risk | opportunity | reward | loss | neutral
    first_run_id    TEXT,
    last_run_id     TEXT,                       -- the runs up to this one are in it
    updated_at      TEXT NOT NULL
);
CREATE INDEX episodes_by_conversation ON episodes(conversation_id, day);
CREATE INDEX episodes_by_time ON episodes(started_at);

CREATE VIRTUAL TABLE episodes_fts USING fts5(
    title, what, people, places, tokenize = 'unicode61 remove_diacritics 2', prefix = '3 4');

CREATE TRIGGER episodes_in AFTER INSERT ON episodes
BEGIN
    INSERT INTO episodes_fts(rowid, title, what, people, places)
    VALUES (new.id, new.title, new.what, new.people, new.places);
END;
CREATE TRIGGER episodes_changed AFTER UPDATE ON episodes
BEGIN
    UPDATE episodes_fts SET title = new.title, what = new.what, people = new.people, places = new.places
    WHERE rowid = new.id;
END;
CREATE TRIGGER episodes_gone AFTER DELETE ON episodes
BEGIN
    DELETE FROM episodes_fts WHERE rowid = old.id;
END;
