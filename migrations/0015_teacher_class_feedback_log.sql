-- Tracks whether My Agent has already asked about a specific period's
-- lesson after it ended — same "presence of the row IS the signal" shape as
-- teacher_period_sweep_log (migration 0013), kept as its own table rather
-- than widening that one (its unique constraint is already live; blind
-- widening means guessing Postgres's auto-generated constraint name).
create table if not exists teacher_class_feedback_log (
    id uuid primary key default gen_random_uuid(),
    teacher_id uuid not null references teachers(id) on delete cascade,
    class_id uuid references classes(id) on delete set null,
    period_number int not null,
    run_date date not null,
    asked_at timestamptz not null default now(),

    unique (teacher_id, period_number, run_date)
);

create index if not exists teacher_class_feedback_log_lookup_idx
    on teacher_class_feedback_log (teacher_id, run_date);
