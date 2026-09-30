-- The generated-chapter cache: one chapter's finished prep material, computed
-- once and served to every school that teaches it.
--
-- WHY THIS IS SOUND, AND NOT A SHORTCUT. The agentic pipeline
-- (app/lib/prep_pipeline_bridge.py) takes grade, subject, chapter title,
-- chapter markdown, page range and figures -- and NOTHING about a school or a
-- class. It cannot: shared/opt_in material is pooled across every section
-- teaching a grade+subject, so context_flow (the per-classroom adaptation
-- stage) is deliberately never called and `context_plan` is always None.
--
-- So the whole output is a pure function of (textbook chapter, grade, subject).
-- Fifty schools teaching the same state textbook were each paying ~80-100
-- model calls to derive the identical answer. This table is the same bargain
-- the textbook catalog already makes (migration 0009): ingest once, read many.
--
-- The per-school work is what happens AFTER this cache is read -- matching
-- lessons onto that school's syllabus_topics and writing shared_prep_materials
-- rows (prep_batch_jobs.save_published_chapter_lessons). That part is pure
-- database work and is not cached, because it genuinely differs per school.
--
-- CACHE_KEY CARRIES A VERSION, deliberately. Caching freezes quality as well
-- as cost: improve the prompts and every cached chapter keeps serving the old
-- output until something invalidates it. Bumping PIPELINE_VERSION in
-- prep_pipeline_bridge.py changes every key and retires the old rows, which is
-- the same reason prep_flow's reasoning cache carries `v4` in its own key.

create table if not exists prep_chapter_cache (
    cache_key       text primary key,
    book_id         text not null,
    chapter_number  integer not null,
    grade           text,
    subject         text,
    chapter_title   text,
    -- The full result dict prep_pipeline_bridge.generate_lessons_from_published_book
    -- returns: lessons[] plus the shipped/needsReview/refused/total counts.
    payload         jsonb not null,
    shipped         integer,
    needs_review    integer,
    total           integer,
    created_at      timestamptz not null default now()
);

-- "Has this book+chapter been generated, under any pipeline version?" — the
-- question an admin screen asks, and the one a cleanup of retired versions
-- asks. The primary key already covers exact-key lookup, which is the hot path.
create index if not exists prep_chapter_cache_book_chapter_idx
    on prep_chapter_cache (book_id, chapter_number);
