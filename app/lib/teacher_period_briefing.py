"""The finest-grained layer of My Agent's autonomy — a short briefing right
before each class, not just twice a day. Reuses almost everything already
built: timetable_resolution.resolve_day_timetable for "what period is this,
what class, what time", teacher_sweep.py's playbooks (unchanged — they
already read ctx["classIds"], so scoping one down to a single period's class
is just passing a narrowed ctx), and argus_sweep.py's phrasing helpers.

Triggered the same "activity, not a timer" way as agent_scheduler.py, just
polled more often (see app/(dashboard)/layout.tsx's ~2 minute interval) —
this backend still has no real background scheduler, so a period's briefing
only fires if a teacher has the app open on some device during its lead
window. That window is short (LEAD_MINUTES before start, through the
period's end), which is why polling needs to be frequent enough to land
inside it.
"""
from datetime import datetime, timedelta, timezone

from .supabase_clients import create_admin_client
from .timetable_resolution import resolve_day_timetable
from .teacher_sweep import detect
from .argus_sweep import _phrase_briefing
from .argus_memory import record_findings, should_report, mark_reported
from .group_chat_store import save_message
from .prep_material_agent import find_existing_prep_material
from .subject_aliases import subject_matches

TABLE = "teacher_period_sweep_log"
LEAD_MINUTES = 10

# leave_status deliberately excluded — it's about the teacher's own leave
# requests, not this period's class, and would be noise in a "right before
# class" briefing. The other four are exactly what's worth knowing walking
# into a specific class.
PERIOD_PLAYBOOKS = ["pending_doubts", "pending_marks", "attendance_concern", "syllabus_pace"]


def _ist_now() -> datetime:
    return datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _minus_minutes(hhmm: str, minutes: int) -> str:
    t = datetime.strptime(hhmm, "%H:%M")
    return (t - timedelta(minutes=minutes)).strftime("%H:%M")


def _is_due(entry: dict, now_hm: str) -> bool:
    start, end = entry.get("startTime"), entry.get("endTime")
    if not start or not end:
        return False
    return _minus_minutes(start, LEAD_MINUTES) <= now_hm <= end


def resolve_period_topic(class_id: str, subject: str, date_str: str, period_number: int, ac) -> dict:
    """What's actually being taught in one specific period — the pinned
    session topic if one was set when the period was opened
    (prep_session_topics, migration 0004), else the most recently created
    prep_materials row for this class+subject (whatever was last
    generated/used is the most likely thing actually taught). Shared by the
    "show what's ready" step below and teacher_class_feedback.py's
    "what should I ask about" step, so both agree on the same answer.

    Matched via subject_matches(), not .eq("subject", ...) — confirmed
    against real data that a timetable period's label ("environmental
    science") and a saved prep_materials row's subject ("Environmental
    Studies") are genuinely different strings for the same real subject, the
    exact mismatch subject_aliases.py exists to handle. An exact match here
    would silently never find real, already-saved material."""
    try:
        candidates = (
            ac.table("prep_session_topics").select("topic, subtopic, subject")
            .eq("class_id", class_id).eq("session_date", date_str).eq("period_number", period_number)
            .execute().data or []
        )
    except Exception:
        candidates = []
    row = next((r for r in candidates if subject_matches(r.get("subject"), subject) and r.get("topic")), None)
    if row:
        return {"topic": row["topic"], "subtopic": row.get("subtopic")}

    try:
        rows = (
            ac.table("prep_materials").select("topic, subtopic, subject")
            .eq("class_id", class_id).order("created_at", desc=True).limit(20).execute().data or []
        )
    except Exception:
        rows = []
    match = next((r for r in rows if subject_matches(r.get("subject"), subject)), None)
    return {"topic": match["topic"], "subtopic": match.get("subtopic")} if match else None


def _material_readiness_line(teacher_id: str, entry: dict, subject: str, topic_info: dict, ac) -> str:
    """Zero generation cost — only reads what's already there. A short
    summary (objective, not the full lesson JSON) so this fits in a chat
    bubble; "Open Prep Materials" is where the real sheet lives."""
    existing = find_existing_prep_material(
        teacher_id, entry["classId"], subject, topic_info["topic"],
        subtopic=topic_info.get("subtopic"), ac=ac,
    )
    lesson = existing.get("lesson")
    if not lesson:
        return f'No prep material ready yet for "{topic_info["topic"]}" — ask me to generate one if you want it before class.'
    objective = (lesson or {}).get("objective")
    return (
        f'Prep material for "{topic_info["topic"]}" is ready{f" — {objective}" if objective else ""}. '
        "Open Prep Materials to see the full sheet."
    )


def _already_ran(teacher_id: str, period_number: int, run_date: str, ac) -> bool:
    try:
        rows = (
            ac.table(TABLE).select("id")
            .eq("teacher_id", teacher_id).eq("period_number", period_number).eq("run_date", run_date)
            .limit(1).execute().data or []
        )
        return bool(rows)
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_period_briefing] _already_ran check failed: {e}")
        return False


def _record_ran(teacher_id: str, class_id: str, period_number: int, run_date: str, ac) -> None:
    try:
        ac.table(TABLE).insert({
            "teacher_id": teacher_id, "class_id": class_id,
            "period_number": period_number, "run_date": run_date,
        }).execute()
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_period_briefing] _record_ran failed: {e}")


async def _brief_one(teacher_id: str, ctx: dict, entry: dict, run_date: str, ac) -> None:
    period_number = entry["periodNumber"]
    if _already_ran(teacher_id, period_number, run_date, ac):
        return

    narrow_ctx = {**ctx, "classIds": [entry["classId"]]}
    detected = detect(teacher_id, narrow_ctx, PERIOD_PLAYBOOKS, run_date)
    diff = record_findings(ctx["schoolId"], detected, key_prefix=f"teacher:{teacher_id}:")

    # Recorded regardless of whether anything's reportable — same order
    # agent_scheduler.py's _run_one already uses, so a quiet period doesn't
    # get re-checked on every subsequent poll that same window.
    _record_ran(teacher_id, entry["classId"], period_number, run_date, ac)

    reportable = [f for f in diff["new"] + diff["recurring"] if should_report(f)]
    prefix = f"Before {entry.get('className') or 'class'} (period {period_number}): "

    if reportable or diff["resolved"]:
        briefing = await _phrase_briefing(reportable, diff["resolved"])
        mark_reported(ctx["schoolId"], [f["findingKey"] for f in reportable])
        save_message(
            ctx["schoolId"], sender="teacher_agent", content=prefix + briefing,
            thread="teacher", teacher_id=teacher_id,
        )

    # Independent of whether the sweep above had anything to say — this is
    # "what's ready for THIS class right now", not a finding with a cooldown.
    # Zero generation cost: find_existing_prep_material only reads.
    subject = entry.get("label") or ""
    topic_info = resolve_period_topic(entry["classId"], subject, run_date, period_number, ac) if subject else None
    if topic_info:
        readiness = _material_readiness_line(teacher_id, entry, subject, topic_info, ac)
        save_message(
            ctx["schoolId"], sender="teacher_agent", content=prefix + readiness,
            thread="teacher", teacher_id=teacher_id,
        )
    for finding in reportable:
        action = finding.get("suggestedAction")
        if not action:
            continue
        save_message(
            ctx["schoolId"], sender="teacher_agent", content=action["label"], thread="teacher", teacher_id=teacher_id,
            pending_confirmation={"tool": action["tool"], "args": action["args"], "label": action["label"]},
        )


async def maybe_run_period_briefings(teacher_id: str, ctx: dict) -> None:
    if not ctx.get("schoolId"):
        return
    ac = create_admin_client()
    now = _ist_now()
    today = now.date().isoformat()
    now_hm = now.strftime("%H:%M")

    try:
        schedule = resolve_day_timetable(teacher_id, today, ac)
    except Exception as e:
        print(f"[teacher_period_briefing] resolve_day_timetable failed: {e}")
        return
    if schedule.get("onLeave"):
        return

    for entry in schedule.get("entries", []):
        if entry.get("coverage") == "covered_away":
            continue
        if entry.get("coverage") == "covering" and not entry.get("fullAccess"):
            continue
        if not _is_due(entry, now_hm):
            continue
        try:
            await _brief_one(teacher_id, ctx, entry, today, ac)
        except Exception as e:
            print(f"[teacher_period_briefing] briefing failed for period {entry.get('periodNumber')}: {e}")
