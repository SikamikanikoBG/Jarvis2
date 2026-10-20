-- Jarvis V2 schema, migration 18: the timezone of the device a run was sent from, so the turn
-- is told the time where Arsen is (review 2026-10-19: Munich airport, times read in UTC).
ALTER TABLE runs ADD COLUMN timezone TEXT;
