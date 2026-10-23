-- Tracks whether a teacher's pre-period briefing has already fired for a
-- given (teacher, period, day) — same "presence of the row IS the signal"
-- idea as agent_sweep_log (migration 0011), but a dedicated table rather
-- than widening that one: its `window` column is CHECK-constrained to
-- 'morning'/'evening' and already carries a live unique constraint from the
-- applied 0011, and safely widening either means guessing Postgres's
-- auto-generated constraint name. A period fires far more often (every
-- class, every day) than the twice-daily sweep, so it also gets its own
-- index on (teacher_id, run_date) rather than reusing agent_sweep_log's.
create table if not exists teacher_period_sweep_log (
    id uuid primary key default gen_random_uuid(),
    teacher_id uuid not null references teachers(id) on delete cascade,
    class_id uuid references classes(id) on delete set null,
    period_number int not null,
    run_date date not null,
    ran_at timestamptz not null default now(),

    unique (teacher_id, period_number, run_date)
);

create index if not exists teacher_period_sweep_log_lookup_idx
    on teacher_period_sweep_log (teacher_id, run_date);
