-- Tracks whether a persona's morning/evening proactive sweep has already run
-- today, so app/lib/agent_scheduler.py's "triggered by activity, not a
-- timer" check (run on page load if due and not yet run) only actually
-- executes a sweep once per window per day, regardless of how many times the
-- Agents page is opened in that window. One row per (school, persona,
-- window, day) it actually ran — presence of the row IS the "already ran"
-- signal, nothing to update after insert.

create table if not exists agent_sweep_log (
    id uuid primary key default gen_random_uuid(),
    school_id uuid not null references schools(id) on delete cascade,
    -- null for 'admin'/'headmaster' (school-wide sweeps); set for 'teacher'
    -- (one autonomous sweep per teacher per day, see maybe_run_teacher_daily_sweep).
    teacher_id uuid references teachers(id) on delete cascade,
    persona text not null check (persona in ('headmaster', 'admin', 'teacher')),
    "window" text not null check ("window" in ('morning', 'evening')),
    run_date date not null,
    ran_at timestamptz not null default now(),

    unique (school_id, persona, "window", run_date, teacher_id)
);

create index if not exists agent_sweep_log_school_idx
    on agent_sweep_log (school_id, run_date);
