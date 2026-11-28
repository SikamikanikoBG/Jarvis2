-- Jarvis V2 schema, migration 27: where Arsen is (features/location.py, 2026-11-28).
--
-- Only the LAST fix is kept - one row, overwritten - never a trail of where he has been.
-- Places are the ones he named (Home, Office) so a turn says "at home", not an address.
-- Additive only: an older core ignores both tables.
CREATE TABLE location_last (
    id          INTEGER PRIMARY KEY CHECK (id = 1),
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    accuracy_m  REAL,
    speed_mps   REAL,
    source      TEXT NOT NULL,
    taken_at    TEXT NOT NULL,
    received_at TEXT NOT NULL
);

CREATE TABLE places (
    id         TEXT PRIMARY KEY,               -- plc_...
    name       TEXT NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    radius_m   REAL NOT NULL DEFAULT 150,
    created_at TEXT NOT NULL
);
