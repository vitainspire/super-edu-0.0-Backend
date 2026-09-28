import json
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_exam_plan_items, upsert_exam_plan_item, delete_exam_plan_item,
    fetch_school_announcements, create_announcement, delete_announcement,
    fetch_school_teachers, fetch_school_classes, fetch_school_schedule, upsert_school_schedule,
)
from ..lib.schemas import AnnouncementSchema, AdminAskIntentSchema
from ..lib.ai import call_ai
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit
from ..deps import require_admin

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# Admin notification routes live in app/routes/admin_notifications.py
# (app/lib/notifications.py), not here — see that file for the inbox routes.


# ─── Exam plan ─────────────────────────────────────────────────────────────────

@router.get("/{schoolId}/exam-plan")
def get_exam_plan(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"items": fetch_exam_plan_items(schoolId, ac)}


class ExamPlanItemBody(BaseModel):
    id: Optional[str] = None
    name: str
    count: Optional[float] = None
    orderIndex: Optional[int] = None


@router.post("/{schoolId}/exam-plan")
def post_exam_plan_item(schoolId: str, body: ExamPlanItemBody, admin: dict = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")

    ac = create_admin_client()
    order_index = body.orderIndex
    if order_index is None:
        order_index = len(fetch_exam_plan_items(schoolId, ac))

    item = {
        "id": body.id or str(uuid.uuid4()), "schoolId": schoolId, "name": body.name.strip(),
        "count": max(1, int(body.count)) if isinstance(body.count, (int, float)) else 1,
        "orderIndex": order_index, "createdAt": _now(),
    }
    upsert_exam_plan_item(item, ac)
    return {"item": item}


class DeleteIdBody(BaseModel):
    id: str


@router.delete("/{schoolId}/exam-plan")
def delete_exam_plan_item_route(schoolId: str, body: DeleteIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_exam_plan_item(body.id, ac)
    return {"ok": True}


# ─── Scanners ──────────────────────────────────────────────────────────────────

@router.get("/{schoolId}/scanners")
def get_scanners(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    data = (
        ac.table("scanner_profiles").select("id, name, email, created_at").eq("school_id", schoolId)
        .order("created_at", desc=False).execute().data or []
    )
    return {"scanners": data}


class ScannerBody(BaseModel):
    name: str
    email: str
    password: str


@router.post("/{schoolId}/scanners")
def post_scanner(schoolId: str, body: ScannerBody, admin: dict = Depends(require_admin)):
    if not body.name.strip() or not body.email.strip() or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Name, email and password (min 6 chars) are required.")

    ac = create_admin_client()
    try:
        auth_res = ac.auth.admin.create_user({
            "email": body.email.strip().lower(), "password": body.password, "email_confirm": True,
            "user_metadata": {"name": body.name.strip(), "role": "scanner"},
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    scanner_id = auth_res.user.id
    school_res = ac.table("schools").select("name").eq("id", schoolId).maybe_single().execute()
    school_name = (school_res.data or {}).get("name", "") if school_res else ""

    try:
        ac.table("scanner_profiles").insert({
            "user_id": scanner_id, "school_id": schoolId, "name": body.name.strip(), "email": body.email.strip().lower(),
        }).execute()
    except Exception as e:
        ac.auth.admin.delete_user(scanner_id)
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "schoolName": school_name}


class ScannerIdBody(BaseModel):
    scannerId: str


@router.delete("/{schoolId}/scanners")
def delete_scanner(schoolId: str, body: ScannerIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    profile_res = ac.table("scanner_profiles").select("user_id").eq("id", body.scannerId).eq("school_id", schoolId).maybe_single().execute()
    profile = profile_res.data if profile_res else None

    ac.table("scanner_profiles").delete().eq("id", body.scannerId).execute()
    if profile and profile.get("user_id"):
        ac.auth.admin.delete_user(profile["user_id"])
    return {"ok": True}


# ─── Overview ──────────────────────────────────────────────────────────────────

@router.get("/{schoolId}/overview")
def get_overview(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    teachers = fetch_school_teachers(schoolId, ac)
    classes = fetch_school_classes(schoolId, ac)

    class_ids = [c["id"] for c in classes]
    students = ac.table("students").select("id, class_id").in_("class_id", class_ids).execute().data if class_ids else []
    timetable = ac.table("timetable").select("teacher_id, class_id").in_("class_id", class_ids).execute().data if class_ids else []

    total_periods = len(timetable)
    published_periods = len([t for t in timetable if t.get("teacher_id")])
    timetable_coverage = round((published_periods / total_periods) * 100) if total_periods > 0 else 0

    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_ago = (datetime.now(timezone.utc) - timedelta(days=6)).strftime("%Y-%m-%d")

    if class_ids:
        assignments = ac.table("teacher_class_assignments").select("class_id").in_("class_id", class_ids).execute().data or []
        attendance = ac.table("attendance").select("status").in_("class_id", class_ids).gte("date", week_ago).execute().data or []
        pending_doubts = (
            ac.table("student_doubts").select("id", count="exact", head=True)
            .in_("class_id", class_ids).eq("status", "pending").execute().count or 0
        )
    else:
        assignments, attendance, pending_doubts = [], [], 0

    teachers_absent_today = (
        ac.table("teacher_availability").select("id", count="exact", head=True)
        .eq("school_id", schoolId).eq("date", today).execute().count or 0
    )
    substitutions = (
        ac.table("timetable_substitutions").select("status")
        .eq("school_id", schoolId).eq("date", today).execute().data or []
    )

    with_teacher = {a["class_id"] for a in assignments}
    with_students = {s["class_id"] for s in students}
    unresolved_cover = len([s for s in substitutions if s.get("status") == "unresolved"])

    present = len([a for a in attendance if a.get("status") in ("present", "late")])

    return {
        "teacherCount": len(teachers), "classCount": len(classes), "studentCount": len(students or []),
        "timetableCoverage": timetable_coverage, "totalPeriods": total_periods,

        # ── Readiness: what is stopping this school from working ──────────
        # Counts of the gap, not of what exists. "40 classes" reads healthy
        # while 36 of them have nobody teaching them, nobody in them, and no
        # syllabus.
        "readiness": {
            "classesWithoutTeacher": len([c for c in classes if c["id"] not in with_teacher]),
            "classesWithoutStudents": len([c for c in classes if c["id"] not in with_students]),
            "unstaffedPeriods": len([t for t in timetable if not t.get("teacher_id")]),
            "publishedPeriods": published_periods,
        },

        # ── Operations: what needs attention today ─────────────────────────
        "operations": {
            # None rather than 0 when nothing has been recorded: a school
            # that has taken no attendance is not a school with 0% attendance.
            "attendanceRate": round((present / len(attendance)) * 100) if attendance else None,
            "attendanceRecords": len(attendance),
            "pendingDoubts": pending_doubts,
            "teachersAbsentToday": teachers_absent_today,
            # The one an admin must see before the first bell: a period whose
            # teacher is away and for whom nobody has been found.
            "unresolvedCover": unresolved_cover,
        },
    }


# ─── Prep materials (admin browsing) ─────────────────────────────────────────

@router.get("/{schoolId}/prep-materials")
def get_prep_materials(schoolId: str, admin: dict = Depends(require_admin)):
    """Every Prep Material generated by any teacher in this school, for admin
    browsing. Deliberately excludes the "lesson" column: fetching the full
    generated lesson (which can carry embedded image references) for every row
    at once is what made this endpoint time out in practice against a school
    with only 80 rows — the list view only ever renders a row's lesson once
    that row is expanded (see PrepSheetView in prep-materials/page.tsx), so
    each one is fetched on demand instead via GET .../prep-materials/{id}."""
    ac = create_admin_client()
    classes = fetch_school_classes(schoolId, ac)
    teachers = fetch_school_teachers(schoolId, ac)

    class_ids = [c["id"] for c in classes]
    if not class_ids:
        return {"materials": []}

    rows = (
        ac.table("prep_materials").select("id, class_id, teacher_id, grade, subject, topic, subtopic, created_at")
        .in_("class_id", class_ids).order("created_at", desc=True).execute()
    ).data or []

    # Session count per class+topic — how many times this was actually taught in
    # class, not just generated. Matched the same way the rest of the app matches
    # topic strings: trimmed, case-insensitive, keyed by class_id since topic
    # names aren't globally unique.
    session_rows = ac.table("sessions").select("class_id, topic").in_("class_id", class_ids).execute().data or []
    session_counts: dict[str, int] = {}
    for s in session_rows:
        key = f"{s['class_id']}|{(s.get('topic') or '').strip().lower()}"
        session_counts[key] = session_counts.get(key, 0) + 1

    class_by_id = {c["id"]: c for c in classes}
    teacher_by_id = {t["id"]: t for t in teachers}

    materials = []
    for r in rows:
        cls = class_by_id.get(r["class_id"])
        teacher = teacher_by_id.get(r.get("teacher_id"))
        session_key = f"{r['class_id']}|{(r.get('topic') or '').strip().lower()}"
        materials.append({
            "id": r["id"],
            "teacherName": (teacher or {}).get("name") or "Unknown teacher",
            "className": (cls or {}).get("name") or "Unknown class",
            "grade": r.get("grade") or (cls or {}).get("grade") or "",
            "subject": r.get("subject") or "",
            "topic": r.get("topic"),
            "subtopic": r.get("subtopic"),
            "createdAt": r.get("created_at"),
            "sessionCount": session_counts.get(session_key, 0),
        })

    return {"materials": materials}


@router.get("/{schoolId}/prep-materials/{materialId}")
def get_prep_material_lesson(schoolId: str, materialId: str, admin: dict = Depends(require_admin)):
    """The one field the list endpoint omits — fetched lazily per row, only
    once a teacher expands it."""
    ac = create_admin_client()
    row = (
        ac.table("prep_materials").select("lesson, class_id")
        .eq("id", materialId).maybe_single().execute()
    ).data
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    cls = ac.table("classes").select("school_id").eq("id", row["class_id"]).maybe_single().execute().data
    if not cls or cls.get("school_id") != schoolId:
        raise HTTPException(status_code=403, detail="Forbidden")
    return {"lesson": row.get("lesson")}


# ─── Prep materials (on-demand generation from a published textbook) ─────────
#
# The shared-batch generator (prep_batch_jobs.py) tops up material for a
# school's own syllabus automatically in the background. These routes are the
# manual counterpart: an admin picks a real published textbook chapter and
# generates + saves its lessons right away, via
# prep_batch_jobs.save_published_chapter_lessons -- the same pipeline, just
# triggered on demand instead of waiting for ensure_stock() to reach it.

@router.get("/{schoolId}/prep-materials/published-books")
def list_published_books(schoolId: str, admin: dict = Depends(require_admin)):
    """Proxies the published-textbook catalog so the admin panel can offer a
    picker -- this service is shared across every school (not scoped to
    schoolId), same as prep_pipeline_bridge.py's own catalog lookup."""
    import requests
    from ..lib.prep_pipeline_bridge import PUBLISHED_TEXTBOOK_API

    try:
        # Render free tier sleeps; a cold start is 30-60s (textbook_catalog.py's
        # own sync path hits the same thing and uses the same 90s allowance).
        resp = requests.get(f"{PUBLISHED_TEXTBOOK_API}/published/books", timeout=90)
        resp.raise_for_status()
        books = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Published textbook catalog unavailable: {e}")
    return {"books": [
        {"bookId": b.get("book_id"), "grade": b.get("grade"), "subject": b.get("subject"),
         "board": b.get("board"), "language": b.get("language"),
         "chaptersPublished": b.get("chapters_published"), "totalChapters": b.get("total_chapters")}
        for b in (books if isinstance(books, list) else [])
    ]}


@router.get("/{schoolId}/prep-materials/published-books/{bookId}/chapters")
def list_published_chapters(schoolId: str, bookId: str, admin: dict = Depends(require_admin)):
    """One book's chapter list (metadata only, no prose) -- for the chapter
    picker once a book is chosen."""
    import requests
    from ..lib.prep_pipeline_bridge import PUBLISHED_TEXTBOOK_API

    try:
        resp = requests.get(f"{PUBLISHED_TEXTBOOK_API}/published/books/{bookId}/chapters", timeout=90)
        resp.raise_for_status()
        chapters = resp.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Published textbook catalog unavailable: {e}")
    return {"chapters": [
        {"chapterNumber": c.get("chapter_number"), "chapterTitle": c.get("chapter_title"),
         "pageStart": c.get("page_start"), "pageEnd": c.get("page_end")}
        for c in (chapters if isinstance(chapters, list) else [])
    ]}


class GenerateFromBookBody(BaseModel):
    bookId: str
    chapterNumber: int


@router.post("/{schoolId}/prep-materials/generate-from-book")
def post_generate_from_book(schoolId: str, body: GenerateFromBookBody, admin: dict = Depends(require_admin)):
    """Starts the on-demand pipeline run in a background thread and returns a
    job id right away -- the run itself takes 1-3 minutes, far past what a
    browser/gateway will hold a request open for. Poll the GET below."""
    from ..lib.prep_batch_jobs import start_chapter_generation
    job_id = start_chapter_generation(schoolId, body.bookId, body.chapterNumber)
    return {"jobId": job_id}


@router.get("/{schoolId}/prep-materials/generate-from-book/{jobId}")
def get_generate_from_book_job(schoolId: str, jobId: str, admin: dict = Depends(require_admin)):
    from ..lib.prep_batch_jobs import get_chapter_job
    job = get_chapter_job(jobId)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


# ─── Announcements ──────────────────────────────────────────────────────────────

@router.get("/{schoolId}/announcements")
def get_announcements(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"announcements": fetch_school_announcements(schoolId, ac)}


@router.post("/{schoolId}/announcements", status_code=201)
def post_announcement(schoolId: str, body: AnnouncementSchema, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    announcement = {
        "id": str(uuid.uuid4()), "schoolId": schoolId, "adminId": admin["id"], "adminName": admin["name"],
        "title": body.title, "body": body.body, "category": body.category or "general", "createdAt": _now(),
    }
    create_announcement(announcement, ac)
    return {"announcement": announcement}


@router.delete("/{schoolId}/announcements")
def delete_announcement_route(schoolId: str, body: DeleteIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_announcement(body.id, schoolId, ac)
    return {"ok": True}


# ─── School schedule template ────────────────────────────────────────────────

@router.get("/{schoolId}/schedule")
def get_schedule(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"schedule": fetch_school_schedule(schoolId, ac)}


class ScheduleBody(BaseModel):
    slots: list[dict]


@router.post("/{schoolId}/schedule")
def post_schedule(schoolId: str, body: ScheduleBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    existing = fetch_school_schedule(schoolId, ac)
    schedule = {
        "id": (existing or {}).get("id") or str(uuid.uuid4()), "schoolId": schoolId,
        "slots": body.slots, "createdAt": (existing or {}).get("createdAt") or _now(),
    }
    try:
        upsert_school_schedule(schedule, ac)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"schedule": schedule}


# ─── Ask assistant — the admin-side harness ──────────────────────────────────
# Same shape as /api/ask-intent (ai_routes2.py) and the same reason: this is
# the ONLY step that touches an LLM. It classifies the question and pulls out
# a teacher name / announcement content exactly as written — resolving that
# name to a real teacher, grouping their pending leave days into a range, and
# actually calling the existing overview/teachers/classes/substitutes/
# leave-requests/announcements endpoints all happens in plain frontend code.
# A parsing mistake can only fail to route; it can never fabricate an action.
@router.post("/{schoolId}/ask-intent")
async def admin_ask_intent(schoolId: str, body: AdminAskIntentSchema, request: Request, admin: dict = Depends(require_admin)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    from ..lib.ask_history import history_block
    prompt = f"""A school admin typed this question into their admin portal: "{body.question}"
{history_block(body.history)}

Classify what they are asking, and pull out any name mentioned EXACTLY as written — do not correct spelling, do not expand a nickname, do not invent a full name.

Categories:
- "overview" — school dashboard stats, readiness, how things are going overall
- "teachers" — the teacher list, workload, who teaches what (optionally about one named teacher)
- "classes" — the class list, grade/section info
- "leave_requests" — CHECKING pending leave requests awaiting approval — NOT approving or rejecting one
- "approve_leave" — approving a SPECIFIC named teacher's leave request — an action
- "reject_leave" — rejecting a SPECIFIC named teacher's leave request — an action
- "substitutes" — today's substitute-coverage board, who's out, unresolved gaps
- "announcements" — CHECKING recent announcements — NOT posting a new one
- "post_announcement" — posting/creating a NEW announcement — an action
- "calendar" — upcoming academic calendar events / holidays
- "exam_schedule" — when exams/tests are scheduled (unit tests, mid-terms, finals) — which months, which dates
- "syllabus_status" — how a grade+subject's syllabus is progressing, per section (on track / behind, completion counts)
- "textbook_status" — how textbook ingestion/review is going (chapters processed, published)
- "unknown" — doesn't fit any of the above, or is too vague to act on

Return JSON only, no markdown:
{{"intent": "one of the categories above", "teacherRef": "teacher name as written, for teachers/approve_leave/reject_leave, or null", "announcementTitle": "only for post_announcement — a short title for it, or null", "announcementBody": "only for post_announcement — the full message content as the admin wants it worded, or null", "announcementCategory": "only for post_announcement — one of general, exam, urgent, holiday based on content, default general", "gradeRef": "grade as written, only for syllabus_status/textbook_status, or null", "subjectRef": "subject as written, only for syllabus_status/textbook_status, or null"}}"""

    try:
        result = await call_ai([{"role": "user", "content": prompt}])
        parsed = json.loads(result)
        api_log("admin-ask-intent", ip, (time.time() - t0) * 1000, False, "ok")
        intent = parsed.get("intent")
        if intent not in (
            "overview", "teachers", "classes", "leave_requests", "approve_leave", "reject_leave",
            "substitutes", "announcements", "post_announcement", "calendar", "exam_schedule",
            "syllabus_status", "textbook_status",
        ):
            intent = "unknown"
        category = parsed.get("announcementCategory")
        if category not in ("general", "exam", "urgent", "holiday"):
            category = "general"
        return {
            "intent": intent,
            "teacherRef": parsed.get("teacherRef") or None,
            "announcementTitle": parsed.get("announcementTitle") or None,
            "announcementBody": parsed.get("announcementBody") or None,
            "announcementCategory": category,
            "gradeRef": parsed.get("gradeRef") or None,
            "subjectRef": parsed.get("subjectRef") or None,
        }
    except Exception as e:
        api_log("admin-ask-intent", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {
            "intent": "unknown", "teacherRef": None, "announcementTitle": None,
            "announcementBody": None, "announcementCategory": "general",
            "gradeRef": None, "subjectRef": None,
        }
