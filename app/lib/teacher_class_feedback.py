"""The "after class" half of My Agent's class-level autonomy — asks how a
lesson went once a period ends, so the teacher's reply (via
teacher_agent.py's save_class_feedback tool) feeds the existing, already-live
adaptation pipeline (classes.feedback_profile / lesson_feedback.insight,
read back by every future generation via prep_context.fetch_feedback_context).

Same "activity, not a timer" trigger shape as teacher_period_briefing.py,
just keyed off a period's END rather than its start, and reusing that
module's resolve_period_topic so both agree on what's actually being taught
in a given period.
"""
from datetime import datetime, timedelta, timezone

from .supabase_clients import create_admin_client
from .timetable_resolution import resolve_day_timetable
from .teacher_period_briefing import resolve_period_topic, _ist_now
from .group_chat_store import save_message

TABLE = "teacher_class_feedback_log"
# How long after a period ends this is still worth asking about — wide
# enough that a poll every ~2 minutes (app/(dashboard)/layout.tsx) reliably
# lands inside it.
ASK_WINDOW_MINUTES = 15


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _is_due(entry: dict, now_hm: str) -> bool:
    end = entry.get("endTime")
    if not end:
        return False
    end_dt = datetime.strptime(end, "%H:%M")
    window_end = (end_dt + timedelta(minutes=ASK_WINDOW_MINUTES)).strftime("%H:%M")
    return end <= now_hm <= window_end


def _already_asked(teacher_id: str, period_number: int, run_date: str, ac) -> bool:
    try:
        rows = (
            ac.table(TABLE).select("id")
            .eq("teacher_id", teacher_id).eq("period_number", period_number).eq("run_date", run_date)
            .limit(1).execute().data or []
        )
        return bool(rows)
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_class_feedback] _already_asked check failed: {e}")
        return False


def _record_asked(teacher_id: str, class_id: str, period_number: int, run_date: str, ac) -> None:
    try:
        ac.table(TABLE).insert({
            "teacher_id": teacher_id, "class_id": class_id,
            "period_number": period_number, "run_date": run_date,
        }).execute()
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_class_feedback] _record_asked failed: {e}")


async def _ask_one(teacher_id: str, ctx: dict, entry: dict, run_date: str, ac) -> None:
    period_number = entry["periodNumber"]
    if _already_asked(teacher_id, period_number, run_date, ac):
        return
    # Recorded regardless of whether there was a topic to ask about — same
    # "don't recheck a quiet period every poll" rule teacher_period_briefing.py uses.
    _record_asked(teacher_id, entry["classId"], period_number, run_date, ac)

    subject = entry.get("label") or ""
    if not subject:
        return
    topic_info = resolve_period_topic(entry["classId"], subject, run_date, period_number, ac)
    if not topic_info:
        return

    class_name = entry.get("className") or "class"
    save_message(
        ctx["schoolId"], sender="teacher_agent",
        content=f'How did "{topic_info["topic"]}" go in {class_name}?',
        thread="teacher", teacher_id=teacher_id,
        # Structured, not just the question text — lets the frontend render
        # a real three-question + free-text card (same shape as Classroom
        # Mode's own reflection screen) and submit it directly, without
        # needing the LLM to interpret a free-text reply. The question text
        # above stays too, so a teacher can still just type a reply if they
        # prefer — save_class_feedback (teacher_agent.py) is that fallback.
        feedback_request={
            "classId": entry["classId"], "className": class_name, "subject": subject,
            "topic": topic_info["topic"], "subtopic": topic_info.get("subtopic"),
        },
    )


async def maybe_ask_class_feedback(teacher_id: str, ctx: dict) -> None:
    if not ctx.get("schoolId"):
        return
    ac = create_admin_client()
    now = _ist_now()
    today = now.date().isoformat()
    now_hm = now.strftime("%H:%M")

    try:
        schedule = resolve_day_timetable(teacher_id, today, ac)
    except Exception as e:
        print(f"[teacher_class_feedback] resolve_day_timetable failed: {e}")
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
            await _ask_one(teacher_id, ctx, entry, today, ac)
        except Exception as e:
            print(f"[teacher_class_feedback] ask failed for period {entry.get('periodNumber')}: {e}")
