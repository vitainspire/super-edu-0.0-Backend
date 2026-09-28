-- A local mirror of the published-textbook service
-- (https://eduteach-textbook-api.onrender.com), so prep material can be
-- grounded in the actual words of the child's book instead of the model's
-- general sense of what a book that grade says.
--
-- Why mirror instead of calling the API at generation time:
--   1. That service runs on Render's free tier and sleeps after ~15 minutes
--      idle. A cold start is 30-60s. A teacher tapping "prep material" and
--      waiting a minute reads as broken, and it would happen every morning
--      — exactly when teachers actually use it.
--   2. New books keep being published, so the catalog has to be refreshed
--      on a schedule anyway. Once you're syncing, you may as well read
--      locally.
--
-- Split into catalog (cheap, refreshed often) and content (bulk, fetched
-- lazily per chapter on first use). Syncing 70+ chapters of ~11k words
-- up front to serve the two a school actually teaches this week would be
-- most of a megabyte of wasted transfer per refresh.

create table if not exists textbook_catalog_books (
    -- The service's own deterministic id, e.g.
    -- "ts_scert_class5_environmental_studies_en". Natural key: it's stable,
    -- and it's what every API path is addressed by.
    book_id text primary key,
    board text,
    grade text,
    subject text,
    language text,
    total_chapters int,
    chapters_published int,
    synced_at timestamptz not null default now()
);

create index if not exists textbook_catalog_books_grade_subject_idx
    on textbook_catalog_books (grade, subject);

create table if not exists textbook_catalog_chapters (
    id uuid primary key default gen_random_uuid(),
    book_id text not null references textbook_catalog_books(book_id) on delete cascade,
    chapter_number int not null,
    chapter_title text,
    -- The textbook's OWN printed page numbers. These do not necessarily
    -- match syllabus_chapters.page_start for the same chapter — that came
    -- from a PDF whose pagination is offset — so the two are reconciled at
    -- read time, never assumed equal.
    page_start int,
    page_end int,

    -- Bulk fields, populated lazily the first time a chapter is actually
    -- needed. Null content means "not fetched yet", not "empty chapter".
    content text,
    -- Image metadata only (image_id, caption, usage, order_index). The
    -- service's image URLs are SIGNED and expire, so persisting them would
    -- mean saved prep material showing broken pictures the next day.
    -- Grounding only ever needs the caption and the page anyway ("show the
    -- picture on page 27"), so the URLs are deliberately dropped.
    images jsonb,
    content_synced_at timestamptz,

    synced_at timestamptz not null default now(),
    unique (book_id, chapter_number)
);

create index if not exists textbook_catalog_chapters_book_idx
    on textbook_catalog_chapters (book_id, chapter_number);
