-- Phase C: the Pedagogy Library. Curated teaching knowledge, independent of
-- any textbook — "World 2" in the ingestion/pedagogy split. Meets Phase B's
-- curriculum knowledge only at generation time, via the shared `competencies`
-- and `contexts` tables (both already exist — see migration 020).
--
-- Deliberately no separate "activity_categories" table: category is a plain
-- text label on each template (e.g. "Count", "Observe" for grade_band 1-3;
-- "Investigate", "Analyze" for 4-5), not an entity with its own metadata worth
-- a join. The three-tier model from the design doc — Category -> Template ->
-- Context — is still fully expressed: Category is a field, Template is the
-- row, Context is the many-to-many link onto the existing global `contexts`
-- table (so "Fruit" discovered during ingestion and "Fruit" authored into an
-- activity template are the same row).

create table if not exists activity_templates (
    id uuid primary key default gen_random_uuid(),
    grade_band text not null check (grade_band in ('1-3', '4-5')),
    category text not null,
    name text not null,
    description text,
    resource_level smallint not null default 0 check (resource_level in (0, 1, 2)),
    duration_min smallint,
    duration_max smallint,
    grouping text check (grouping in ('individual', 'pair', 'group')),
    classroom_type text check (classroom_type in ('indoor', 'outdoor', 'both')),
    bloom_level text check (bloom_level in
        ('remember', 'understand', 'apply', 'analyze', 'evaluate', 'create')),
    fln_compatible boolean not null default true,
    assessment_method text,
    created_at timestamptz not null default now(),
    unique (grade_band, category, name)
);
create index if not exists activity_templates_grade_category_idx
    on activity_templates(grade_band, category);

create table if not exists activity_template_competencies (
    id uuid primary key default gen_random_uuid(),
    activity_template_id uuid not null references activity_templates(id) on delete cascade,
    competency_id uuid not null references competencies(id) on delete cascade,
    created_at timestamptz not null default now(),
    unique (activity_template_id, competency_id)
);
create index if not exists atc_template_idx on activity_template_competencies(activity_template_id);
create index if not exists atc_competency_idx on activity_template_competencies(competency_id);

create table if not exists activity_template_contexts (
    id uuid primary key default gen_random_uuid(),
    activity_template_id uuid not null references activity_templates(id) on delete cascade,
    context_id uuid not null references contexts(id) on delete cascade,
    created_at timestamptz not null default now(),
    unique (activity_template_id, context_id)
);
create index if not exists atx_template_idx on activity_template_contexts(activity_template_id);
create index if not exists atx_context_idx on activity_template_contexts(context_id);
