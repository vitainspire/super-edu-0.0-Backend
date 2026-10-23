-- Group Chat + Individual Messages (admin side) + the Teacher Agent's own
-- thread: all live in this one table, told apart by `thread`. 'group' is the
-- shared room where an admin talks with the Headmaster Agent and the Admin
-- Agent together (see app/lib/argus_agent.py's PERSONAS and
-- app/lib/group_chat_store.py). 'headmaster'/'admin' are 1:1 DM threads with
-- just that persona — used both for a deliberate direct question (no
-- routing classifier involved) and for that persona's autonomous,
-- twice-daily messages (see app/lib/agent_scheduler.py). 'teacher' is a
-- DIFFERENT kind of 1:1 thread: a teacher talking with their own Teacher
-- Agent (app/lib/teacher_agent.py) — scoped additionally by `teacher_id`,
-- since (unlike the admin-side threads, where `school_id` alone is enough
-- scope) multiple teachers at the same school each need a fully separate,
-- private thread.
--
-- Unlike argus_findings (migration 0008), which is keyed by problem and
-- upserted, this is a plain append-only message log — one row per message,
-- human or agent, in send order. `trace` captures the tool calls an agent
-- message's answer was grounded in (same shape as run_agent's returned
-- trace list), so the UI can render "🔧 get_syllabus_status -> ..." inline
-- instead of hiding the reasoning behind a single final answer.

create table if not exists group_chat_messages (
    id uuid primary key default gen_random_uuid(),
    school_id uuid not null references schools(id) on delete cascade,
    -- Set only for thread='teacher' — null for every admin-side thread.
    teacher_id uuid references teachers(id) on delete cascade,

    thread text not null default 'group' check (thread in ('group', 'headmaster', 'admin', 'teacher')),

    sender text not null check (sender in ('admin', 'headmaster_agent', 'admin_agent', 'teacher', 'teacher_agent')),
    -- Which human admin sent it; null for agent messages and for teacher-side messages.
    -- admins.id is text in this schema, not uuid (confirmed against the real
    -- table — a uuid FK here fails with "incompatible types: uuid and text").
    admin_id text references admins(id) on delete set null,

    content text not null,

    -- The tool-call steps behind an agent message's answer:
    -- [{"tool": "...", "args": {...}, "ok": true}, ...]. Empty for a plain
    -- admin message.
    trace jsonb not null default '[]'::jsonb,

    -- Set when this message ended the agent's run on a proposed action
    -- rather than a final answer: {"tool": "...", "args": {...}, "label": "..."}.
    -- Mirrors argus_findings.suggested_action — storing the proposal, not
    -- executing it, keeps POST /group-chat/confirm the only write path.
    pending_confirmation jsonb,
    confirmed_at timestamptz,

    created_at timestamptz not null default now()
);

create index if not exists group_chat_messages_school_thread_created_idx
    on group_chat_messages (school_id, thread, teacher_id, created_at);
