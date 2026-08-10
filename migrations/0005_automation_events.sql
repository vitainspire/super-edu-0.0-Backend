-- Audit log for the absence/substitution automation (app/lib/events.py).
--
-- timetable_substitutions holds only the CURRENT assignment for a slot — it's
-- upserted in place on every recomputation. That answers "who is covering 7B
-- period 4 on Tuesday" but not "why", and not "who was on it before". This
-- table is the append-only record of the decisions: absences recorded, covers
-- assigned, covers cancelled, periods nobody could take.
--
-- Deliberately not foreign-keyed to teachers/classes. It's history: a row must
-- survive the deletion of the teacher it refers to, and an event that couldn't
-- be written because a referenced row had already gone would be worse than
-- useless. Identifiers live inside `payload` and are resolved on read.
-- IF NOT EXISTS throughout: these migrations are applied by hand, so re-running
-- one must be a no-op rather than an error.
CREATE TABLE IF NOT EXISTS automation_events (
  id uuid PRIMARY KEY,
  type text NOT NULL,
  school_id uuid,
  -- The date the event is ABOUT (the absence date), not when it was recorded —
  -- that's created_at. A leave approved on Monday for Friday has date=Friday.
  date date,
  payload jsonb NOT NULL DEFAULT '{}'::jsonb,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Covers the two reads this table has: an admin scrolling a school's recent
-- automation activity, and "what happened for this school on this date".
CREATE INDEX IF NOT EXISTS automation_events_school_created_idx ON automation_events (school_id, created_at DESC);
CREATE INDEX IF NOT EXISTS automation_events_school_date_idx ON automation_events (school_id, date);
