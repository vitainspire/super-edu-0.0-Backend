-- Argus's persistent memory. The agent loop in app/lib/argus_agent.py has
-- only per-run working memory (a scratchpad discarded when the run ends),
-- which is fine for answering a question but not for *running* anything: a
-- proactive sweep that re-reports the same six unstaffed classes every
-- morning is noise an admin learns to ignore, and noise an admin ignores is
-- indistinguishable from no alert at all.
--
-- So each finding gets a stable, content-derived `finding_key` (e.g.
-- "coverage:<substitutionId>" or "readiness:no_teacher:<classId>"), and this
-- table records what has already been surfaced. A sweep re-detecting the
-- same key is silent by default; a key that stops appearing is resolved and
-- gets closed, which is also what makes "what did Argus fix this week?"
-- answerable at all.
--
-- Deliberately one row per (school, finding_key) rather than one per sweep
-- run: the question this table answers is "is this still open, and have we
-- already said so", not "what did run #47 see". Run history lives in the
-- automation_events audit trail (migration 0005), which already exists for
-- exactly that purpose.

create table if not exists argus_findings (
    id uuid primary key default gen_random_uuid(),
    school_id uuid not null references schools(id) on delete cascade,

    -- Stable identity for a real-world problem, not for a sighting of it.
    -- Recomputed from the same inputs on every sweep, so the same gap always
    -- lands on the same row.
    finding_key text not null,
    playbook text not null,
    severity text not null default 'info' check (severity in ('info', 'attention', 'urgent')),

    title text not null,
    detail text,

    -- The confirm-gated action this finding could be resolved by, if any:
    -- {"tool": "assign_substitute", "args": {...}}. Null for findings that
    -- are informational only (e.g. "no attendance recorded"), or whose fix
    -- needs information the system doesn't hold (e.g. which students belong
    -- in an empty class). Storing the proposal, not executing it, is the
    -- whole point — POST /argus/confirm remains the only write path.
    suggested_action jsonb,

    status text not null default 'open' check (status in ('open', 'resolved', 'dismissed')),

    first_seen_at timestamptz not null default now(),
    last_seen_at timestamptz not null default now(),
    -- Set when a sweep no longer detects the key, so "resolved" means
    -- "actually gone", not "someone clicked dismiss".
    resolved_at timestamptz,
    -- Set when the admin chooses to stop hearing about a still-present
    -- finding, which must not be confused with it being fixed.
    dismissed_at timestamptz,
    -- Last time this was actually shown/reported, so a long-open finding can
    -- be re-surfaced on a cadence instead of only once, ever.
    last_reported_at timestamptz,

    created_at timestamptz not null default now(),
    updated_at timestamptz not null default now(),

    unique (school_id, finding_key)
);

create index if not exists argus_findings_school_status_idx
    on argus_findings (school_id, status, severity);

create index if not exists argus_findings_school_playbook_idx
    on argus_findings (school_id, playbook, status);
