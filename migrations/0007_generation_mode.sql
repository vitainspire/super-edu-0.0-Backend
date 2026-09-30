-- Per-(grade, subject) generation mode: whether Prep Material generation for
-- this grade+subject stays on the cheap shared-batch default ("opt_in" — a
-- teacher can still personalize a single topic on demand) or switches to
-- always generating fresh per-teacher lessons ("full_personalization").
--
-- Lives on grade_subject_feedback_profiles rather than a new table — it's
-- already the one row per (school_id, grade, subject), which is exactly the
-- scope this setting needs.
--
-- mode_set_by distinguishes an admin's deliberate choice from the system's
-- own suggestion: the auto-similarity check (comparing assigned teachers'
-- personalization profiles) is only ever allowed to write here when
-- mode_set_by = 'auto' — once an admin sets it explicitly, recomputation
-- becomes advisory only and must never silently overwrite that choice.
ALTER TABLE grade_subject_feedback_profiles
  ADD COLUMN IF NOT EXISTS generation_mode text NOT NULL DEFAULT 'opt_in',
  ADD COLUMN IF NOT EXISTS mode_set_by text NOT NULL DEFAULT 'auto',
  ADD COLUMN IF NOT EXISTS mode_updated_at timestamptz NOT NULL DEFAULT now();

-- Which specific saved lesson a teacher is looking at: a plain cached copy of
-- the shared lesson ('shared'), an explicit "make this mine" personalization
-- ('personal'), or a one-off live generation because nothing shared existed
-- yet ('live_fallback'). Only 'personal' rows are ever preferred over the
-- shared pool on a later fetch — a mere cache of the shared lesson must not
-- be mistaken for a teacher's deliberate override.
ALTER TABLE prep_materials
  ADD COLUMN IF NOT EXISTS source text NOT NULL DEFAULT 'shared';

-- A minimal admin-facing counterpart to teacher_notifications (migration
-- 0002) — same shape, scoped to school_id instead of teacher_id, since no
-- admin notification outbox exists yet at all.
CREATE TABLE IF NOT EXISTS admin_notifications (
  id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  school_id uuid NOT NULL,
  type text NOT NULL,
  message text NOT NULL,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_admin_notifications_school
  ON admin_notifications (school_id, created_at DESC);
