-- Closes the post-class feedback loop: lesson_feedback was write-only before
-- this (nothing ever read it back). Purely additive — scripts/seed-demo-analytics.js
-- still writes engagement/comprehension/pacing directly, so those columns stay
-- as-is rather than being restructured.
--
-- Apply manually in the Supabase SQL editor (no migration runner/DB connection
-- string is available to apply this automatically).

ALTER TABLE lesson_feedback ADD COLUMN IF NOT EXISTS responses jsonb;      -- [{question, answer}] when tailored check-in questions were shown instead of the 3 fixed ones
ALTER TABLE lesson_feedback ADD COLUMN IF NOT EXISTS insight text;          -- interpreted takeaway, filled in after submission only if something was flagged

ALTER TABLE lesson_feedback ALTER COLUMN engagement DROP NOT NULL;
ALTER TABLE lesson_feedback ALTER COLUMN comprehension DROP NOT NULL;
ALTER TABLE lesson_feedback ALTER COLUMN pacing DROP NOT NULL;              -- null when `responses` was used instead of these 3 fixed columns

ALTER TABLE classes ADD COLUMN IF NOT EXISTS feedback_profile text;        -- running per-class tendency summary, updated in place as feedback comes in
