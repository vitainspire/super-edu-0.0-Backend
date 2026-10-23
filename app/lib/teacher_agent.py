"""The Teacher Agent — one per teacher, scoped strictly to their own classes
and students. Same reason/act/observe shape as app/lib/argus_agent.py's
run_agent, but a single tool catalog (no persona split — there's only one
agent on this side, so no routing classifier is needed either).

Read tools call existing app/routes/teacher.py route functions directly as
plain Python calls — the same reuse pattern argus_agent.py already
established for the admin side (e.g. _tool_overview calls
admin_misc.get_overview). Every one of those functions already takes
`teacher_id: str = Depends(require_teacher)` as a keyword-defaultable
parameter, so calling them outside of FastAPI's request cycle is safe and
exactly how Depends-based functions are meant to be reusable.

Two things deliberately do NOT reuse a teacher.py route function:
- get_announcements: the real route (`announcements(request: Request)`)
  resolves its teacher/school scope from the raw Request object itself
  (soft-auth pattern), which isn't a clean fit for a plain function call with
  just a teacher_id — a small direct query stands in instead.
- request_leave: `post_leave` has the same Request-coupling. Its actual
  write, though, goes through a real reusable helper
  (`admin_queries.create_pending_leave_request`) — this tool replicates
  post_leave's validation and calls that helper directly, so nothing is
  duplicated at the write layer, only the validation shape.

get_profile reuses `get_own_profile` by constructing the minimal `user` dict
it needs (`{"id": teacher_id}`) rather than a direct query — confirmed
`teachers.id` IS the authenticated user's id here (`get_own_profile` itself
queries `teachers` by `user["id"]`), so this is a real, correct reuse, not a
workaround.
"""
import json
import time
from collections import Counter
from datetime import date, datetime, timedelta, timezone

from .ai import call_ai
from .supabase_clients import create_admin_client
from .teacher_context import get_teacher_context
from .admin_queries import create_pending_leave_request, fetch_teacher_availability_for_teacher
from .teacher_agent_memory import load_active_notes, save_note, archive_note_by_ref
from .ask_history import history_block
from .logger import api_log
from .schemas import TeacherTestUpsertSchema
from ..routes.teacher import (
    get_own_classes, get_students_by_classes, get_own_marks, get_attendance_by_classes,
    get_student_doubts, get_own_timetable, get_own_profile, get_shared_prep_material,
    upsert_own_test, _class_row_to_dto,
)

MAX_STEPS = 6
ALLOWED_LEAVE_REASONS = {"on_leave", "late_arrival", "official_duty", "other"}
MAX_LEAVE_RANGE_DAYS = 60


def _class_ids_csv(class_ids: list) -> str:
    return ",".join(class_ids)


# ---------------------------------------------------------------------------
# Read tools
# ---------------------------------------------------------------------------

def _tool_my_classes(teacher_id: str, ctx: dict, args: dict) -> dict:
    return get_own_classes(teacher_id=teacher_id)


def _tool_my_students(teacher_id: str, ctx: dict, args: dict) -> dict:
    return get_students_by_classes(classIds=_class_ids_csv(ctx["classIds"]), teacher_id=teacher_id)


def _tool_marks(teacher_id: str, ctx: dict, args: dict) -> dict:
    return get_own_marks(teacher_id=teacher_id)


def _tool_attendance(teacher_id: str, ctx: dict, args: dict) -> dict:
    return get_attendance_by_classes(classIds=_class_ids_csv(ctx["classIds"]), teacher_id=teacher_id)


def _tool_doubts(teacher_id: str, ctx: dict, args: dict) -> dict:
    return get_student_doubts(classIds=_class_ids_csv(ctx["classIds"]), teacher_id=teacher_id)


def _tool_announcements(teacher_id: str, ctx: dict, args: dict) -> dict:
    ac = create_admin_client()
    if not ctx["schoolId"]:
        return {"announcements": []}
    rows = (
        ac.table("announcements").select("*").eq("school_id", ctx["schoolId"])
        .order("created_at", desc=True).limit(10).execute().data or []
    )
    return {"announcements": rows}


def _tool_syllabus_status(teacher_id: str, ctx: dict, args: dict) -> dict:
    """Teacher-scoped variant of argus_agent._tool_syllabus_status — filtered
    to this teacher's own classes rather than a school-wide grade+subject."""
    ac = create_admin_client()
    class_ids = ctx["classIds"]
    if not class_ids:
        return {"sections": []}
    rows = (
        ac.table("syllabus_topics").select("class_id, subject, is_completed")
        .in_("class_id", class_ids).execute().data or []
    )
    classes = get_own_classes(teacher_id=teacher_id).get("classes", [])
    class_by_id = {c["id"]: c for c in classes}
    by_class_subject: dict = {}
    for r in rows:
        by_class_subject.setdefault((r["class_id"], r["subject"]), []).append(r)

    sections = []
    for (class_id, subject), topic_rows in by_class_subject.items():
        total = len(topic_rows)
        completed = sum(1 for r in topic_rows if r.get("is_completed"))
        cls = class_by_id.get(class_id, {})
        sections.append({
            "className": cls.get("name") or f'{cls.get("grade")} {cls.get("section")}',
            "subject": subject, "completed": completed, "total": total,
        })
    return {"sections": sections}


def _tool_leave_status(teacher_id: str, ctx: dict, args: dict) -> dict:
    ac = create_admin_client()
    today = datetime.now(timezone.utc).date().isoformat()
    rows = fetch_teacher_availability_for_teacher(teacher_id, today, ac)
    return {"leaves": rows}


def _tool_timetable(teacher_id: str, ctx: dict, args: dict) -> dict:
    return get_own_timetable(teacher_id=teacher_id)


def _tool_profile(teacher_id: str, ctx: dict, args: dict) -> dict:
    try:
        return {"profile": get_own_profile(user={"id": teacher_id})}
    except Exception as e:
        return {"error": str(e)}


def _tool_get_prep_material(teacher_id: str, ctx: dict, args: dict) -> dict:
    """Checks two different tables, in order — generate_prep_material saves
    to this teacher's own prep_materials (source='personal'); the shared pool
    (shared_prep_materials, one lesson per school+grade+subject+topic, read
    by every section teaching it) is a separate table entirely. A personal
    copy, when one exists, is the more specific answer and wins."""
    subject = (args.get("subject") or "").strip()
    topic = (args.get("topic") or "").strip()
    class_ref = (args.get("classRef") or "").strip()
    if not subject or not topic or not class_ref:
        return {"error": "classRef, subject and topic are all required."}
    match = _match_class(teacher_id, class_ref)
    if match["kind"] == "none":
        return {"error": f'No class matching "{class_ref}".'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one class matches "{class_ref}": {", ".join(match["candidates"])} — which one?'}
    cls = match["class"]

    ac = create_admin_client()
    personal = (
        ac.table("prep_materials").select("lesson")
        .eq("teacher_id", teacher_id).eq("class_id", cls["id"]).ilike("topic", topic)
        .order("created_at", desc=True).limit(1).execute().data or []
    )
    if personal:
        return {"lesson": personal[0]["lesson"], "source": "personal"}

    shared = get_shared_prep_material(
        classId=cls["id"], subject=subject, grade=cls["grade"], topic=topic,
        subtopic=args.get("subtopic"), teacher_id=teacher_id,
    )
    if shared.get("lesson") is not None:
        return {"lesson": shared["lesson"], "source": "shared"}
    return {"lesson": None}


# ---------------------------------------------------------------------------
# Action tools — propose-only, same contract as argus_agent.py's. Real writes
# only ever happen via POST /api/teacher/agent/confirm
# (app/lib/teacher_confirm_dispatch.py), never from inside this file.
# ---------------------------------------------------------------------------

def _match_student(ctx: dict, name_ref: str, ac) -> dict:
    class_ids = ctx["classIds"]
    if not class_ids or not name_ref:
        return {"kind": "none"}
    rows = (
        ac.table("students").select("id, name, class_id").in_("class_id", class_ids).execute().data or []
    )
    ref = name_ref.strip().lower()
    hits = [r for r in rows if ref in (r.get("name") or "").lower()]
    if not hits:
        return {"kind": "none"}
    if len(hits) > 1:
        return {"kind": "ambiguous", "candidates": [h["name"] for h in hits]}
    return {"kind": "found", "student": hits[0]}


def _match_class(teacher_id: str, class_ref: str) -> dict:
    """Same found/ambiguous/none contract as _match_student, for classRef
    instead of a student name — used by create_test, get_prep_material,
    generate_prep_material and record_attendance_bulk."""
    class_ref = (class_ref or "").strip()
    if not class_ref:
        return {"kind": "none"}
    classes = get_own_classes(teacher_id=teacher_id).get("classes", [])
    ref = class_ref.lower()
    hits = [c for c in classes if ref in (c.get("name") or "").lower()]
    if not hits:
        return {"kind": "none"}
    if len(hits) > 1:
        return {"kind": "ambiguous", "candidates": [c["name"] for c in hits]}
    return {"kind": "found", "class": hits[0]}


def _tool_record_mark(teacher_id: str, ctx: dict, args: dict) -> dict:
    student_ref = (args.get("studentRef") or "").strip()
    test_id = (args.get("testId") or "").strip()
    score = args.get("score")
    if not student_ref or not test_id or score is None:
        return {"error": "studentRef, testId and score are all required"}
    ac = create_admin_client()
    match = _match_student(ctx, student_ref, ac)
    if match["kind"] == "none":
        return {"error": f'No student matching "{student_ref}" in your classes.'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one student matches "{student_ref}": {", ".join(match["candidates"])} — which one?'}
    student = match["student"]
    import uuid
    return {
        "proposal": {
            "tool": "record_mark",
            "id": str(uuid.uuid4()), "testId": test_id, "studentId": student["id"],
            "score": float(score), "feedback": args.get("feedback"),
            "enteredAt": datetime.now(timezone.utc).isoformat(), "source": "agent",
        },
        "label": f'Record a mark of {score} for {student["name"]}',
    }


def _tool_record_attendance(teacher_id: str, ctx: dict, args: dict) -> dict:
    student_ref = (args.get("studentRef") or "").strip()
    status = (args.get("status") or "").strip()
    date_str = args.get("date") or datetime.now(timezone.utc).date().isoformat()
    if not student_ref or not status:
        return {"error": "studentRef and status are both required"}
    ac = create_admin_client()
    match = _match_student(ctx, student_ref, ac)
    if match["kind"] == "none":
        return {"error": f'No student matching "{student_ref}" in your classes.'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one student matches "{student_ref}": {", ".join(match["candidates"])} — which one?'}
    student = match["student"]
    import uuid
    return {
        "proposal": {
            "tool": "record_attendance",
            "id": str(uuid.uuid4()), "studentId": student["id"], "classId": student["class_id"],
            "date": date_str, "status": status,
        },
        "label": f'Mark {student["name"]} as {status} on {date_str}',
    }


def _tool_answer_doubt(teacher_id: str, ctx: dict, args: dict) -> dict:
    doubt_id = (args.get("doubtId") or "").strip()
    answer = (args.get("answer") or "").strip()
    if not doubt_id or not answer:
        return {"error": "doubtId and answer are both required — look up doubtId with get_doubts first."}
    return {
        "proposal": {"tool": "answer_doubt", "id": doubt_id, "answer": answer},
        "label": "Answer this student doubt",
    }


def _tool_add_intervention_note(teacher_id: str, ctx: dict, args: dict) -> dict:
    student_ref = (args.get("studentRef") or "").strip()
    note = (args.get("note") or "").strip()
    if not student_ref or not note:
        return {"error": "studentRef and note are both required"}
    ac = create_admin_client()
    match = _match_student(ctx, student_ref, ac)
    if match["kind"] == "none":
        return {"error": f'No student matching "{student_ref}" in your classes.'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one student matches "{student_ref}": {", ".join(match["candidates"])} — which one?'}
    student = match["student"]
    import uuid
    return {
        "proposal": {
            "tool": "add_intervention_note",
            "id": str(uuid.uuid4()), "studentId": student["id"], "note": note,
            "date": datetime.now(timezone.utc).date().isoformat(),
        },
        "label": f'Add an intervention note for {student["name"]}',
    }


def _tool_request_leave(teacher_id: str, ctx: dict, args: dict) -> dict:
    """Validates like post_leave does, but only proposes — the actual writes
    (one per day in range, via the same create_pending_leave_request helper
    post_leave itself calls) only happen on confirm."""
    reason = args.get("reason") if args.get("reason") in ALLOWED_LEAVE_REASONS else None
    start_str, end_str = args.get("startDate"), args.get("endDate") or args.get("startDate")
    if not reason or not start_str:
        return {"error": "reason and startDate are required (endDate optional, defaults to startDate)."}
    try:
        start = date.fromisoformat(start_str)
        end = date.fromisoformat(end_str)
    except ValueError:
        return {"error": "Dates must be YYYY-MM-DD."}
    if end < start:
        return {"error": "endDate must be on or after startDate."}
    if (end - start).days + 1 > MAX_LEAVE_RANGE_DAYS:
        return {"error": f"Leave requests can span at most {MAX_LEAVE_RANGE_DAYS} days."}

    date_label = start_str if start_str == end_str else f"{start_str} to {end_str}"
    return {
        "proposal": {
            "tool": "request_leave", "startDate": start_str, "endDate": end_str,
            "reason": reason, "note": args.get("note"),
        },
        "label": f"Request leave ({reason.replace('_', ' ')}) for {date_label}",
    }


def _tool_create_test(teacher_id: str, ctx: dict, args: dict) -> dict:
    subject = (args.get("subject") or "").strip()
    topic = (args.get("topic") or "").strip()
    total_marks = args.get("totalMarks")
    conducted_on = (args.get("conductedOn") or "").strip()
    if not subject or not topic or total_marks is None or not conducted_on:
        return {"error": "subject, topic, totalMarks and conductedOn are all required."}
    class_id = None
    class_ref = (args.get("classRef") or "").strip()
    if class_ref:
        match = _match_class(teacher_id, class_ref)
        if match["kind"] == "ambiguous":
            return {"error": f'More than one class matches "{class_ref}": {", ".join(match["candidates"])} — which one?'}
        if match["kind"] == "found":
            class_id = match["class"]["id"]
    import uuid
    return {
        "proposal": {
            "tool": "create_test",
            "id": str(uuid.uuid4()), "classId": class_id, "subject": subject, "topic": topic,
            "totalMarks": float(total_marks), "conductedOn": conducted_on, "term": args.get("term"),
        },
        "label": f'Create a test "{topic}" ({subject}) on {conducted_on}',
    }


def _tool_generate_prep_material(teacher_id: str, ctx: dict, args: dict) -> dict:
    """Only resolves classRef and validates inputs — cheap and synchronous,
    same as every other action tool's fn. The real generation (a genuine
    LLM + image pipeline, tens of seconds, real cost) only runs on confirm,
    via teacher_confirm_dispatch.py's await of generate_smart_lesson."""
    subject = (args.get("subject") or "").strip()
    topic = (args.get("topic") or "").strip()
    class_ref = (args.get("classRef") or "").strip()
    if not subject or not topic or not class_ref:
        return {"error": "classRef, subject and topic are all required."}
    match = _match_class(teacher_id, class_ref)
    if match["kind"] == "none":
        return {"error": f'No class matching "{class_ref}".'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one class matches "{class_ref}": {", ".join(match["candidates"])} — which one?'}
    cls = match["class"]
    below_grade_level = bool(args.get("belowGradeLevel"))
    label = f'Generate and save {"a simplified, below-grade-level " if below_grade_level else ""}prep material for "{topic}" ({subject}, {cls["name"]}) — this takes a little while and uses a real AI call'
    return {
        "proposal": {
            "tool": "generate_prep_material",
            "classId": cls["id"], "grade": cls["grade"], "subject": subject,
            "topic": topic, "subtopic": args.get("subtopic"), "belowGradeLevel": below_grade_level,
        },
        "label": label,
    }


def _tool_record_attendance_bulk(teacher_id: str, ctx: dict, args: dict) -> dict:
    class_ref = (args.get("classRef") or "").strip()
    if not class_ref:
        return {"error": "classRef is required."}
    match = _match_class(teacher_id, class_ref)
    if match["kind"] == "none":
        return {"error": f'No class matching "{class_ref}".'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one class matches "{class_ref}": {", ".join(match["candidates"])} — which one?'}
    cls = match["class"]
    date_str = args.get("date") or datetime.now(timezone.utc).date().isoformat()
    default_status = (args.get("defaultStatus") or "present").strip()

    ac = create_admin_client()
    students = (
        ac.table("students").select("id, name").eq("class_id", cls["id"]).eq("is_active", True).execute().data or []
    )
    if not students:
        return {"error": f'No active students found in {cls["name"]}.'}

    statuses = {s["id"]: default_status for s in students}
    unresolved = []
    for exc in (args.get("exceptions") or []):
        ref = (exc.get("studentRef") or "").strip().lower()
        status = (exc.get("status") or "").strip()
        if not ref or not status:
            continue
        hits = [s for s in students if ref in s["name"].lower()]
        if len(hits) == 1:
            statuses[hits[0]["id"]] = status
        else:
            unresolved.append(exc.get("studentRef"))
    if unresolved:
        return {"error": f'Could not uniquely match these students in {cls["name"]}: {", ".join(unresolved)}.'}

    import uuid
    records = [
        {"id": str(uuid.uuid4()), "studentId": sid, "classId": cls["id"], "date": date_str, "status": status}
        for sid, status in statuses.items()
    ]
    counts = Counter(statuses.values())
    summary = ", ".join(f"{n} {s}" for s, n in counts.items())
    return {
        "proposal": {"tool": "record_attendance_bulk", "records": records},
        "label": f'Mark attendance for {cls["name"]} on {date_str}: {summary}',
    }


def _tool_record_marks_bulk(teacher_id: str, ctx: dict, args: dict) -> dict:
    test_id = (args.get("testId") or "").strip()
    scores = args.get("scores") or []
    if not test_id or not scores:
        return {"error": "testId and scores are both required — scores is a list of {studentRef, score}."}

    ac = create_admin_client()
    records, unresolved, ambiguous = [], [], []
    import uuid
    for item in scores:
        student_ref = (item.get("studentRef") or "").strip()
        score = item.get("score")
        if not student_ref or score is None:
            continue
        match = _match_student(ctx, student_ref, ac)
        if match["kind"] == "none":
            unresolved.append(student_ref)
            continue
        if match["kind"] == "ambiguous":
            ambiguous.append(student_ref)
            continue
        student = match["student"]
        records.append({
            "id": str(uuid.uuid4()), "testId": test_id, "studentId": student["id"],
            "score": float(score), "feedback": item.get("feedback"),
            "enteredAt": datetime.now(timezone.utc).isoformat(), "source": "agent",
        })

    if unresolved or ambiguous:
        parts = []
        if unresolved:
            parts.append(f'no match: {", ".join(unresolved)}')
        if ambiguous:
            parts.append(f'ambiguous: {", ".join(ambiguous)}')
        return {"error": "Couldn't resolve some students — " + "; ".join(parts)}
    if not records:
        return {"error": "No valid scores to record."}

    return {
        "proposal": {"tool": "record_marks_bulk", "records": records},
        "label": f"Record marks for {len(records)} student(s)",
    }


def _tool_remember_note(teacher_id: str, ctx: dict, args: dict) -> dict:
    """No "proposal" key in the return — this never needs the propose/confirm
    gate, since it only ever touches the agent's own notes about the teacher,
    never a student/school record. The loop treats this like any other tool
    result and keeps going."""
    note = (args.get("note") or "").strip()
    if not note:
        return {"error": "note is required — say what to remember."}
    return save_note(teacher_id, ctx["schoolId"], note)


def _tool_forget_note(teacher_id: str, ctx: dict, args: dict) -> dict:
    note_ref = (args.get("noteRef") or "").strip()
    if not note_ref:
        return {"error": "noteRef is required — describe what to forget."}
    match = archive_note_by_ref(teacher_id, note_ref)
    if match["kind"] == "none":
        return {"error": f'Nothing remembered matching "{note_ref}".'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one remembered note matches "{note_ref}": {"; ".join(match["candidates"])} — which one?'}
    return {"forgotten": True}


TOOLS = {
    "get_my_classes": {
        "description": "List this teacher's own classes (owned + assigned).",
        "params": {}, "fn": _tool_my_classes,
    },
    "get_my_students": {
        "description": "List every student across this teacher's own classes.",
        "params": {}, "fn": _tool_my_students,
    },
    "get_marks": {
        "description": "All marks this teacher has recorded, across their own tests.",
        "params": {}, "fn": _tool_marks,
    },
    "get_attendance": {
        "description": "Attendance records across this teacher's own classes.",
        "params": {}, "fn": _tool_attendance,
    },
    "get_doubts": {
        "description": "Student doubts raised in this teacher's own classes, including doubtId needed to answer one.",
        "params": {}, "fn": _tool_doubts,
    },
    "get_announcements": {
        "description": "The 10 most recent school-wide announcements.",
        "params": {}, "fn": _tool_announcements,
    },
    "get_syllabus_status": {
        "description": "Syllabus completion per class/subject, for this teacher's own classes only.",
        "params": {}, "fn": _tool_syllabus_status,
    },
    "get_leave_status": {
        "description": "This teacher's own upcoming leave requests and their approval status.",
        "params": {}, "fn": _tool_leave_status,
    },
    "get_timetable": {
        "description": "This teacher's own weekly recurring timetable (read-only — timetable changes go through the school admin).",
        "params": {}, "fn": _tool_timetable,
    },
    "get_profile": {
        "description": "This teacher's own profile — name, subject, grade, phone, language preference.",
        "params": {}, "fn": _tool_profile,
    },
    "get_prep_material": {
        "description": "Check whether prep material already exists for a topic in one of this teacher's classes — fast, no generation.",
        "params": {"classRef": "string, required", "subject": "string, required", "topic": "string, required", "subtopic": "string, optional"},
        "fn": _tool_get_prep_material,
    },
    "record_mark": {
        "description": "Record a mark for a student on a test.",
        "params": {"studentRef": "string, required", "testId": "string, required — get it from get_marks or ask the teacher", "score": "number, required", "feedback": "string, optional"},
        "side_effect": "action", "fn": _tool_record_mark,
    },
    "record_attendance": {
        "description": "Record a student's attendance status for a date.",
        "params": {"studentRef": "string, required", "status": "string, required, e.g. present/absent/late", "date": "string, optional, ISO date, defaults to today"},
        "side_effect": "action", "fn": _tool_record_attendance,
    },
    "answer_doubt": {
        "description": "Answer a student's doubt.",
        "params": {"doubtId": "string, required — get it from get_doubts", "answer": "string, required"},
        "side_effect": "action", "fn": _tool_answer_doubt,
    },
    "add_intervention_note": {
        "description": "Add an intervention note for a student who needs extra support.",
        "params": {"studentRef": "string, required", "note": "string, required"},
        "side_effect": "action", "fn": _tool_add_intervention_note,
    },
    "request_leave": {
        "description": "Request leave for a date or date range. Stays pending until the school admin approves it.",
        "params": {"reason": "one of on_leave/late_arrival/official_duty/other, required", "startDate": "string, required, ISO date", "endDate": "string, optional, defaults to startDate", "note": "string, optional"},
        "side_effect": "action", "fn": _tool_request_leave,
    },
    "create_test": {
        "description": "Create a new test for this teacher's own class.",
        "params": {"subject": "string, required", "topic": "string, required", "totalMarks": "number, required", "conductedOn": "string, required, ISO date", "classRef": "string, optional", "term": "string, optional"},
        "side_effect": "action", "fn": _tool_create_test,
    },
    "generate_prep_material": {
        "description": "Generate and save a new prep sheet for a topic using the real AI lesson pipeline, grounded in the actual textbook. Slow (tens of seconds) and costs real money — check get_prep_material first in case one already exists. Refuses if no real textbook match is found for the topic, rather than generating ungrounded content.",
        "params": {"classRef": "string, required", "subject": "string, required", "topic": "string, required", "subtopic": "string, optional", "belowGradeLevel": "boolean, optional — set true when the teacher asks for a simpler version for a class reading below grade level"},
        "side_effect": "action", "fn": _tool_generate_prep_material,
    },
    "record_attendance_bulk": {
        "description": "Mark attendance for an entire class at once — everyone gets defaultStatus unless listed in exceptions.",
        "params": {"classRef": "string, required", "date": "string, optional, ISO date, defaults to today", "defaultStatus": "string, optional, defaults to present", "exceptions": "list of {studentRef, status}, optional"},
        "side_effect": "action", "fn": _tool_record_attendance_bulk,
    },
    "record_marks_bulk": {
        "description": "Record marks for several students on the same test in one go.",
        "params": {"testId": "string, required", "scores": "list of {studentRef, score, feedback?}, required"},
        "side_effect": "action", "fn": _tool_record_marks_bulk,
    },
    "remember_note": {
        "description": "Save a short note about this teacher's preferences or recurring concerns, to be remembered in future conversations too. Saves immediately, no confirmation needed.",
        "params": {"note": "string, required, under 240 characters"},
        "fn": _tool_remember_note,
    },
    "forget_note": {
        "description": "Delete a previously remembered note that's wrong or no longer relevant.",
        "params": {"noteRef": "string, required — a word or phrase from the note to forget"},
        "fn": _tool_forget_note,
    },
}


def _tool_catalog_text() -> str:
    lines = []
    for name, spec in TOOLS.items():
        params = ", ".join(f"{k} ({v})" for k, v in spec["params"].items()) or "none"
        tag = " [ACTION — calling this only proposes it; nothing happens until the teacher confirms]" if spec.get("side_effect") == "action" else ""
        lines.append(f'- "{name}"{tag}: {spec["description"]} Params: {params}.')
    return "\n".join(lines)


def _scratchpad_text(scratchpad: list) -> str:
    if not scratchpad:
        return "(none yet)"
    return "\n".join(f"- {r['tool']}({json.dumps(r['args'])}) -> {json.dumps(r['result'])}" for r in scratchpad)


def _memory_block(notes: list) -> str:
    if not notes:
        return ""
    lines = "\n".join(f"- {n['content']}" for n in notes)
    return f"""

What you remember about this teacher from earlier conversations (their own
words — use it, but a tool result always wins if the two conflict):
{lines}"""


async def run_teacher_agent(teacher_id: str, question: str, history: list, ip: str) -> dict:
    """Same three-shape return contract as argus_agent.run_agent:
      - {"status": "answered", "response": ..., "trace": [...]}
      - {"status": "pending_confirmation", "proposal": {...}, "label": ..., "trace": [...]}
      - {"status": "error" | "step_limit", "response": ..., "trace": [...]}
    """
    ctx = get_teacher_context(teacher_id)
    notes = load_active_notes(teacher_id)
    scratchpad: list[dict] = []
    trace: list[dict] = []

    for _ in range(MAX_STEPS):
        prompt = f"""You are this teacher's own assistant. Your scope is only THEIR OWN
classes and students — you have no visibility into any other teacher's
classes. You never state a fact yourself; every fact in your final answer
must come from a tool result below, not from your own generation. If the
message is a casual greeting or too vague to act on ("hello", "hi", "you
there?"), reply conversationally and ask what's needed — do not call a tool
until there's an actual question or request to answer. When the
teacher tells you a preference or a recurring concern worth carrying into
future conversations, call remember_note to save it — not every fact already
available from a tool, just what's worth remembering about THEM.

The teacher asked: "{question}"
{history_block(history)}
{_memory_block(notes)}

Tools available — call as many as you need, in any order:
{_tool_catalog_text()}

Results gathered so far this run:
{_scratchpad_text(scratchpad)}

Decide your next step. Return JSON only, no markdown, one of:
{{"action": "call_tool", "tool": "<name>", "args": {{...}}}}
{{"action": "finish", "response": "<plain, simple-English answer to the teacher>"}}
"""
        t0 = time.time()
        try:
            raw = await call_ai([{"role": "user", "content": prompt}])
            decision = json.loads(raw)
        except Exception as e:
            api_log("teacher-agent", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
            return {"status": "error", "response": "Something went wrong reasoning about that — try again.", "trace": trace}

        action = decision.get("action")

        if action == "finish":
            api_log("teacher-agent", ip, (time.time() - t0) * 1000, False, "ok")
            return {"status": "answered", "response": decision.get("response") or "I didn't find anything that answers that.", "trace": trace}

        if action == "call_tool":
            tool_name = decision.get("tool")
            args = decision.get("args") or {}
            tool = TOOLS.get(tool_name)
            if not tool:
                result = {"error": f'unknown tool "{tool_name}"'}
            else:
                try:
                    result = tool["fn"](teacher_id, ctx, args)
                except Exception as e:
                    result = {"error": str(e)}

            if isinstance(result, dict) and "proposal" in result:
                api_log("teacher-agent", ip, (time.time() - t0) * 1000, False, "ok")
                trace.append({"tool": tool_name, "args": args, "ok": True})
                return {"status": "pending_confirmation", "proposal": result["proposal"], "label": result.get("label"), "trace": trace}

            scratchpad.append({"tool": tool_name, "args": args, "result": result})
            trace.append({"tool": tool_name, "args": args, "ok": "error" not in result})
            continue

        return {"status": "error", "response": "I didn't quite understand what to do with that — try rephrasing.", "trace": trace}

    return {"status": "step_limit", "response": "That took more steps than expected — try a more specific question.", "trace": trace}
