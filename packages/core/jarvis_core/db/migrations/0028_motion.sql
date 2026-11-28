-- Jarvis V2 schema, migration 28: still or moving, and how (features/location.py, 2026-11-28).
--
-- The previous fix is kept beside the last one - only to measure speed and "since when" - and
-- is overwritten with it: still no trail. Additive only.
ALTER TABLE location_last ADD COLUMN prev_lat REAL;
ALTER TABLE location_last ADD COLUMN prev_lon REAL;
ALTER TABLE location_last ADD COLUMN prev_accuracy_m REAL;
ALTER TABLE location_last ADD COLUMN prev_taken_at TEXT;
ALTER TABLE location_last ADD COLUMN motion TEXT;            -- still|walking|running|cycling|vehicle|flying|unknown
ALTER TABLE location_last ADD COLUMN motion_since TEXT;
ALTER TABLE location_last ADD COLUMN motion_speed_mps REAL;
