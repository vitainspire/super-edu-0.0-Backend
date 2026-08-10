-- School-scoped alert inbox for the admin portal.
--
-- Separate from teacher_notifications rather than a nullable-teacher column on
-- it, because the two have different addressing. A teacher notification is
-- personal mail: "you are covering period 4". An admin notification is a work
-- item belonging to the SCHOOL: "period 4 has no cover". Any admin can act on
-- it, and once one of them has, it's done for all of them — so read_at is
-- shared, not per-admin. A school with three admins should not need three
-- people to dismiss the same uncovered period.
--
-- Written idempotently. A table of this name already existed in at least one
-- environment, created outside these migrations, so this has to be safe to run
-- against both a fresh database and one that already has some version of it —
-- hence IF NOT EXISTS throughout and per-column ADD COLUMN rather than a
-- single CREATE that assumes nothing is there.
CREATE TABLE IF NOT EXISTS admin_notifications (
  id uuid PRIMARY KEY,
  school_id uuid NOT NULL REFERENCES schools(id),
  type text NOT NULL,
  message text NOT NULL,
  date date,
  class_id uuid REFERENCES classes(id),
  period_number int,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);

-- Brings a pre-existing table up to the shape app/lib/notifications.py writes.
-- Every column the code touches is listed, not just the ones new in this
-- migration, because the table that already exists was created outside these
-- files and its shape is unknown — this has to converge it either way.
--
-- All added nullable. NOT NULL cannot be applied to a table that already holds
-- rows without a backfill, and guessing a backfill for data we can't see would
-- be worse than a permissive column. On a fresh database the CREATE above
-- supplies the stricter constraints and these are all no-ops.
--
--   school_id / type / message — the alert itself.
--   date / class_id / period_number — what it's about, so the bell can
--     deep-link to the page that fixes it and so a superseded alert can be
--     found and cleared automatically.
--   read_at — acknowledged by an admin. Distinct from the alert becoming moot
--     on its own: an uncovered period that later gets a substitute is
--     auto-resolved (see resolve_admin_notifications_for_slot) so the inbox
--     doesn't accumulate alerts for problems that already fixed themselves.
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS school_id uuid;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS type text;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS message text;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS date date;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS class_id uuid;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS period_number int;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS read_at timestamptz;
ALTER TABLE admin_notifications ADD COLUMN IF NOT EXISTS created_at timestamptz NOT NULL DEFAULT now();

CREATE INDEX IF NOT EXISTS admin_notifications_school_idx ON admin_notifications (school_id, created_at DESC);

-- Supports the auto-resolve lookup, which is by exactly this slot tuple.
CREATE INDEX IF NOT EXISTS admin_notifications_slot_idx ON admin_notifications (school_id, date, class_id, period_number);
