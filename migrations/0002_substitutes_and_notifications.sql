-- Adds an approval gate to teacher-submitted leave requests, and a minimal
-- notification outbox for substitute-coverage alerts.
--
-- teacher_availability.status: 'pending' | 'approved' | 'rejected'.
-- Defaults to 'approved' so every EXISTING row (and every row written by the
-- admin override / same-day check-in paths, which stay instant) reads as
-- already-effective with no backfill needed. Only the teacher-submitted
-- multi-day /leaves request now creates rows with status = 'pending'.
ALTER TABLE teacher_availability ADD COLUMN status text NOT NULL DEFAULT 'approved';

CREATE TABLE teacher_notifications (
  id uuid PRIMARY KEY,
  teacher_id uuid NOT NULL REFERENCES teachers(id),
  type text NOT NULL,
  message text NOT NULL,
  class_id uuid REFERENCES classes(id),
  date date,
  read_at timestamptz,
  created_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX teacher_notifications_teacher_id_idx ON teacher_notifications (teacher_id, created_at DESC);
