-- Shared prep-material batches.
--
-- The syllabus is already authored once per (school, grade, subject), not per
-- class/section (see admin_grade_syllabus.py's definition_id fan-out — one
-- definition_id, one row per section). Prep-material generation moves to the
-- same granularity: generate a batch of upcoming topics' lessons ONCE per
-- (school, grade, subject), and every section teaching that grade+subject
-- (5A, 5B, ...) fetches from the same generated stock instead of each
-- triggering its own generation. Feedback still happens per class/session as
-- before; it gets pooled across every class sharing a batch to shape the
-- *next* batch for that same grade+subject.

CREATE TABLE IF NOT EXISTS prep_batches (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id uuid NOT NULL,
  grade text NOT NULL,
  subject text NOT NULL,
  status text NOT NULL DEFAULT 'pending', -- pending | running | done | error
  topic_count int NOT NULL DEFAULT 0,
  completed_count int NOT NULL DEFAULT 0,
  error text,
  created_at timestamptz NOT NULL DEFAULT now(),
  completed_at timestamptz
);

CREATE INDEX IF NOT EXISTS idx_prep_batches_lookup
  ON prep_batches (school_id, grade, subject, status);

CREATE TABLE IF NOT EXISTS shared_prep_materials (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id uuid NOT NULL,
  grade text NOT NULL,
  subject text NOT NULL,
  topic_definition_id uuid NOT NULL,
  topic text NOT NULL,
  subtopic text,
  order_index int NOT NULL DEFAULT 0,
  lesson jsonb NOT NULL,
  batch_id uuid REFERENCES prep_batches(id) ON DELETE SET NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- One generated lesson per topic per (school, grade, subject) — this is the
-- constraint that actually makes "5A and 5B share one generation" true rather
-- than aspirational; a second insert for the same topic just overwrites.
CREATE UNIQUE INDEX IF NOT EXISTS idx_shared_prep_materials_topic
  ON shared_prep_materials (school_id, grade, subject, topic_definition_id);

CREATE INDEX IF NOT EXISTS idx_shared_prep_materials_batch
  ON shared_prep_materials (batch_id);

-- Pooled feedback tendency profile for a whole grade+subject cohort — the
-- same free-text-paragraph idea as classes.feedback_profile (migration 0001),
-- but there is no single class row that represents "grade 5 maths in
-- general" once generation is shared, so this is a new table rather than a
-- new column on an existing one.
CREATE TABLE IF NOT EXISTS grade_subject_feedback_profiles (
  school_id uuid NOT NULL,
  grade text NOT NULL,
  subject text NOT NULL,
  profile text,
  updated_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (school_id, grade, subject)
);
