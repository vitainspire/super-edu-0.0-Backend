"""Makes the Headmaster Agent and Admin Agent autonomous — able to message
the admin first, twice a day, without being asked.

This backend has no scheduler of any kind (no cron, no background task
runner). Rather than add one, this is deliberately "triggered by activity,
not a timer": whenever an admin opens the Agents page (see
app/routes/group_chat_routes.py's GET handlers), we check whether today's
morning/evening sweep for each persona is already done, and run it right
then if it's due and hasn't run. If literally nobody opens the app in a
given window, that window's sweep just doesn't happen — an accepted
trade-off for not needing a persistent background process.

Detection itself reuses app/lib/argus_sweep.py's existing, already-working
playbooks entirely — this module's only job is turning a sweep's result into
real chat messages on the right persona's individual thread, and remembering
that it already did so today.
"""
from datetime import datetime, timezone

from .supabase_clients import create_admin_client
from .argus_sweep import run_sweep
from .teacher_sweep import run_teacher_sweep
from .group_chat_store import save_message

TABLE = "agent_sweep_log"

# ~7:30am / ~4:30pm IST, this app's schools being TS-SCERT/Indian-curriculum
# per the textbook ingestion code elsewhere in this backend — adjust these if
# that assumption is wrong for a given deployment.
MORNING_TRIGGER_HOUR_UTC = 2
EVENING_TRIGGER_HOUR_UTC = 11

# Which playbooks run for which persona's sweep — the 4 existing playbooks
# are all operations-flavored (Admin Agent's domain); syllabus_pace (added
# alongside this module) is the one academic playbook, Headmaster Agent's.
PLAYBOOKS_BY_PERSONA = {
    "admin": ["coverage", "readiness", "approvals", "operations"],
    "headmaster": ["syllabus_pace"],
}


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _already_ran(school_id: str, persona: str, window: str, run_date: str, ac, teacher_id: str = None) -> bool:
    try:
        query = (
            ac.table(TABLE).select("id")
            .eq("school_id", school_id).eq("persona", persona)
            .eq("window", window).eq("run_date", run_date)
        )
        if teacher_id:
            query = query.eq("teacher_id", teacher_id)
        rows = query.limit(1).execute().data or []
        return bool(rows)
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_scheduler] _already_ran check failed: {e}")
        # Table missing means we can't remember what ran — treat as "not yet
        # run" so the sweep still fires rather than silently never running,
        # same "a missing memory table shouldn't take the feature down"
        # rule argus_memory.py uses, just inverted for a boolean guard.
        return False


def _record_ran(school_id: str, persona: str, window: str, run_date: str, ac, teacher_id: str = None) -> None:
    try:
        ac.table(TABLE).insert({
            "school_id": school_id, "persona": persona, "window": window, "run_date": run_date,
            "teacher_id": teacher_id,
        }).execute()
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_scheduler] _record_ran failed: {e}")


async def _run_one(school_id: str, persona: str, window: str, run_date: str, ac) -> None:
    if _already_ran(school_id, persona, window, run_date, ac):
        return

    result = await run_sweep(school_id, playbooks=PLAYBOOKS_BY_PERSONA[persona])
    _record_ran(school_id, persona, window, run_date, ac)

    # Nothing to say — don't post "nothing needs your attention" twice a day
    # forever, that's noise, not an autonomous assistant.
    if not result["reported"] and not result["resolved"]:
        return

    sender = f"{persona}_agent"
    save_message(school_id, sender=sender, content=result["briefing"], thread=persona)
    for finding in result["actionable"]:
        action = finding["suggestedAction"]
        save_message(
            school_id, sender=sender, content=action["label"], thread=persona,
            pending_confirmation={"tool": action["tool"], "args": action["args"], "label": action["label"]},
        )


async def maybe_run_daily_sweeps(school_id: str) -> None:
    ac = create_admin_client()
    now = datetime.now(timezone.utc)
    run_date = now.date().isoformat()

    due_windows = []
    if now.hour >= MORNING_TRIGGER_HOUR_UTC:
        due_windows.append("morning")
    if now.hour >= EVENING_TRIGGER_HOUR_UTC:
        due_windows.append("evening")
    if not due_windows:
        return

    for persona in PLAYBOOKS_BY_PERSONA:
        for window in due_windows:
            try:
                await _run_one(school_id, persona, window, run_date, ac)
            except Exception as e:
                print(f"[agent_scheduler] sweep failed for {persona}/{window}: {e}")


async def _run_one_teacher(teacher_id: str, ctx: dict, window: str, run_date: str, ac) -> None:
    if _already_ran(ctx["schoolId"], "teacher", window, run_date, ac, teacher_id=teacher_id):
        return

    result = await run_teacher_sweep(teacher_id, ctx)
    _record_ran(ctx["schoolId"], "teacher", window, run_date, ac, teacher_id=teacher_id)

    if not result["reported"] and not result["resolved"]:
        return

    save_message(ctx["schoolId"], sender="teacher_agent", content=result["briefing"], thread="teacher", teacher_id=teacher_id)
    for finding in result["actionable"]:
        action = finding["suggestedAction"]
        save_message(
            ctx["schoolId"], sender="teacher_agent", content=action["label"], thread="teacher", teacher_id=teacher_id,
            pending_confirmation={"tool": action["tool"], "args": action["args"], "label": action["label"]},
        )


async def maybe_run_teacher_daily_sweep(teacher_id: str, ctx: dict) -> None:
    """Same "triggered by activity, not a timer" shape as
    maybe_run_daily_sweeps, scoped to one teacher: whenever they open My
    Agent, check whether today's morning/evening sweep for THEM already ran,
    and run it if due. No-op if they're not attached to a school yet."""
    if not ctx.get("schoolId"):
        return
    ac = create_admin_client()
    now = datetime.now(timezone.utc)
    run_date = now.date().isoformat()

    due_windows = []
    if now.hour >= MORNING_TRIGGER_HOUR_UTC:
        due_windows.append("morning")
    if now.hour >= EVENING_TRIGGER_HOUR_UTC:
        due_windows.append("evening")
    if not due_windows:
        return

    for window in due_windows:
        try:
            await _run_one_teacher(teacher_id, ctx, window, run_date, ac)
        except Exception as e:
            print(f"[agent_scheduler] teacher sweep failed for {teacher_id}/{window}: {e}")
