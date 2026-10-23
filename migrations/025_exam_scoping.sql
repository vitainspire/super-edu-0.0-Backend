-- Lets an admin-scheduled "exam" calendar event (academic_events) optionally
-- scope itself to specific grades, and lets a teacher's test (tests) record
-- which scheduled exam it fulfilled -- the two pieces behind the progress
-- report's "scheduled exam" placeholders (app/lib/exam_matching.py).
--
-- academic_events.grades is nullable, JSON-encoded text (same convention as
-- tests.questions -- not a native array/jsonb column): NULL means "every
-- grade", which is also what every row written before this migration, and
-- every holiday/term category event (which never sets this field), means.
--
-- tests.exam_event_id is a plain nullable uuid with no FK constraint,
-- mirroring shared_prep_materials.book_id's loose-coupling convention
-- (migration 024) rather than a hard reference -- app/lib/exam_matching.py
-- is the only writer, and a dangling id (the calendar event later deleted)
-- should just mean "no longer shown as scheduled", not break the test row
-- it's attached to.

alter table academic_events
    add column if not exists grades text;

alter table tests
    add column if not exists exam_event_id uuid;

create index if not exists tests_exam_event_id_idx
    on tests (exam_event_id);
