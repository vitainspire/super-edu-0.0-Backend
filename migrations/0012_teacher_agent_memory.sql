-- My Agent's persistent memory: short notes it saves when a teacher tells it
-- something worth remembering across conversations — a preference, a
-- recurring concern, context about their class. Read back into every future
-- run_teacher_agent prompt (app/lib/teacher_agent.py). Archived, not deleted,
-- on forget — same "keep the trail" choice argus_findings' dismissed status
-- already makes (migration 0008), so a wrongly-forgotten note isn't gone
-- without a trace.

create table if not exists teacher_agent_memory (
    id uuid primary key default gen_random_uuid(),
    teacher_id uuid not null references teachers(id) on delete cascade,
    school_id uuid references schools(id) on delete cascade,
    content text not null,
    created_at timestamptz not null default now(),
    archived_at timestamptz
);

create index if not exists teacher_agent_memory_active_idx
    on teacher_agent_memory (teacher_id, created_at desc)
    where archived_at is null;
