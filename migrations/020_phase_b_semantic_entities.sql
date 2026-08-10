-- Phase B: concepts / competencies / vocabulary / contexts as canonical,
-- cross-book entities, plus per-topic learning outcomes and two scalar
-- columns (bloom_level, difficulty) directly on syllabus_topics.
--
-- Design: concepts/competencies/vocabulary/contexts are GLOBAL (no school_id) —
-- shared curriculum taxonomy, not per-tenant data. The library starts empty and
-- grows organically as textbooks are ingested (see app/lib/canonical_mapping.py):
-- the first extraction of "Number Recognition" creates the row; a later
-- extraction of "Recognize Numerals" matches onto it via LLM similarity and gets
-- recorded as an alias instead of creating a duplicate.
--
-- Topics are fanned out one row per class section (see syllabus_persist.py's
-- docstring), all sharing `definition_id`. These junction tables key on
-- definition_id, not the per-section row id, so a topic's concepts/competencies
-- are stored once regardless of how many sections that grade+subject has.

create table if not exists concepts (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    description text,
    aliases text[] not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists competencies (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    description text,
    aliases text[] not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists vocabulary (
    id uuid primary key default gen_random_uuid(),
    term text not null unique,
    description text,
    aliases text[] not null default '{}',
    created_at timestamptz not null default now()
);

-- Shared with the Phase C Pedagogy Library's curated Context Library (Home /
-- School / Market / Nature / Community / Festivals / Sports) — a context
-- discovered during ingestion (e.g. "Fruit" in a word problem) canonically
-- maps onto the same table Phase C authors activity contexts into.
create table if not exists contexts (
    id uuid primary key default gen_random_uuid(),
    name text not null unique,
    category text,
    aliases text[] not null default '{}',
    created_at timestamptz not null default now()
);

create table if not exists topic_concepts (
    id uuid primary key default gen_random_uuid(),
    topic_definition_id uuid not null,
    concept_id uuid not null references concepts(id) on delete cascade,
    created_at timestamptz not null default now(),
    unique (topic_definition_id, concept_id)
);
create index if not exists topic_concepts_topic_idx on topic_concepts(topic_definition_id);

create table if not exists topic_competencies (
    id uuid primary key default gen_random_uuid(),
    topic_definition_id uuid not null,
    competency_id uuid not null references competencies(id) on delete cascade,
    created_at timestamptz not null default now(),
    unique (topic_definition_id, competency_id)
);
create index if not exists topic_competencies_topic_idx on topic_competencies(topic_definition_id);

create table if not exists topic_vocabulary (
    id uuid primary key default gen_random_uuid(),
    topic_definition_id uuid not null,
    vocabulary_id uuid not null references vocabulary(id) on delete cascade,
    created_at timestamptz not null default now(),
    unique (topic_definition_id, vocabulary_id)
);
create index if not exists topic_vocabulary_topic_idx on topic_vocabulary(topic_definition_id);

create table if not exists topic_contexts (
    id uuid primary key default gen_random_uuid(),
    topic_definition_id uuid not null,
    context_id uuid not null references contexts(id) on delete cascade,
    created_at timestamptz not null default now(),
    unique (topic_definition_id, context_id)
);
create index if not exists topic_contexts_topic_idx on topic_contexts(topic_definition_id);

-- NOT canonicalized — a learning outcome is a specific statement about one
-- topic ("Add quantities up to 10"), not a reusable taxonomy entry the way a
-- concept or competency is.
create table if not exists topic_learning_outcomes (
    id uuid primary key default gen_random_uuid(),
    topic_definition_id uuid not null,
    text text not null,
    created_at timestamptz not null default now()
);
create index if not exists topic_learning_outcomes_topic_idx on topic_learning_outcomes(topic_definition_id);

alter table syllabus_topics add column if not exists bloom_level text;
alter table syllabus_topics add column if not exists difficulty text;

alter table syllabus_topics
    add constraint syllabus_topics_bloom_level_check
    check (bloom_level is null or bloom_level in
        ('remember', 'understand', 'apply', 'analyze', 'evaluate', 'create'));

alter table syllabus_topics
    add constraint syllabus_topics_difficulty_check
    check (difficulty is null or difficulty in ('easy', 'medium', 'hard'));
