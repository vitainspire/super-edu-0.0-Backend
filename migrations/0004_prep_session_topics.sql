-- Pins the syllabus topic taught in one specific timetable period on one
-- specific day, instead of recomputing "today's topic" fresh (first
-- not-yet-completed syllabus topic) every time a teacher opens Prep Material.
--
-- Without this, a class with two periods of the same subject on the same day
-- had no way to tell "reopening period 1's prep material" apart from "opening
-- period 2's prep material" — both just asked "what's the first incomplete
-- topic?", and since generating period 1's lesson immediately marks that
-- topic complete, reopening period 1 later the same day would wrongly show
-- period 2's topic instead of period 1's own.
--
-- (class_id, subject, session_date, period_number) is the full identity: the
-- first time a period is opened that day, its topic gets pinned here; every
-- later open of that same period that same day reads the pin back instead of
-- recomputing, so it stays stable. A different period_number the same day
-- still recomputes fresh (correctly advancing to the next topic).

CREATE TABLE IF NOT EXISTS prep_session_topics (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  class_id uuid NOT NULL,
  subject text NOT NULL,
  session_date date NOT NULL,
  period_number int NOT NULL,
  topic text NOT NULL,
  subtopic text,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_prep_session_topics_identity
  ON prep_session_topics (class_id, subject, session_date, period_number);
