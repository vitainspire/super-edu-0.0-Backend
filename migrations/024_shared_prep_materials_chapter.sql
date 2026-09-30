-- Which real textbook chapter a shared_prep_materials row came from.
--
-- Nothing here before this migration recorded it. save_published_chapter_
-- lessons (app/lib/prep_batch_jobs.py) knows book_id and chapter_number for
-- the whole run it's persisting -- generate_lessons_from_published_book's
-- own result even carries chapterTitle -- but none of the three ever made it
-- onto the row itself. Every topic from every chapter for a grade+subject
-- landed in the same flat list with nothing distinguishing "from Agriculture-
-- Crops" from "from Sense Organs" except human memory of when it was run.
--
-- All three columns are nullable, deliberately: rows saved before this
-- migration (and anything the OLDER, non-book engine still writes via
-- prep_batches/batch_id -- see that column's own comment on this table)
-- simply have no chapter to group by. A NULL chapter_title is "not tagged",
-- never "tagged as nothing" -- callers group those under their own
-- ungrouped/legacy bucket rather than treating NULL as a real chapter name.

alter table shared_prep_materials
    add column if not exists book_id        text,
    add column if not exists chapter_number integer,
    add column if not exists chapter_title  text;

-- "Every topic from this chapter, for this school+grade+subject" -- the
-- query the grouped Browse My/Shared Library view runs once per chapter
-- heading instead of once per topic.
create index if not exists shared_prep_materials_chapter_idx
    on shared_prep_materials (school_id, grade, subject, book_id, chapter_number);
