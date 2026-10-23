-- Visible record of agent-to-agent communication — Headmaster Agent <-> Admin
-- Agent (message_admin_agent / message_headmaster_agent) and Headmaster
-- Agent -> a specific teacher's own agent (invoke_teacher_agent, already
-- live, previously only visible as a trace line inside whatever chat
-- message triggered it). Not something a human posts into — a read-only log
-- so an admin can see what the agents are coordinating about among
-- themselves, separate from Group Chat / Individual Messages (which are
-- about talking to the admin).
create table if not exists agent_activity_log (
    id uuid primary key default gen_random_uuid(),
    school_id uuid not null references schools(id) on delete cascade,

    from_persona text not null check (from_persona in ('headmaster_agent', 'admin_agent')),
    -- Display label for the recipient — "Admin Agent" / "Headmaster Agent" /
    -- "<teacher name>'s Agent". Kept as plain text rather than a second enum
    -- since a teacher's own agent isn't one of a fixed small set.
    to_label text not null,
    -- Set only when the recipient is a teacher's own agent.
    to_teacher_id uuid references teachers(id) on delete set null,

    question text not null,
    answer text,
    -- Mirrors run_agent's own status strings: answered / pending_confirmation / error / step_limit.
    status text not null,

    created_at timestamptz not null default now()
);

create index if not exists agent_activity_log_school_created_idx
    on agent_activity_log (school_id, created_at desc);
