-- Jarvis V2 schema, migration 16: chunks a recording device has already delivered.
-- The phone uploads each piece of a meeting until it is acknowledged; a retry after a lost
-- answer must not transcribe the same piece twice.
CREATE TABLE meeting_chunks (
    meeting_id TEXT NOT NULL REFERENCES meetings(id) ON DELETE CASCADE,
    n          INTEGER NOT NULL,
    segments   INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (meeting_id, n)
);
