-- Not a migration — a diagnostic to run BEFORE 0006_admin_notifications.sql.
--
-- An admin_notifications table already exists in at least one environment,
-- created outside these files, and nothing in either repo references it. 0006
-- is written to adapt to it, but it cannot fix one thing: a NOT NULL column
-- that app/lib/notifications.py's create_admin_notification doesn't populate
-- will reject every insert.
--
-- Run this and check the output. Anything listed as NOT NULL with no default,
-- other than id / school_id / type / message, needs either a default or a
-- decision about what to write into it.
SELECT
  column_name,
  data_type,
  is_nullable,
  column_default
FROM information_schema.columns
WHERE table_name = 'admin_notifications'
ORDER BY ordinal_position;
