-- Two tables that give the Headmaster/Admin Agent real memory and state,
-- mirroring what My Agent already has (migrations 0011/0012):
--
--   agent_notes       persistent notes across conversations, the Headmaster/
--                     Admin equivalent of teacher_agent_memory (0012) — see
--                     app/lib/agent_notes.py
--   agent_action_log  a durable record of every "auto" tool the agent has
--                     actually executed on its own (live chat or a proactive
--                     sweep), not just proposed — see app/lib/agent_action_log.py

create table if not exists agent_notes (
    id uuid primary key default gen_random_uuid(),
    school_id uuid not null references schools(id) on delete cascade,
    persona text not null check (persona in ('headmaster', 'admin')),
    content text not null,
    created_at timestamptz not null default now(),
    archived_at timestamptz
);

create index if not exists agent_notes_active_idx
    on agent_notes (school_id, persona, created_at desc)
    where archived_at is null;

create table if not exists agent_action_log (
    id uuid primary key default gen_random_uuid(),
    school_id uuid not null references schools(id) on delete cascade,
    persona text not null check (persona in ('headmaster', 'admin')),
    tool text not null,
    args jsonb,
    label text,
    status text not null check (status in ('succeeded', 'failed')),
    source text not null check (source in ('chat', 'sweep')),
    outcome jsonb,
    error text,
    created_at timestamptz not null default now()
);

create index if not exists agent_action_log_recent_idx
    on agent_action_log (school_id, persona, created_at desc);
