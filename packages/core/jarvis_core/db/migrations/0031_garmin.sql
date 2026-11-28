-- Jarvis V2 schema, migration 31: his Garmin data (features/garmin.py, 2026-11-28).
-- What the Garmin Personal API returned, per day and kind, as it came (the field names are
-- Garmin's and are read defensively). Kept settings.garmin.keep_days (90) for the baselines.
CREATE TABLE garmin_raw (
    day        TEXT NOT NULL,                  -- YYYY-MM-DD, his calendar day
    kind       TEXT NOT NULL,                  -- summary | sleep | body_battery | stress | hrv | heart | activities
    data       TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
