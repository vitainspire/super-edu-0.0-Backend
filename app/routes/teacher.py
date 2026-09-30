from typing import Optional
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client, get_anon_client
from ..lib.subject_aliases import subject_matches
from ..lib.lesson_thumbnails import first_lesson_image
from ..lib.admin_queries import (
    fetch_published_academic_events, fetch_school_announcements,
    fetch_teacher_availability_for_teacher, create_pending_leave_request,
    fetch_school_schedule,
)
from ..lib.substitute_automation import apply_teacher_absence, clear_teacher_absence
from ..lib.timetable_resolution import resolve_day_timetable, overlay_substitutions_on_week
from ..lib.notifications import fetch_notifications, mark_notification_read, mark_all_notifications_read
from ..lib.schemas import (
    PeerPairDissolveSchema, TeacherProfileUpsertSchema, TeacherClassUpsertSchema, TeacherAssignmentUpsertSchema,
    SyllabusTopicUpsertSchema, SyllabusSubTopicUpsertSchema, TeacherSessionUpsertSchema,
    TeacherStudentUpsertSchema, TeacherTestUpsertSchema, TeacherMarkUpsertSchema,
    TeacherAttendanceUpsertSchema, TeacherTopicMasteryUpsertSchema,
    TeacherTimetableEntryUpsertSchema, TeacherCatchupMaterialUpsertSchema, TeacherPrepMaterialUpsertSchema,
    TeacherInterventionUpsertSchema, TeacherStudentDoubtUpsertSchema,
    TeacherRecoveryAttemptUpsertSchema, TeacherTaughtTopicUpsertSchema, TeacherLessonFeedbackUpsertSchema,
    PrepSessionTopicUpsertSchema,
)
from ..lib.peer_pair_progress import compute_avg_mastery, compute_progress_status
from ..deps import require_user, require_teacher

router = APIRouter()

def _whole_if_int(x):
    """Postgrest sends whatever json.dumps produces; a Python whole float like
    7.0 renders as "7.0", which Postgres's integer columns reject (unlike JS,
    where JSON.stringify(7) is "7"). Normalize before writes to integer columns.
    Mirrors ai_routes2.py's helper of the same name."""
    if isinstance(x, float) and x == int(x):
        return int(x)
    return x


_PAGE_SIZE = 1000


def _fetch_all_rows(query_factory):
    """Mirrors the frontend's fetchAllPages helper (lib/supabase-queries.ts) —
    PostgREST caps a single response around 1000 rows regardless of caller
    (service role included), so large classes/schools need explicit .range()
    paging or silently truncate. query_factory must return a fresh, unexecuted
    query each call since .range() can't be reapplied to an already-run one."""
    out = []
    start = 0
    while True:
        page = query_factory().range(start, start + _PAGE_SIZE - 1).execute().data or []
        out.extend(page)
        if len(page) < _PAGE_SIZE:
            return out
        start += _PAGE_SIZE


def _bearer_token(request: Request) -> Optional[str]:
    header = request.headers.get("authorization")
    if not header or not header.startswith("Bearer "):
        return None
    return header[len("Bearer ") :].strip() or None


def _soft_resolve_teacher(request: Request, ac) -> Optional[dict]:
    """'Soft' auth used by routes that return an empty/default payload instead
    of a 401 when the caller isn't recognised (matches the Next.js originals)."""
    token = _bearer_token(request)
    if not token:
        return None
    try:
        res = get_anon_client().auth.get_user(token)
    except Exception:
        return None
    if not res.user:
        return None
    row = ac.table("teachers").select("id, school_id").eq("user_id", res.user.id).maybe_single().execute()
    data = row.data if row else None
    if not data or not data.get("school_id"):
        return None
    return {"teacherId": data["id"], "schoolId": data["school_id"]}


def _strict_resolve_teacher(request: Request, ac) -> dict:
    """Hard auth used by routes that need to distinguish 'not logged in' (401)
    from 'logged in but not linked to a school yet' (400) — mirrors the
    Next.js originals' resolveTeacher()."""
    token = _bearer_token(request)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")
    try:
        res = get_anon_client().auth.get_user(token)
    except Exception:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not res.user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    row = ac.table("teachers").select("id, school_id").eq("user_id", res.user.id).maybe_single().execute()
    data = row.data if row else None
    if not data:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not data.get("school_id"):
        raise HTTPException(status_code=400, detail="Your account isn't linked to a school yet — ask your admin to check your teacher record.")
    return {"teacherId": data["id"], "schoolId": data["school_id"]}


# GET /api/teacher/school-data
@router.get("/school-data")
def school_data(request: Request):
    empty = {"classes": [], "students": [], "timetable": [], "assignments": []}
    try:
        ac = create_admin_client()
        teacher = _soft_resolve_teacher(request, ac)
        if not teacher:
            return empty
        teacher_id = teacher["teacherId"]

        asg_rows = ac.table("teacher_class_assignments").select("id, class_id, subject, created_at").eq("teacher_id", teacher_id).execute().data or []
        timetable_rows = ac.table("school_timetable_periods").select("*").eq("teacher_id", teacher_id).execute().data or []

        asg_class_ids = {r["class_id"] for r in asg_rows}
        tt_class_ids = list({r["class_id"] for r in timetable_rows})
        synthetic_assignments = [
            {"id": f"timetable-derived-{cid}", "teacherId": teacher_id, "classId": cid, "subject": None, "createdAt": ""}
            for cid in tt_class_ids
            if cid not in asg_class_ids
        ]

        assignments = [
            {"id": r["id"], "teacherId": teacher_id, "classId": r["class_id"], "subject": r.get("subject"), "createdAt": r.get("created_at") or ""}
            for r in asg_rows
        ] + synthetic_assignments

        if timetable_rows:
            timetable = [
                {
                    "id": f"stp-{r['id']}", "teacherId": r["teacher_id"], "classId": r["class_id"],
                    "dayOfWeek": r["day_of_week"], "periodNumber": r["period_number"],
                    "startTime": r["start_time"], "endTime": r["end_time"], "label": r.get("label"),
                }
                for r in timetable_rows
            ]
        else:
            pub_rows = ac.table("timetable").select("*").eq("teacher_id", teacher_id).execute().data or []
            timetable = [
                {
                    "id": r["id"], "teacherId": r["teacher_id"], "classId": r["class_id"],
                    "dayOfWeek": r["day_of_week"], "periodNumber": r["period_number"],
                    "startTime": r["start_time"], "endTime": r["end_time"], "label": r.get("label"),
                }
                for r in pub_rows
            ]

        own_class_rows = ac.table("classes").select("id").eq("teacher_id", teacher_id).execute().data or []

        # Today's coverage folded into the weekly plan: periods this teacher has
        # handed over are tagged "covered_away", periods they've picked up are
        # appended as "covering". Everything else is tagged "regular" so the
        # frontend switches on one field. Best-effort — a failure here must not
        # cost the teacher their whole timetable, so the base plan stands.
        today_str = datetime.now(timezone.utc).date().isoformat()
        try:
            timetable = overlay_substitutions_on_week(timetable, teacher_id, today_str, ac)
        except Exception as e:
            print(f"[teacher/school-data] substitution overlay failed, serving base timetable: {e}")

        # Only subject-matched coverage (status="assigned") widens class access.
        # Every cover the automation now produces IS subject-matched — see
        # mark_teacher_unavailable, which leaves a period unresolved rather than
        # drafting in someone who can't teach it. The filter still matters for
        # the two cases that bypass it: pre-existing "assigned_fallback" rows,
        # and "manual" from an admin hand-picking a substitute. Those see the
        # period on their timetable and nothing behind it.
        covering_rows = (
            ac.table("timetable_substitutions").select("class_id")
            .eq("substitute_teacher_id", teacher_id).eq("date", today_str).eq("status", "assigned")
            .execute().data or []
        )

        class_id_set = (
            {r["class_id"] for r in asg_rows}
            | {r["class_id"] for r in timetable_rows}
            | {r["id"] for r in own_class_rows}
            | {r["class_id"] for r in covering_rows}
        )
        class_ids = list(class_id_set)
        if not class_ids:
            return {**empty, "timetable": timetable, "assignments": assignments}

        class_rows = ac.table("classes").select("*").in_("id", class_ids).execute().data or []
        student_rows = ac.table("students").select("*").in_("class_id", class_ids).order("roll_number").execute().data or []

        classes = [
            {
                "id": r["id"], "teacherId": r.get("teacher_id"), "schoolName": r.get("school_name") or "",
                "schoolId": r.get("school_id"), "name": r["name"], "grade": r["grade"],
                "section": r.get("section") or "", "academicYear": r.get("academic_year") or "",
                "createdAt": r.get("created_at") or "", "classCode": r.get("class_code"),
            }
            for r in class_rows
        ]
        students = [
            {
                "id": r["id"], "teacherId": r.get("teacher_id"), "classId": r.get("class_id") or "",
                "name": r["name"], "rollNumber": r["roll_number"], "isActive": r["is_active"],
                "interests": r.get("interests") or [], "goal": r.get("goal") or "",
                "pin": r.get("pin"), "studentCode": r.get("student_code"),
            }
            for r in student_rows
        ]
        return {"classes": classes, "students": students, "timetable": timetable, "assignments": assignments}
    except Exception as e:
        print(f"[teacher/school-data] failed: {e}")
        return empty


def _teacher_row_to_profile(data: dict) -> dict:
    return {
        "id": data["id"], "userId": data.get("user_id") or data["id"],
        "name": data.get("name") or "", "schoolName": data.get("school_name") or "",
        "schoolId": data.get("school_id"), "subject": data.get("subject") or "",
        "grade": data.get("grade") or "", "phone": data.get("phone") or "",
        "languagePreference": data.get("language_preference") or "english",
        "academicYearStart": data.get("academic_year_start"), "currentTerm": data.get("current_term"),
        "teacherCode": data.get("teacher_code"), "teachingProfile": data.get("teaching_profile"),
    }


# GET /api/teacher/profile — own profile row, keyed by the authenticated user.
@router.get("/profile")
def get_own_profile(user: dict = Depends(require_user)):
    ac = create_admin_client()
    row = ac.table("teachers").select("*").eq("id", user["id"]).maybe_single().execute()
    data = row.data if row else None
    if not data:
        raise HTTPException(status_code=404, detail="No teacher profile found")
    return _teacher_row_to_profile(data)


# POST /api/teacher/profile — create or update own profile. `id`/`userId` are
# never taken from the body — they're always the authenticated user's own id,
# so a teacher can only ever write their own row (mirrors signUp's convention
# that teachers.id == the Supabase Auth user id).
@router.post("/profile")
def upsert_own_profile(body: TeacherProfileUpsertSchema, user: dict = Depends(require_user)):
    ac = create_admin_client()
    ac.table("teachers").upsert({
        "id": user["id"], "user_id": user["id"], "name": body.name,
        "school_name": body.schoolName, "school_id": body.schoolId,
        "subject": body.subject, "grade": body.grade, "phone": body.phone,
        "language_preference": body.languagePreference,
        "academic_year_start": body.academicYearStart, "current_term": body.currentTerm,
        "teacher_code": body.teacherCode, "teaching_profile": body.teachingProfile,
    }).execute()
    return {"ok": True}


def _class_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r.get("teacher_id"), "schoolName": r.get("school_name") or "",
        "schoolId": r.get("school_id"), "name": r["name"], "grade": r["grade"],
        "section": r.get("section") or "", "academicYear": r.get("academic_year") or "",
        "createdAt": r.get("created_at") or "", "classCode": r.get("class_code"),
    }


# GET /api/teacher/classes — own classes + admin-assigned classes, service-role.
# Replaces the old RLS-scoped direct-Supabase fetchClasses: since service role
# bypasses RLS entirely, this never needs a separate schoolData gap-fill merge
# the way the old RLS-scoped fetch did.
@router.get("/classes")
def get_own_classes(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    own_rows = ac.table("classes").select("*").eq("teacher_id", teacher_id).order("created_at").execute().data or []
    own_ids = {r["id"] for r in own_rows}

    asg_rows = ac.table("teacher_class_assignments").select("class_id").eq("teacher_id", teacher_id).execute().data or []
    extra_ids = [r["class_id"] for r in asg_rows if r["class_id"] not in own_ids]
    extra_rows = (
        ac.table("classes").select("*").in_("id", extra_ids).order("created_at").execute().data or []
        if extra_ids else []
    )

    return {"classes": [_class_row_to_dto(r) for r in [*own_rows, *extra_rows]]}


# POST /api/teacher/classes — create or update a class owned by this teacher.
# teacher_id is always the resolved teacher, never client-supplied, for the
# same reason as /profile above — this also matches the RLS behavior it
# replaces, where a teacher could only ever successfully write their own rows.
@router.post("/classes")
def upsert_own_class(body: TeacherClassUpsertSchema, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("classes").upsert({
        "id": body.id, "teacher_id": teacher_id, "school_name": body.schoolName,
        "school_id": body.schoolId, "name": body.name, "grade": body.grade,
        "section": body.section, "academic_year": body.academicYear,
        "created_at": body.createdAt, "class_code": body.classCode,
    }).execute()
    return {"ok": True}


# DELETE /api/teacher/classes/{class_id} — shallow delete (no cascade to that
# class's students/tests/etc, matching the original's behavior exactly),
# restricted to classes this teacher owns — mirrors the RLS check the old
# direct-Supabase delete relied on (it silently no-opped for classes owned by
# someone else; this does the same, just as an explicit ownership check).
@router.delete("/classes/{class_id}")
def delete_own_class(class_id: str, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("classes").delete().eq("id", class_id).eq("teacher_id", teacher_id).execute()
    return {"ok": True}


# POST /api/teacher/clear-all-data — full account wipe (used by "delete my
# account"). Ports the original's 14-table parallel delete, but resolves the
# owned class/test/student id sets fresh from the DB instead of trusting the
# client's in-memory React state, which could be stale or partially loaded.
@router.post("/clear-all-data")
def clear_all_own_data(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    tid = teacher_id

    own_classes = ac.table("classes").select("id").eq("teacher_id", tid).execute().data or []
    owned_class_ids = [c["id"] for c in own_classes]
    own_tests = ac.table("tests").select("id").eq("teacher_id", tid).execute().data or []
    test_ids = [t["id"] for t in own_tests]
    owned_students = (
        ac.table("students").select("id").in_("class_id", owned_class_ids).execute().data or []
        if owned_class_ids else []
    )
    owned_student_ids = [s["id"] for s in owned_students]

    def _safe(fn):
        try:
            fn()
        except Exception as e:
            print(f"[teacher/clear-all-data] one delete failed (continuing): {e}")

    # Dependents first, classes last: classes.id is an FK target for students
    # (ON DELETE SET NULL, discovered live — deleting classes first orphans
    # students with a null class_id instead of removing them, since the
    # follow-up "students in owned_class_ids" delete then matches nothing).
    # The original client-side version fired all 14 deletes in one
    # Promise.all with no ordering guarantee, so it carried this same race
    # silently; doing these sequentially server-side is the chance to fix it
    # instead of just relocating the bug.
    _safe(lambda: test_ids and ac.table("marks").delete().in_("test_id", test_ids).execute())
    _safe(lambda: owned_class_ids and ac.table("attendance").delete().in_("class_id", owned_class_ids).execute())
    _safe(lambda: owned_student_ids and ac.table("student_topic_mastery").delete().in_("student_id", owned_student_ids).execute())
    _safe(lambda: ac.table("sessions").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("syllabus_topics").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("syllabus_sub_topics").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("timetable").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("catchup_materials").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("prep_materials").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("taught_topics").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("teacher_class_assignments").delete().eq("teacher_id", tid).execute())
    _safe(lambda: ac.table("tests").delete().eq("teacher_id", tid).execute())
    _safe(lambda: owned_class_ids and ac.table("students").delete().in_("class_id", owned_class_ids).execute())
    _safe(lambda: ac.table("classes").delete().eq("teacher_id", tid).execute())

    return {"ok": True}


# POST /api/teacher/assignments — self-assign to an (typically admin-owned) class.
@router.post("/assignments")
def upsert_own_assignment(body: TeacherAssignmentUpsertSchema, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("teacher_class_assignments").upsert({
        "id": body.id, "teacher_id": teacher_id, "class_id": body.classId,
        "subject": body.subject, "created_at": body.createdAt,
    }, on_conflict="teacher_id,class_id").execute()
    return {"ok": True}


# DELETE /api/teacher/assignments?classId=... — self-unassign from a class.
@router.delete("/assignments")
def delete_own_assignment(classId: str, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("teacher_class_assignments").delete().eq("teacher_id", teacher_id).eq("class_id", classId).execute()
    return {"ok": True}


def _syllabus_topic_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "classId": r["class_id"], "teacherId": r.get("teacher_id"),
        "grade": r.get("grade"), "subject": r.get("subject"), "definitionId": r.get("definition_id"),
        "topic": r["topic"], "description": r.get("description") or "",
        "weekNumber": r.get("week_number"), "orderIndex": r.get("order_index") or 0,
        "isCompleted": r.get("is_completed") or False, "createdAt": r.get("created_at") or "",
        "estimatedSessions": r.get("estimated_sessions"),
        "prerequisiteDefinitionId": r.get("prerequisite_definition_id"),
    }


# GET /api/teacher/syllabus-topics?classIds=a,b,c — scoped by class only, not
# author, matching the direct-Supabase original: admin-authored syllabus rows
# have no teacher_id a per-teacher filter could ever match.
@router.get("/syllabus-topics")
def get_syllabus_topics(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    if not ids:
        return {"topics": []}
    ac = create_admin_client()
    rows = ac.table("syllabus_topics").select("*").in_("class_id", ids).order("order_index").execute().data or []
    return {"topics": [_syllabus_topic_row_to_dto(r) for r in rows]}


# POST /api/teacher/syllabus-topics
@router.post("/syllabus-topics")
def upsert_syllabus_topic(body: SyllabusTopicUpsertSchema, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("syllabus_topics").upsert({
        "id": body.id, "class_id": body.classId, "teacher_id": body.teacherId,
        "grade": body.grade, "subject": body.subject, "definition_id": body.definitionId,
        "topic": body.topic, "description": body.description,
        "week_number": body.weekNumber, "order_index": body.orderIndex,
        "is_completed": body.isCompleted, "created_at": body.createdAt,
        "estimated_sessions": body.estimatedSessions,
        "prerequisite_definition_id": body.prerequisiteDefinitionId,
    }).execute()
    return {"ok": True}


# DELETE /api/teacher/syllabus-topics?ids=a,b,c
@router.delete("/syllabus-topics")
def delete_syllabus_topics(ids: str = "", teacher_id: str = Depends(require_teacher)):
    id_list = [i for i in ids.split(",") if i]
    if id_list:
        create_admin_client().table("syllabus_topics").delete().in_("id", id_list).execute()
    return {"ok": True}


def _sub_topic_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "topicId": r["topic_id"], "classId": r["class_id"],
        "teacherId": r.get("teacher_id"), "definitionId": r.get("definition_id"),
        "name": r["name"], "description": r.get("description"), "orderIndex": r.get("order_index") or 0,
        "isCompleted": r.get("is_completed") or False, "completedAt": r.get("completed_at"),
        "createdAt": r.get("created_at") or "", "estimatedSessions": r.get("estimated_sessions"),
    }


# GET /api/teacher/syllabus-sub-topics?classIds=a,b,c — same class-only scoping as topics.
@router.get("/syllabus-sub-topics")
def get_syllabus_sub_topics(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    if not ids:
        return {"subTopics": []}
    try:
        ac = create_admin_client()
        rows = ac.table("syllabus_sub_topics").select("*").in_("class_id", ids).order("order_index").execute().data or []
        return {"subTopics": [_sub_topic_row_to_dto(r) for r in rows]}
    except Exception:
        return {"subTopics": []}


# POST /api/teacher/syllabus-sub-topics
@router.post("/syllabus-sub-topics")
def upsert_syllabus_sub_topic(body: SyllabusSubTopicUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        ac.table("syllabus_sub_topics").upsert({
            "id": body.id, "topic_id": body.topicId, "class_id": body.classId,
            "teacher_id": body.teacherId, "definition_id": body.definitionId,
            "name": body.name, "description": body.description, "order_index": body.orderIndex,
            "is_completed": body.isCompleted, "completed_at": body.completedAt, "created_at": body.createdAt,
            "estimated_sessions": body.estimatedSessions,
        }).execute()
    except Exception:
        pass  # table may not exist yet — matches the original's defensive try/catch
    return {"ok": True}


# DELETE /api/teacher/syllabus-sub-topics?ids=a,b,c
@router.delete("/syllabus-sub-topics")
def delete_syllabus_sub_topics(ids: str = "", teacher_id: str = Depends(require_teacher)):
    id_list = [i for i in ids.split(",") if i]
    if id_list:
        try:
            create_admin_client().table("syllabus_sub_topics").delete().in_("id", id_list).execute()
        except Exception:
            pass
    return {"ok": True}


def _session_row_to_dto(r: dict) -> dict:
    snapshot = r.get("lesson_snapshot")
    if isinstance(snapshot, str):
        import json
        try:
            snapshot = json.loads(snapshot)
        except Exception:
            snapshot = None
    return {
        "id": r["id"], "classId": r["class_id"], "teacherId": r["teacher_id"],
        "syllabusTopicId": r.get("syllabus_topic_id"), "topic": r["topic"],
        "date": r["date"], "createdAt": r.get("created_at") or "",
        "sessionNote": r.get("session_note"), "lessonSnapshot": snapshot,
    }


# GET /api/teacher/sessions — own sessions only (this table IS teacher-owned,
# unlike syllabus above).
@router.get("/sessions")
def get_own_sessions(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    rows = ac.table("sessions").select("*").eq("teacher_id", teacher_id).order("date", desc=True).execute().data or []
    return {"sessions": [_session_row_to_dto(r) for r in rows]}


# POST /api/teacher/sessions
@router.post("/sessions")
def upsert_own_session(body: TeacherSessionUpsertSchema, teacher_id: str = Depends(require_teacher)):
    import json
    ac = create_admin_client()
    ac.table("sessions").upsert({
        "id": body.id, "class_id": body.classId, "teacher_id": teacher_id,
        "syllabus_topic_id": body.syllabusTopicId, "topic": body.topic,
        "date": body.date, "created_at": body.createdAt,
        "session_note": body.sessionNote,
        "lesson_snapshot": json.dumps(body.lessonSnapshot) if body.lessonSnapshot else None,
    }, on_conflict="id").execute()
    return {"ok": True}


def _student_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r.get("teacher_id"), "classId": r.get("class_id") or "",
        "name": r["name"], "rollNumber": r["roll_number"], "isActive": r["is_active"],
        "interests": r.get("interests") or [], "goal": r.get("goal") or "",
        "pin": r.get("pin"), "studentCode": r.get("student_code"),
    }


def _teachers_own_and_assigned_class_ids(ac, teacher_id: str) -> set:
    own = ac.table("classes").select("id").eq("teacher_id", teacher_id).execute().data or []
    asg = ac.table("teacher_class_assignments").select("class_id").eq("teacher_id", teacher_id).execute().data or []
    return {r["id"] for r in own} | {r["class_id"] for r in asg}


# GET /api/teacher/students?classIds=a,b,c — service-role, no RLS gap for
# admin-created students in admin-owned classes (unlike the old direct fetch).
@router.get("/students")
def get_students_by_classes(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    if not ids:
        return {"students": []}
    ac = create_admin_client()
    rows = _fetch_all_rows(lambda: ac.table("students").select("*").in_("class_id", ids).order("roll_number"))
    return {"students": [_student_row_to_dto(r) for r in rows]}


# POST /api/teacher/students — classId must be one of this teacher's own or
# assigned classes (a genuine new check — the old RLS gate silently blocked
# writes to admin-owned classes' students the same way it blocked reads).
@router.post("/students")
def upsert_student(body: TeacherStudentUpsertSchema, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    if body.classId not in _teachers_own_and_assigned_class_ids(ac, teacher_id):
        raise HTTPException(status_code=403, detail="Not your class")
    ac.table("students").upsert({
        "id": body.id, "teacher_id": teacher_id, "class_id": body.classId, "name": body.name,
        "roll_number": body.rollNumber, "is_active": body.isActive, "interests": body.interests,
        "goal": body.goal, "pin": body.pin, "student_code": body.studentCode,
    }).execute()
    return {"ok": True}


def _test_row_to_dto(r: dict) -> dict:
    questions = r.get("questions")
    if isinstance(questions, str):
        import json
        try:
            questions = json.loads(questions)
        except Exception:
            questions = None
    return {
        "id": r["id"], "teacherId": r["teacher_id"], "classId": r.get("class_id"),
        "subject": r["subject"], "topic": r["topic"], "totalMarks": r["total_marks"],
        "conductedOn": r["conducted_on"], "term": r.get("term"), "questions": questions,
    }


# GET /api/teacher/tests — own tests only (teacher-authored, no RLS-gap pattern here).
@router.get("/tests")
def get_own_tests(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    rows = ac.table("tests").select("*").eq("teacher_id", teacher_id).order("conducted_on", desc=True).execute().data or []
    return {"tests": [_test_row_to_dto(r) for r in rows]}


# POST /api/teacher/tests
@router.post("/tests")
def upsert_own_test(body: TeacherTestUpsertSchema, teacher_id: str = Depends(require_teacher)):
    import json
    ac = create_admin_client()
    ac.table("tests").upsert({
        "id": body.id, "teacher_id": teacher_id, "class_id": body.classId,
        "subject": body.subject, "topic": body.topic, "total_marks": _whole_if_int(body.totalMarks),
        "conducted_on": body.conductedOn, "term": body.term,
        "questions": json.dumps(body.questions) if body.questions is not None else None,
    }).execute()
    return {"ok": True}


def _mark_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "testId": r["test_id"], "studentId": r["student_id"], "score": r["score"],
        "feedback": r.get("feedback"), "breakdown": r.get("breakdown"), "enteredAt": r["entered_at"],
        "source": r.get("source"), "imageUrl": r.get("image_url"), "driveUrl": r.get("drive_url"),
    }


# GET /api/teacher/marks — all marks for this teacher's own tests, paginated.
@router.get("/marks")
def get_own_marks(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    tests = ac.table("tests").select("id").eq("teacher_id", teacher_id).execute().data or []
    test_ids = [t["id"] for t in tests]
    if not test_ids:
        return {"marks": []}
    rows = _fetch_all_rows(lambda: ac.table("marks").select("*").in_("test_id", test_ids))
    return {"marks": [_mark_row_to_dto(r) for r in rows]}


# POST /api/teacher/marks
@router.post("/marks")
def upsert_mark(body: TeacherMarkUpsertSchema, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("marks").upsert({
        "id": body.id, "test_id": body.testId, "student_id": body.studentId, "score": _whole_if_int(body.score),
        "feedback": body.feedback, "breakdown": body.breakdown, "entered_at": body.enteredAt,
        "source": body.source or "manual", "image_url": body.imageUrl, "drive_url": body.driveUrl,
    }).execute()
    return {"ok": True}


def _attendance_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "sessionId": r.get("session_id") or "", "studentId": r["student_id"],
        "classId": r.get("class_id") or "", "syllabusTopicId": r.get("syllabus_topic_id") or "",
        "date": r["date"], "status": r["status"],
    }


# GET /api/teacher/attendance?classIds=a,b,c — paginated (this table's 1000-row
# cap caused a real reported bug: a class attendance rate showing 34% instead
# of 93% before the frontend added pagination — same fix, ported).
@router.get("/attendance")
def get_attendance_by_classes(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    if not ids:
        return {"attendance": []}
    ac = create_admin_client()
    rows = _fetch_all_rows(lambda: ac.table("attendance").select("*").in_("class_id", ids).order("date", desc=True))
    return {"attendance": [_attendance_row_to_dto(r) for r in rows]}


# POST /api/teacher/attendance
@router.post("/attendance")
def upsert_attendance(body: TeacherAttendanceUpsertSchema, teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    ac.table("attendance").upsert({
        "id": body.id, "session_id": body.sessionId, "student_id": body.studentId,
        "class_id": body.classId, "syllabus_topic_id": body.syllabusTopicId,
        "date": body.date, "status": body.status,
    }).execute()
    return {"ok": True}


# DELETE /api/teacher/attendance?sessionId=...
@router.delete("/attendance")
def delete_attendance_by_session(sessionId: str, teacher_id: str = Depends(require_teacher)):
    create_admin_client().table("attendance").delete().eq("session_id", sessionId).execute()
    return {"ok": True}


def _mastery_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "studentId": r["student_id"], "topic": r["topic"], "subject": r["subject"],
        "mastery": r["mastery"], "attempts": r["attempts"], "lastUpdated": r["last_updated"],
    }


# GET /api/teacher/topic-mastery?classIds=a,b,c — resolves students for those
# classes first (service-role, same no-RLS-gap benefit as /students), then
# paginates mastery rows for them.
@router.get("/topic-mastery")
def get_topic_mastery(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    ac = create_admin_client()
    if ids:
        students = ac.table("students").select("id").in_("class_id", ids).execute().data or []
    else:
        students = ac.table("students").select("id").eq("teacher_id", teacher_id).execute().data or []
    student_ids = [s["id"] for s in students]
    if not student_ids:
        return {"mastery": []}
    rows = _fetch_all_rows(lambda: ac.table("student_topic_mastery").select("*").in_("student_id", student_ids))
    return {"mastery": [_mastery_row_to_dto(r) for r in rows]}


# POST /api/teacher/topic-mastery
@router.post("/topic-mastery")
def upsert_topic_mastery(body: TeacherTopicMasteryUpsertSchema, teacher_id: str = Depends(require_teacher)):
    create_admin_client().table("student_topic_mastery").upsert({
        "id": body.id, "student_id": body.studentId, "topic": body.topic, "subject": body.subject,
        "mastery": _whole_if_int(body.mastery), "attempts": body.attempts, "last_updated": body.lastUpdated,
    }, on_conflict="student_id,topic").execute()
    return {"ok": True}


def _timetable_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r["teacher_id"], "classId": r["class_id"],
        "dayOfWeek": r["day_of_week"], "periodNumber": r["period_number"],
        "startTime": r["start_time"], "endTime": r["end_time"], "label": r.get("label"),
    }


# GET /api/teacher/timetable — own entries only, the recurring weekly plan with
# no per-date adjustments. For what the teacher should actually turn up to on a
# given day, use /timetable/day.
@router.get("/timetable")
def get_own_timetable(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("timetable").select("*").eq("teacher_id", teacher_id).execute().data or []
        return {"timetable": [_timetable_row_to_dto(r) for r in rows]}
    except Exception:
        return {"timetable": []}


# GET /api/teacher/timetable/day?date=YYYY-MM-DD (defaults to today)
# The teacher's schedule for one date with substitutions already applied:
# periods handed to someone else are marked, periods picked up from an absent
# colleague are included. This is the readjusted day, not the weekly plan.
@router.get("/timetable/day")
def get_resolved_day(request: Request, date: Optional[str] = None):
    try:
        ac = create_admin_client()
        teacher = _soft_resolve_teacher(request, ac)
        date = date or datetime.now(timezone.utc).date().isoformat()
        if not teacher:
            return {"date": date, "dayOfWeek": None, "onLeave": False, "reason": None, "entries": []}
        return resolve_day_timetable(teacher["teacherId"], date, ac)
    except Exception as e:
        print(f"[teacher/timetable/day] failed: {e}")
        return {"date": date, "dayOfWeek": None, "onLeave": False, "reason": None, "entries": []}


# POST /api/teacher/timetable — note: admin backend also generates/manages
# timetable server-side (admin_timetable.py). This preserves the existing
# teacher-portal write path as-is; it doesn't resolve the ownership question
# of which side's writes should win for the same slot.
@router.post("/timetable")
def upsert_own_timetable_entry(body: TeacherTimetableEntryUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("timetable").upsert({
            "id": body.id, "teacher_id": teacher_id, "class_id": body.classId,
            "day_of_week": body.dayOfWeek, "period_number": body.periodNumber,
            "start_time": body.startTime, "end_time": body.endTime, "label": body.label,
        }).execute()
    except Exception:
        pass
    return {"ok": True}


# DELETE /api/teacher/timetable?id=...
@router.delete("/timetable")
def delete_own_timetable_entry(id: str, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("timetable").delete().eq("id", id).execute()
    except Exception:
        pass
    return {"ok": True}


def _catchup_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r["teacher_id"], "studentId": r["student_id"],
        "studentName": r.get("student_name") or "", "topic": r["topic"], "subject": r["subject"],
        "grade": r["grade"], "explanation": r["explanation"],
        "practiceQuestions": r.get("practice_questions") or [],
        "activity": r.get("activity"), "focusNote": r.get("focus_note"),
        "status": r["status"], "createdAt": r["created_at"], "reason": r.get("reason"),
    }


# GET /api/teacher/catchup-materials
@router.get("/catchup-materials")
def get_own_catchup_materials(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("catchup_materials").select("*").eq("teacher_id", teacher_id).execute().data or []
        return {"catchupMaterials": [_catchup_row_to_dto(r) for r in rows]}
    except Exception:
        return {"catchupMaterials": []}


# POST /api/teacher/catchup-materials
@router.post("/catchup-materials")
def upsert_own_catchup_material(body: TeacherCatchupMaterialUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("catchup_materials").upsert({
            "id": body.id, "teacher_id": teacher_id, "student_id": body.studentId,
            "student_name": body.studentName, "topic": body.topic, "subject": body.subject,
            "grade": body.grade, "explanation": body.explanation,
            "practice_questions": body.practiceQuestions, "activity": body.activity,
            "focus_note": body.focusNote, "status": body.status, "created_at": body.createdAt,
            "reason": body.reason,
        }).execute()
    except Exception:
        pass
    return {"ok": True}


# PATCH /api/teacher/catchup-materials/{id}/status
@router.patch("/catchup-materials/{id}/status")
def update_catchup_status(id: str, status: str, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("catchup_materials").update({"status": status}).eq("id", id).execute()
    except Exception:
        pass
    return {"ok": True}


def _prep_material_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r["teacher_id"], "classId": r["class_id"],
        "subject": r["subject"], "grade": r["grade"], "topic": r["topic"],
        "subtopic": r.get("subtopic"), "gapTopics": r.get("gap_topics") or [],
        "lesson": r["lesson"], "createdAt": r["created_at"], "source": r.get("source") or "shared",
    }


# GET /api/teacher/prep-materials — capped to the most recent rows, newest
# first. Some saved lessons carry an embedded diagram image inline in the
# lesson JSON (several MB each); pulling a teacher's entire history
# unbounded eventually exceeds the DB statement timeout as it accumulates,
# and every caller here (getPrepMaterial's newestFor) already wants the
# newest match for a topic anyway, so trimming the tail is safe.
@router.get("/prep-materials")
def get_own_prep_materials(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = (
            ac.table("prep_materials").select("*").eq("teacher_id", teacher_id)
            .order("created_at", desc=True).limit(25).execute().data or []
        )
        return {"prepMaterials": [_prep_material_row_to_dto(r) for r in rows]}
    except Exception as e:
        print(f"[teacher/prep-materials] fetch failed: {e}")
        return {"prepMaterials": []}


# POST /api/teacher/prep-materials — best-effort like the original: a save
# failure shouldn't block the teacher from seeing the lesson they already
# generated, but it's still logged, not swallowed silently.
@router.post("/prep-materials")
def upsert_own_prep_material(body: TeacherPrepMaterialUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("prep_materials").upsert({
            "id": body.id, "teacher_id": teacher_id, "class_id": body.classId,
            "subject": body.subject, "grade": body.grade, "topic": body.topic,
            "subtopic": body.subtopic, "lesson": body.lesson, "created_at": body.createdAt,
            "source": body.source or "shared",
        }).execute()
    except Exception as e:
        print(f"[teacher/prep-materials] upsert failed: {e}")
    return {"ok": True}


# GET /api/teacher/prep-materials/generation-mode?classId=&subject= — a
# read-only check of which mode this class's grade+subject is currently in.
# Deliberately does NOT call recompute_grade_subject_mode (that's an admin-
# facing write path) — a teacher's fetch should never trigger a recompute,
# only read whatever is currently in effect.
@router.get("/prep-materials/generation-mode")
def get_own_generation_mode(classId: str, subject: str, teacher_id: str = Depends(require_teacher)):
    from ..lib.generation_mode import get_grade_subject_mode
    try:
        ac = create_admin_client()
        class_res = ac.table("classes").select("school_id, grade").eq("id", classId).maybe_single().execute()
        class_row = class_res.data if class_res else None
        if not class_row:
            return {"mode": "opt_in"}
        mode = get_grade_subject_mode(ac, class_row["school_id"], class_row["grade"], subject)
        return {"mode": mode}
    except Exception as e:
        print(f"[teacher/prep-materials/generation-mode] failed: {e}")
        return {"mode": "opt_in"}


# GET /api/teacher/prep-materials/shared — fetch from the shared (school,
# grade, subject) stock instead of generating live: the syllabus is already
# authored once per grade+subject, not per class, so every section teaching
# it (5A, 5B, ...) reads the same generated lesson. Side-effect-free — the
# frontend calls ensure-stock separately to keep the stock topped up, so a
# fetch never blocks on a generation.
@router.get("/prep-materials/shared")
def get_shared_prep_material(
    classId: str, subject: str, grade: str, topic: str,
    subtopic: Optional[str] = None, topicDefinitionId: Optional[str] = None,
    teacher_id: str = Depends(require_teacher),
):
    try:
        ac = create_admin_client()
        class_res = ac.table("classes").select("school_id").eq("id", classId).maybe_single().execute()
        class_row = class_res.data if class_res else None
        school_id = (class_row or {}).get("school_id")
        if not school_id:
            return {"lesson": None}

        # topicDefinitionId, when given, is already an exact unique key -- no
        # subject filtering on that path. The topic-name fallback has no such
        # key, so it widens to subject_matches() instead of .eq("subject",
        # ...): a period labelled "Science" must still find a lesson saved
        # under "Environmental Studies" (subject_aliases.py).
        q = (
            ac.table("shared_prep_materials").select("lesson, subtopic, subject")
            .eq("school_id", school_id).eq("grade", grade)
        )
        if topicDefinitionId:
            rows = q.eq("topic_definition_id", topicDefinitionId).execute().data or []
        else:
            rows = [r for r in (q.ilike("topic", topic).execute().data or [])
                    if subject_matches(r.get("subject"), subject)]
        if not rows:
            return {"lesson": None}
        want_sub = (subtopic or "").strip().lower()
        exact = next((r for r in rows if (r.get("subtopic") or "").strip().lower() == want_sub), None)
        return {"lesson": (exact or rows[0]).get("lesson")}
    except Exception as e:
        print(f"[teacher/prep-materials/shared] fetch failed: {e}")
        return {"lesson": None}


# GET /api/teacher/prep-materials/my-grades — every grade this teacher
# teaches, with the subject(s) they teach in it, for "Browse My Library" (the
# teacher-side counterpart to the admin's "Browse Shared Library").
#
# Collapsed by grade, NOT by class/section -- a first version of this listed
# each class separately ("Grade 5 A", "Grade 5 B"), but shared material is
# keyed by school+grade+subject only, never by section (see
# shared_prep_materials's own schema): every section of the same grade reads
# the exact same content. Listing sections as separate options was actively
# misleading, not just redundant -- it implied a real choice where there
# wasn't one. Reuses _my_class_ids (below) for "which classes" and
# teacher_class_assignments.subject for "which subject in each" -- the
# authoritative source, NOT teachers.subject/grade, which admin_queries.py's
# own docstring notes is "never actually set". A grade with no subject on
# record in any of its sections is silently skipped -- there is nothing to
# look up a shared lesson by without one.
@router.get("/prep-materials/my-grades")
def list_my_grades(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    class_ids = _my_class_ids(ac, teacher_id)
    if not class_ids:
        return {"grades": []}

    class_rows = ac.table("classes").select("id, grade").in_("id", class_ids).execute().data or []
    grade_by_class = {c["id"]: c["grade"] for c in class_rows}

    asg_rows = (
        ac.table("teacher_class_assignments").select("class_id, subject")
        .eq("teacher_id", teacher_id).execute()
    ).data or []
    subjects_by_grade: dict[str, set[str]] = {}
    for r in asg_rows:
        grade = grade_by_class.get(r["class_id"])
        if grade and r.get("subject"):
            subjects_by_grade.setdefault(grade, set()).add(r["subject"])

    grades = [{"grade": g, "subjects": sorted(subs)} for g, subs in subjects_by_grade.items()]
    grades.sort(key=lambda x: x["grade"])
    return {"grades": grades}


def _my_school_id(ac, teacher_id: str) -> Optional[str]:
    """This teacher's school, via whichever of their own classes has one set
    -- teacher.py has no Depends() that injects school_id directly (see
    require_teacher's own docstring), and every one of a teacher's classes is
    at the same school in practice, so the first hit is sufficient."""
    class_ids = _my_class_ids(ac, teacher_id)
    if not class_ids:
        return None
    rows = ac.table("classes").select("school_id").in_("id", class_ids).execute().data or []
    return next((r["school_id"] for r in rows if r.get("school_id")), None)


# GET /api/teacher/prep-materials/shared-topics — list side of Browse My
# Library, paired with the detail route right below. Same list/detail split
# as GET /prep-materials/shared above (that one wants a specific topic
# already in mind; this is for when the teacher doesn't and wants to see
# what's there). Also the same split the admin panel's own shared-topics
# endpoints use (admin_misc.py) -- kept as teacher.py's own version rather
# than reusing that route directly, since this one derives school_id from the
# teacher's own classes instead of taking a schoolId path param, and is
# scoped to a teacher, not gated by require_admin.
#
# `title`/`rawTopic` split: the saved `topic` string is sometimes a real
# technical description of what's taught, and sometimes just the textbook's
# own heading/caption copied as the topic name (Node 1 doesn't always rename
# it) -- e.g. "AGRICULTURE - CROPS 2" as a topic name tells a teacher nothing
# about what that period actually covers. `lesson.objective` is ALWAYS the
# real technical description, confirmed against every topic in this school's
# own saved data, so it's shown as the title; the original `topic` is kept as
# `rawTopic` for reference, but only when it actually differs -- otherwise
# the same text would show twice.
@router.get("/prep-materials/shared-topics")
def list_my_shared_topics(grade: str, subject: str, teacher_id: str = Depends(require_teacher)):
    # subject_matches(), not .eq() -- a teacher assigned "Science" must also
    # see material saved under "Environmental Studies" (subject_aliases.py).
    ac = create_admin_client()
    school_id = _my_school_id(ac, teacher_id)
    if not school_id:
        return {"chapters": []}
    rows = (
        ac.table("shared_prep_materials")
        .select("topic_definition_id, topic, subtopic, order_index, subject, lesson, chapter_title")
        .eq("school_id", school_id).eq("grade", grade)
        .order("order_index").execute()
    ).data or []

    # Grouped by chapter_title (migration 024) -- a topic saved before that
    # migration, or by the older non-chapter engine, has none, and groups
    # under the null bucket instead of being dropped or mis-sorted into a
    # chapter it was never actually part of. Group order is "first topic
    # encountered" (order_index order, since rows are already sorted by it),
    # not alphabetical -- that keeps a chapter's own topics reading in their
    # real teaching order across chapters too. The null bucket sorts last
    # regardless of where its topics first appear, since "ungrouped" reads
    # better as a catch-all at the end than interleaved with real chapters.
    groups: dict[Optional[str], list[dict]] = {}
    for r in rows:
        if not subject_matches(r.get("subject"), subject):
            continue
        lesson = r.get("lesson") or {}
        raw_topic = r["topic"]
        title = (lesson.get("objective") or raw_topic or "").strip() or raw_topic
        groups.setdefault(r.get("chapter_title"), []).append({
            "topicDefinitionId": r["topic_definition_id"],
            "title": title,
            "rawTopic": raw_topic if raw_topic.strip().lower() != title.strip().lower() else None,
            "subtopic": r.get("subtopic"),
            "orderIndex": r.get("order_index"),
            "thumbnailUrl": first_lesson_image(lesson),
        })

    chapters = [
        {"chapterTitle": chapter_title, "topics": topics}
        for chapter_title, topics in groups.items() if chapter_title is not None
    ]
    if None in groups:
        chapters.append({"chapterTitle": None, "topics": groups[None]})
    return {"chapters": chapters}


@router.get("/prep-materials/shared-topics/{topicDefinitionId}")
def get_my_shared_topic_lesson(
    topicDefinitionId: str, grade: str, subject: str, teacher_id: str = Depends(require_teacher),
):
    # Looked up by topicDefinitionId + grade alone, not subject -- see
    # admin_misc.py's get_shared_topic_lesson for why: this id may have
    # matched the list above under subject's alias, not subject itself.
    ac = create_admin_client()
    school_id = _my_school_id(ac, teacher_id)
    if not school_id:
        raise HTTPException(status_code=404, detail="Not found")
    row = (
        ac.table("shared_prep_materials").select("lesson, topic")
        .eq("school_id", school_id).eq("grade", grade)
        .eq("topic_definition_id", topicDefinitionId).maybe_single().execute()
    ).data
    if not row:
        raise HTTPException(status_code=404, detail="Not found")
    return {"lesson": row.get("lesson"), "topic": row.get("topic")}


class EnsureStockBody(BaseModel):
    classId: str
    subject: str
    grade: str
    topicDefinitionId: Optional[str] = None


# POST /api/teacher/prep-materials/ensure-stock — idempotent: only starts a
# new batch when this (school, grade, subject) is genuinely low on upcoming
# generated material and nothing is already pending/running for it.
@router.post("/prep-materials/ensure-stock")
def ensure_prep_stock(body: EnsureStockBody, teacher_id: str = Depends(require_teacher)):
    from ..lib.prep_batch_jobs import ensure_stock
    try:
        ac = create_admin_client()
        class_res = ac.table("classes").select("school_id").eq("id", body.classId).maybe_single().execute()
        class_row = class_res.data if class_res else None
        school_id = (class_row or {}).get("school_id")
        if not school_id:
            return {"batchId": None}

        from_order_index = 0
        if body.topicDefinitionId:
            topic_res = (
                ac.table("syllabus_topics").select("order_index")
                .eq("definition_id", body.topicDefinitionId).limit(1).maybe_single().execute()
            )
            topic_row = topic_res.data if topic_res else None
            from_order_index = (topic_row or {}).get("order_index") or 0

        batch_id = ensure_stock(school_id, body.grade, body.subject, from_order_index, teacher_id)
        return {"batchId": batch_id}
    except Exception as e:
        print(f"[teacher/prep-materials/ensure-stock] failed: {e}")
        return {"batchId": None}


# GET /api/teacher/prep-materials/session-topic — the pinned topic for one
# specific timetable period on one specific day (see migration 0004). Reading
# this BEFORE falling back to "first incomplete syllabus topic" is what makes
# reopening the same period on the same day stable, while a different period
# the same day still falls through and gets the next topic in sequence.
@router.get("/prep-materials/session-topic")
def get_prep_session_topic(
    classId: str, subject: str, date: str, periodNumber: int,
    teacher_id: str = Depends(require_teacher),
):
    try:
        ac = create_admin_client()
        row = (
            ac.table("prep_session_topics").select("topic, subtopic")
            .eq("class_id", classId).eq("subject", subject)
            .eq("session_date", date).eq("period_number", periodNumber)
            .maybe_single().execute()
        )
        data = row.data if row else None
        return {"topic": (data or {}).get("topic"), "subtopic": (data or {}).get("subtopic")}
    except Exception as e:
        print(f"[teacher/prep-materials/session-topic] fetch failed: {e}")
        return {"topic": None, "subtopic": None}


# POST /api/teacher/prep-materials/session-topic — pins the topic the FIRST
# time a period is opened that day; every later call for the same
# (classId, subject, date, periodNumber) just overwrites with the same values,
# since the frontend only calls this once per period, right after generation.
@router.post("/prep-materials/session-topic")
def save_prep_session_topic(body: PrepSessionTopicUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("prep_session_topics").upsert({
            "class_id": body.classId, "subject": body.subject,
            "session_date": body.date, "period_number": body.periodNumber,
            "topic": body.topic, "subtopic": body.subtopic,
        }, on_conflict="class_id,subject,session_date,period_number").execute()
    except Exception as e:
        print(f"[teacher/prep-materials/session-topic] save failed: {e}")
    return {"ok": True}


# GET /api/teacher/prep-batches/{batchId} — poll a batch's progress.
@router.get("/prep-batches/{batchId}")
def get_prep_batch(batchId: str, teacher_id: str = Depends(require_teacher)):
    from ..lib.prep_batch_jobs import get_progress
    progress = get_progress(batchId)
    if not progress:
        raise HTTPException(status_code=404, detail="Batch not found")
    return progress


def _intervention_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "studentId": r["student_id"], "teacherId": r["teacher_id"],
        "note": r["note"], "date": r["date"], "createdAt": r["created_at"],
    }


# GET /api/teacher/interventions
@router.get("/interventions")
def get_own_interventions(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("interventions").select("*").eq("teacher_id", teacher_id).execute().data or []
        return {"interventions": [_intervention_row_to_dto(r) for r in rows]}
    except Exception:
        return {"interventions": []}


# GET /api/teacher/interventions/by-student?studentId=...
@router.get("/interventions/by-student")
def get_interventions_by_student(studentId: str, teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("interventions").select("*").eq("student_id", studentId).execute().data or []
        return {"interventions": [_intervention_row_to_dto(r) for r in rows]}
    except Exception:
        return {"interventions": []}


# POST /api/teacher/interventions
@router.post("/interventions")
def upsert_own_intervention(body: TeacherInterventionUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("interventions").upsert({
            "id": body.id, "student_id": body.studentId, "teacher_id": teacher_id,
            "note": body.note, "date": body.date, "created_at": body.createdAt,
        }).execute()
    except Exception:
        pass
    return {"ok": True}


# DELETE /api/teacher/interventions?id=...
@router.delete("/interventions")
def delete_own_intervention(id: str, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("interventions").delete().eq("id", id).execute()
    except Exception:
        pass
    return {"ok": True}


def _doubt_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "studentId": r["student_id"], "studentName": r.get("student_name") or "",
        "classId": r["class_id"], "subject": r.get("subject") or "",
        "question": r["question"], "answer": r.get("answer"),
        "answeredAt": r.get("answered_at"), "createdAt": r.get("created_at") or "",
        "status": r.get("status") or "pending",
    }


# GET /api/teacher/student-doubts?classIds=a,b,c
@router.get("/student-doubts")
def get_student_doubts(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    if not ids:
        return {"doubts": []}
    try:
        ac = create_admin_client()
        rows = ac.table("student_doubts").select("*").in_("class_id", ids).order("created_at", desc=True).execute().data or []
        return {"doubts": [_doubt_row_to_dto(r) for r in rows]}
    except Exception:
        return {"doubts": []}


# GET /api/teacher/student-doubts/pending-count?classIds=a,b,c
@router.get("/student-doubts/pending-count")
def get_pending_doubts_count(classIds: str = "", teacher_id: str = Depends(require_teacher)):
    ids = [c for c in classIds.split(",") if c]
    if not ids:
        return {"count": 0}
    try:
        ac = create_admin_client()
        res = ac.table("student_doubts").select("id", count="exact").in_("class_id", ids).eq("status", "pending").execute()
        return {"count": res.count or 0}
    except Exception:
        return {"count": 0}


# POST /api/teacher/student-doubts
@router.post("/student-doubts")
def upsert_student_doubt(body: TeacherStudentDoubtUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("student_doubts").upsert({
            "id": body.id, "student_id": body.studentId, "student_name": body.studentName,
            "class_id": body.classId, "subject": body.subject, "question": body.question,
            "answer": body.answer, "answered_at": body.answeredAt,
            "created_at": body.createdAt, "status": body.status,
        }).execute()
    except Exception:
        pass
    return {"ok": True}


# PATCH /api/teacher/student-doubts/{id}/answer
@router.patch("/student-doubts/{id}/answer")
def answer_student_doubt(id: str, answer: str, teacher_id: str = Depends(require_teacher)):
    import datetime as _dt
    try:
        create_admin_client().table("student_doubts").update({
            "answer": answer,
            "answered_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
            "status": "answered",
        }).eq("id", id).execute()
    except Exception:
        pass
    return {"ok": True}


# POST /api/teacher/recovery-attempts
@router.post("/recovery-attempts")
def upsert_recovery_attempt(body: TeacherRecoveryAttemptUpsertSchema, teacher_id: str = Depends(require_teacher)):
    create_admin_client().table("recovery_attempts").upsert({
        "id": body.id, "student_id": body.studentId, "topic": body.topic,
        "approach_used": body.approachUsed, "helped": body.helped, "generated_at": body.generatedAt,
    }).execute()
    return {"ok": True}


# GET /api/teacher/recovery-attempts?studentId=...
#
# The paired read. It was dropped during the port and the table went
# write-only, which quietly disabled the feature it exists for: the recovery
# prompt is built around "this child has tried N times, here is what was
# already attempted, now give me something DIFFERENT". With nothing loading
# the history back, every visit told the model no approach had been tried, so
# it could hand back the explanation that had already failed twice.
#
# Oldest first — the prompt numbers the attempts in order, and an approach's
# position in the sequence is part of what makes the next one different.
@router.get("/recovery-attempts")
def get_recovery_attempts(studentId: str, teacher_id: str = Depends(require_teacher)):
    try:
        rows = (
            create_admin_client().table("recovery_attempts").select("*")
            .eq("student_id", studentId).order("generated_at").execute().data or []
        )
        return {"recoveryAttempts": [
            {
                "id": r["id"], "studentId": r["student_id"], "topic": r["topic"],
                "approachUsed": r.get("approach_used") or "", "helped": r.get("helped"),
                "generatedAt": r.get("generated_at") or "",
            }
            for r in rows
        ]}
    except Exception as e:
        # A missing history must not break the student page. The recovery flow
        # degrades to what it did before this endpoint existed.
        print(f"[teacher/recovery-attempts GET] failed: {e}")
        return {"recoveryAttempts": []}


def _taught_topic_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r["teacher_id"], "classId": r["class_id"],
        "date": r["date"], "topic": r["topic"], "subtopic": r.get("subtopic"),
        "createdAt": r["created_at"],
    }


# GET /api/teacher/taught-topics
@router.get("/taught-topics")
def get_own_taught_topics(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("taught_topics").select("*").eq("teacher_id", teacher_id).execute().data or []
        return {"taughtTopics": [_taught_topic_row_to_dto(r) for r in rows]}
    except Exception:
        return {"taughtTopics": []}


# POST /api/teacher/taught-topics
@router.post("/taught-topics")
def upsert_own_taught_topic(body: TeacherTaughtTopicUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("taught_topics").upsert({
            "id": body.id, "teacher_id": teacher_id, "class_id": body.classId,
            "date": body.date, "topic": body.topic, "subtopic": body.subtopic,
            "created_at": body.createdAt,
        }).execute()
    except Exception:
        pass
    return {"ok": True}


def _lesson_feedback_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r["teacher_id"], "classId": r["class_id"],
        "date": r["date"], "topic": r["topic"], "subtopic": r.get("subtopic"),
        "engagement": r.get("engagement"), "comprehension": r.get("comprehension"), "pacing": r.get("pacing"),
        "responses": r.get("responses"), "otherFeedback": r.get("other_feedback"),
        "insight": r.get("insight"), "createdAt": r["created_at"],
    }


# POST /api/teacher/lesson-feedback
@router.post("/lesson-feedback")
def upsert_own_lesson_feedback(body: TeacherLessonFeedbackUpsertSchema, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("lesson_feedback").upsert({
            "id": body.id, "teacher_id": teacher_id, "class_id": body.classId,
            "date": body.date, "topic": body.topic, "subtopic": body.subtopic,
            "engagement": body.engagement, "comprehension": body.comprehension, "pacing": body.pacing,
            "responses": body.responses, "other_feedback": (body.otherFeedback or "").strip() or None,
            "created_at": body.createdAt,
        }).execute()
    except Exception as e:
        print(f"[teacher/lesson-feedback] upsert failed: {e}")
    return {"ok": True}


# GET /api/teacher/lesson-feedback/latest?classId=...&topic=...
@router.get("/lesson-feedback/latest")
def get_latest_lesson_feedback(classId: str, topic: str, teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("lesson_feedback").select("*").eq("class_id", classId).eq("topic", topic) \
            .order("created_at", desc=True).limit(1).execute().data or []
        return {"feedback": _lesson_feedback_row_to_dto(rows[0]) if rows else None}
    except Exception:
        return {"feedback": None}


# PATCH /api/teacher/lesson-feedback/{id}/insight
@router.patch("/lesson-feedback/{id}/insight")
def update_lesson_feedback_insight(id: str, insight: str, teacher_id: str = Depends(require_teacher)):
    try:
        create_admin_client().table("lesson_feedback").update({"insight": insight}).eq("id", id).execute()
    except Exception as e:
        print(f"[teacher/lesson-feedback] insight update failed: {e}")
    return {"ok": True}


# PATCH /api/teacher/classes/{class_id}/feedback-profile?profile=...
@router.patch("/classes/{class_id}/feedback-profile")
def update_class_feedback_profile(class_id: str, profile: str, teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        if class_id not in _teachers_own_and_assigned_class_ids(ac, teacher_id):
            raise HTTPException(status_code=403, detail="Not your class")
        ac.table("classes").update({"feedback_profile": profile}).eq("id", class_id).execute()
    except HTTPException:
        raise
    except Exception as e:
        print(f"[teacher/classes] feedback-profile update failed: {e}")
    return {"ok": True}


def _topic_poll_row_to_dto(r: dict) -> dict:
    return {
        "id": r["id"], "studentId": r["student_id"], "classId": r["class_id"],
        "syllabusTopicId": r.get("syllabus_topic_id"), "topic": r["topic"],
        "subject": r.get("subject"), "response": r["response"], "respondedAt": r.get("responded_at"),
    }


# GET /api/teacher/topic-polls?classId=... — read-only from the teacher side
# (submission is student-side and doesn't go through this backend yet).
@router.get("/topic-polls")
def get_topic_polls_by_class(classId: str, teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("topic_polls").select("*").eq("class_id", classId).execute().data or []
        return {"polls": [_topic_poll_row_to_dto(r) for r in rows]}
    except Exception:
        return {"polls": []}


# GET /api/teacher/academic-calendar
@router.get("/academic-calendar")
def academic_calendar(request: Request):
    try:
        ac = create_admin_client()
        teacher = _soft_resolve_teacher(request, ac)
        if not teacher:
            return {"events": []}
        return {"events": fetch_published_academic_events(teacher["schoolId"], ac)}
    except Exception as e:
        print(f"[teacher/academic-calendar] failed: {e}")
        return {"events": []}


# GET /api/teacher/schedule — the school's fixed daily period grid (Period 1..N
# with times, plus break slots), same structure every working weekday. Used to
# find genuinely empty periods on the home-page schedule; nothing about which
# teacher teaches what — that's the separate timetable/timetable_substitutions data.
@router.get("/schedule")
def get_schedule(request: Request):
    try:
        ac = create_admin_client()
        teacher = _soft_resolve_teacher(request, ac)
        if not teacher:
            return {"slots": []}
        schedule = fetch_school_schedule(teacher["schoolId"], ac)
        return {"slots": (schedule or {}).get("slots") or []}
    except Exception as e:
        print(f"[teacher/schedule] failed: {e}")
        return {"slots": []}


# GET /api/teacher/announcements
@router.get("/announcements")
def announcements(request: Request):
    try:
        ac = create_admin_client()
        teacher = _soft_resolve_teacher(request, ac)
        if not teacher:
            return {"announcements": []}
        items = fetch_school_announcements(teacher["schoolId"], ac)
        return {"announcements": items[:20]}
    except Exception as e:
        print(f"[teacher/announcements] failed: {e}")
        return {"announcements": []}


# GET /api/teacher/student-overview/{studentId}
@router.get("/student-overview/{studentId}")
def student_overview(studentId: str, user: dict = Depends(require_user)):
    try:
        ac = create_admin_client()
        teacher_res = ac.table("teachers").select("id, name, school_id").eq("user_id", user["id"]).maybe_single().execute()
        teacher = teacher_res.data if teacher_res else None
        if not teacher:
            raise HTTPException(status_code=403, detail="Forbidden")

        student_res = ac.table("students").select("*").eq("id", studentId).maybe_single().execute()
        student = student_res.data if student_res else None
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

        cls_res = ac.table("classes").select("*").eq("id", student["class_id"]).maybe_single().execute()
        cls = cls_res.data if cls_res else None
        if not cls:
            raise HTTPException(status_code=404, detail="Class not found")

        if teacher.get("school_id") and cls.get("school_id") and teacher["school_id"] != cls["school_id"]:
            raise HTTPException(status_code=403, detail="Forbidden")

        subjects: list[dict] = []
        assignments = ac.table("teacher_class_assignments").select("teacher_id, subject").eq("class_id", cls["id"]).execute().data or []

        if assignments and any(a.get("subject") for a in assignments):
            for a in assignments:
                t_res = ac.table("teachers").select("name").eq("id", a["teacher_id"]).maybe_single().execute()
                t = t_res.data if t_res else None
                subjects.append({
                    "classId": cls["id"], "subjectName": a.get("subject") or "Subject",
                    "teacherName": (t or {}).get("name") or "Unknown Teacher",
                    "teacherId": a["teacher_id"], "studentRowId": student["id"],
                })
        else:
            q = ac.table("classes").select("*").eq("grade", cls["grade"])
            if cls.get("section"):
                q = q.eq("section", cls["section"])
            if cls.get("school_id"):
                q = q.eq("school_id", cls["school_id"])
            else:
                q = q.eq("school_name", cls.get("school_name"))
            all_classes = q.execute().data or []

            for c in all_classes:
                st_res = (
                    ac.table("students").select("id").eq("class_id", c["id"])
                    .eq("roll_number", student["roll_number"]).eq("is_active", True).maybe_single().execute()
                )
                st = st_res.data if st_res else None
                if not st:
                    continue
                t_res = ac.table("teachers").select("name, subject").eq("id", c.get("teacher_id")).maybe_single().execute()
                t = t_res.data if t_res else None
                subjects.append({
                    "classId": c["id"], "subjectName": (t or {}).get("subject") or c["name"],
                    "teacherName": (t or {}).get("name") or "Unknown Teacher",
                    "teacherId": c.get("teacher_id"), "studentRowId": st["id"],
                })

        subject_data = []
        for s in subjects:
            att_rows = ac.table("attendance").select("status").eq("student_id", s["studentRowId"]).eq("class_id", s["classId"]).execute().data or []
            total_sessions = len(att_rows)
            present_count = sum(1 for a in att_rows if a["status"] in ("present", "late"))
            attendance_rate = present_count / total_sessions if total_sessions > 0 else 0

            recent_marks: list[dict] = []
            avg_score = 0
            entry_added = False

            if s.get("teacherId"):
                tests = ac.table("tests").select("id, topic, total_marks, conducted_on").eq("teacher_id", s["teacherId"]).eq("class_id", s["classId"]).execute().data or []
                if tests:
                    test_ids = [t["id"] for t in tests]
                    marks = ac.table("marks").select("test_id, score").eq("student_id", s["studentRowId"]).in_("test_id", test_ids).execute().data or []
                    if marks:
                        test_map = {t["id"]: t for t in tests}
                        total_pct = sum((m["score"] / test_map[m["test_id"]]["total_marks"]) for m in marks if m["test_id"] in test_map)
                        avg_score = total_pct / len(marks)

                        all_marks = sorted(
                            (
                                {"topic": test_map[m["test_id"]]["topic"], "score": m["score"], "totalMarks": test_map[m["test_id"]]["total_marks"], "date": test_map[m["test_id"]]["conducted_on"]}
                                for m in marks if m["test_id"] in test_map
                            ),
                            key=lambda x: x["date"], reverse=True,
                        )
                        total_tests = len(all_marks)
                        recent_marks = all_marks[:5]
                        subject_data.append({
                            "classId": s["classId"], "subjectName": s["subjectName"], "teacherName": s["teacherName"],
                            "attendanceRate": attendance_rate, "totalSessions": total_sessions,
                            "avgScore": avg_score, "totalTests": total_tests, "recentMarks": recent_marks,
                        })
                        entry_added = True

            if not entry_added:
                subject_data.append({
                    "classId": s["classId"], "subjectName": s["subjectName"], "teacherName": s["teacherName"],
                    "attendanceRate": attendance_rate, "totalSessions": total_sessions,
                    "avgScore": avg_score, "totalTests": len(recent_marks), "recentMarks": recent_marks,
                })

        return {
            "student": {
                "id": student["id"], "name": student["name"], "rollNumber": student["roll_number"],
                "studentCode": student.get("student_code"), "grade": cls.get("grade") or "", "section": cls.get("section") or "",
            },
            "subjects": subject_data,
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[teacher/student-overview] failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")


# ─── Peer pairings ────────────────────────────────────────────────────────────

def _my_class_ids(ac, teacher_id: str) -> list[str]:
    owned = ac.table("classes").select("id").eq("teacher_id", teacher_id).execute().data or []
    assigned = ac.table("teacher_class_assignments").select("class_id").eq("teacher_id", teacher_id).execute().data or []
    return list({*(c["id"] for c in owned), *(a["class_id"] for a in assigned)})


@router.get("/peer-pairings")
async def get_peer_pairings(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        class_ids = _my_class_ids(ac, teacher_id)
        if not class_ids:
            return {"classes": []}

        classes = ac.table("classes").select("id, name, grade, section").in_("id", class_ids).execute().data or []
        pairings = ac.table("peer_pairings").select("*").in_("class_id", class_ids).neq("status", "dissolved").execute().data or []

        student_ids = list({sid for p in pairings for sid in (p["requester_student_id"], p["target_student_id"])})
        students = ac.table("students").select("id, name").in_("id", student_ids).execute().data if student_ids else []
        name_by_id = {s["id"]: s["name"] for s in (students or [])}

        progress_by_pairing_id: dict[str, str] = {}
        for p in pairings:
            if p["status"] == "active" and p.get("responded_at"):
                current_requester = await compute_avg_mastery(ac, p["requester_student_id"], p.get("subject"))
                current_target = await compute_avg_mastery(ac, p["target_student_id"], p.get("subject"))
                progress_by_pairing_id[p["id"]] = compute_progress_status(
                    p["responded_at"], p.get("baseline_requester_mastery"), p.get("baseline_target_mastery"), current_requester, current_target
                )

        result = [
            {
                "classId": cls["id"], "className": cls["name"], "grade": cls["grade"], "section": cls.get("section"),
                "pairings": [
                    {
                        "id": p["id"], "status": p["status"], "subject": p.get("subject"), "activity": p.get("activity"),
                        "requesterName": name_by_id.get(p["requester_student_id"], "Student"),
                        "targetName": name_by_id.get(p["target_student_id"], "Student"),
                        "createdAt": p["created_at"], "progressStatus": progress_by_pairing_id.get(p["id"]),
                    }
                    for p in pairings if p["class_id"] == cls["id"]
                ],
            }
            for cls in classes
        ]
        return {"classes": result}
    except Exception as e:
        print(f"[teacher/peer-pairings GET] failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")


@router.patch("/peer-pairings")
def patch_peer_pairings(body: PeerPairDissolveSchema, teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        row_res = ac.table("peer_pairings").select("id, class_id").eq("id", body.id).maybe_single().execute()
        row = row_res.data if row_res else None
        if not row:
            raise HTTPException(status_code=404, detail="Not found")

        class_ids = _my_class_ids(ac, teacher_id)
        if row["class_id"] not in class_ids:
            raise HTTPException(status_code=403, detail="Forbidden")

        ac.table("peer_pairings").update({"status": "dissolved", "responded_at": datetime.now(timezone.utc).isoformat()}).eq("id", row["id"]).execute()
        return {"ok": True}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[teacher/peer-pairings PATCH] failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")


_SUBSTITUTES_TODAY_EMPTY = {"onLeave": False, "reason": None, "covering": [], "coveredBy": []}


def _build_substitutes_today_status(teacher_id: str, date: str, ac) -> dict:
    avail_res = ac.table("teacher_availability").select("reason, status").eq("teacher_id", teacher_id).eq("date", date).maybe_single().execute()
    avail = avail_res.data if avail_res else None
    # A leave request the admin hasn't approved yet hasn't taken effect: the
    # teacher is still on duty. Matters because this endpoint accepts any date,
    # so a future date with a pending request would otherwise report on-leave —
    # and disagree with the timetable grid rendered beside it, which resolves
    # the same question through timetable_resolution.
    if avail and (avail.get("status") or "approved") != "approved":
        avail = None
    covering_rows = ac.table("timetable_substitutions").select("*").eq("substitute_teacher_id", teacher_id).eq("date", date).execute().data or []
    covered_by_rows = ac.table("timetable_substitutions").select("*").eq("original_teacher_id", teacher_id).eq("date", date).execute().data or []

    class_ids = list({r["class_id"] for r in (*covering_rows, *covered_by_rows)})
    other_teacher_ids = list({
        *(r["original_teacher_id"] for r in covering_rows),
        *(r["substitute_teacher_id"] for r in covered_by_rows if r.get("substitute_teacher_id")),
    })

    class_rows = ac.table("classes").select("id, name").in_("id", class_ids).execute().data or [] if class_ids else []
    teacher_rows = ac.table("teachers").select("id, name").in_("id", other_teacher_ids).execute().data or [] if other_teacher_ids else []
    # Look up start/end times: they live on the ORIGINAL teacher's own published
    # timetable row for that class/day/period (substitutions are seeded from it).
    timetable_rows = ac.table("timetable").select("teacher_id, class_id, day_of_week, period_number, start_time, end_time").in_("teacher_id", [teacher_id, *other_teacher_ids]).execute().data or []

    class_name = {r["id"]: r["name"] for r in class_rows}
    teacher_name = {r["id"]: r["name"] for r in teacher_rows}
    time_by_key = {
        f"{r['teacher_id']}|{r['class_id']}|{r['day_of_week']}|{r['period_number']}": {"startTime": r["start_time"], "endTime": r["end_time"]}
        for r in timetable_rows
    }

    return {
        "onLeave": bool(avail),
        "reason": (avail or {}).get("reason"),
        "covering": [
            {
                "classId": r["class_id"], "className": class_name.get(r["class_id"], "Class"),
                "subject": r.get("subject"), "periodNumber": r["period_number"],
                **time_by_key.get(f"{r['original_teacher_id']}|{r['class_id']}|{r['day_of_week']}|{r['period_number']}", {}),
                "originalTeacherName": teacher_name.get(r["original_teacher_id"], "a teacher"),
                # "assigned" = subject match, full class access — and the only
                # status the automation produces. "manual" (an admin's own
                # reassignment) and legacy "assigned_fallback" are informational
                # only, no prep-material/student access — see school_data()'s
                # covering_rows filter, which only unions in "assigned" classes.
                "status": r["status"],
            }
            for r in covering_rows
        ],
        "coveredBy": [
            {
                "classId": r["class_id"], "className": class_name.get(r["class_id"], "Class"),
                "subject": r.get("subject"), "periodNumber": r["period_number"],
                **time_by_key.get(f"{teacher_id}|{r['class_id']}|{r['day_of_week']}|{r['period_number']}", {}),
                "substituteTeacherName": (teacher_name.get(r["substitute_teacher_id"], "a colleague") if r.get("substitute_teacher_id") else "unassigned — needs admin attention"),
            }
            for r in covered_by_rows
        ],
    }


# GET /api/teacher/substitutes-today?date=YYYY-MM-DD (defaults to today)
# Returns whether the calling teacher is marked unavailable today, which of
# their own periods are being covered by someone else, and which other
# teachers' periods they're covering.
@router.get("/substitutes-today")
def substitutes_today_get(request: Request, date: Optional[str] = None):
    try:
        ac = create_admin_client()
        teacher = _soft_resolve_teacher(request, ac)
        if not teacher:
            return _SUBSTITUTES_TODAY_EMPTY
        date = date or datetime.now(timezone.utc).date().isoformat()
        return _build_substitutes_today_status(teacher["teacherId"], date, ac)
    except Exception as e:
        print(f"[teacher/substitutes-today GET] failed: {e}")
        return _SUBSTITUTES_TODAY_EMPTY


class SubstitutesTodayBody(BaseModel):
    reason: str


# POST { reason } — the teacher's own daily check-in. Always applies to
# today (self check-in isn't for arbitrary dates). This is the PRIMARY way
# availability gets set; the admin's Substitutes page is a fallback override
# for teachers who can't check in themselves.
@router.post("/substitutes-today")
def substitutes_today_post(body: SubstitutesTodayBody, request: Request):
    try:
        ac = create_admin_client()
        teacher = _strict_resolve_teacher(request, ac)

        today = datetime.now(timezone.utc).date().isoformat()

        if body.reason == "available":
            clear_teacher_absence(teacher["schoolId"], teacher["teacherId"], today, ac)
        else:
            apply_teacher_absence(teacher["schoolId"], teacher["teacherId"], today, body.reason, "teacher", None, ac)

        return _build_substitutes_today_status(teacher["teacherId"], today, ac)
    except HTTPException:
        raise
    except Exception as e:
        print(f"[teacher/substitutes-today POST] failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")


# ── Leave requests — the teacher's own self-service view onto teacher_availability ──

_ALLOWED_LEAVE_REASONS = {"on_leave", "late_arrival", "official_duty", "other"}
_MAX_LEAVE_RANGE_DAYS = 60


def _parse_leave_date(s: str) -> Optional[date]:
    try:
        return date.fromisoformat(s)
    except (ValueError, TypeError):
        return None


# GET — the calling teacher's own upcoming leave requests (today onward).
@router.get("/leaves")
def get_leaves(request: Request):
    ac = create_admin_client()
    teacher = _strict_resolve_teacher(request, ac)
    today = datetime.now(timezone.utc).date().isoformat()
    return {"leaves": fetch_teacher_availability_for_teacher(teacher["teacherId"], today, ac)}


class LeaveRequestBody(BaseModel):
    startDate: str
    endDate: str
    reason: str
    note: Optional[str] = None


# POST — the teacher requests leave for a date (or inclusive date range).
# Unlike the same-day check-in, this does NOT take effect immediately: it's
# recorded as "pending" and substitute assignment only happens once an admin
# approves it (see admin_substitutes.py's /leave-requests/approve). A same-day
# emergency should still use /substitutes-today, which stays instant.
@router.post("/leaves")
def post_leave(body: LeaveRequestBody, request: Request):
    ac = create_admin_client()
    teacher = _strict_resolve_teacher(request, ac)

    if not body.startDate or not body.endDate or not body.reason:
        raise HTTPException(status_code=400, detail="startDate, endDate and reason are required.")
    if body.reason not in _ALLOWED_LEAVE_REASONS:
        raise HTTPException(status_code=400, detail="Invalid reason.")

    start = _parse_leave_date(body.startDate)
    end = _parse_leave_date(body.endDate)
    if not start or not end:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.")
    if end < start:
        raise HTTPException(status_code=400, detail="End date must be on or after start date.")

    today = datetime.now(timezone.utc).date()
    if end < today:
        raise HTTPException(status_code=400, detail="Can't request leave for dates entirely in the past.")

    span_days = (end - start).days + 1
    if span_days > _MAX_LEAVE_RANGE_DAYS:
        raise HTTPException(status_code=400, detail=f"Leave requests can span at most {_MAX_LEAVE_RANGE_DAYS} days.")

    # Skip any days already in the past within the range (e.g. a range that
    # starts yesterday and ends next week) rather than rejecting the whole request.
    d = max(start, today)
    while d <= end:
        create_pending_leave_request(teacher["schoolId"], teacher["teacherId"], d.isoformat(), body.reason, body.note or None, ac)
        d += timedelta(days=1)

    return {"leaves": fetch_teacher_availability_for_teacher(teacher["teacherId"], today.isoformat(), ac)}


class LeaveRangeBody(BaseModel):
    startDate: str
    endDate: str


# DELETE — cancels an upcoming leave request (or part of one). Only affects
# today-or-future dates within the given range; past dates are left as history.
@router.delete("/leaves")
def delete_leave(body: LeaveRangeBody, request: Request):
    ac = create_admin_client()
    teacher = _strict_resolve_teacher(request, ac)

    if not body.startDate or not body.endDate:
        raise HTTPException(status_code=400, detail="startDate and endDate are required.")
    start = _parse_leave_date(body.startDate)
    end = _parse_leave_date(body.endDate)
    if not start or not end:
        raise HTTPException(status_code=400, detail="Dates must be YYYY-MM-DD.")

    today = datetime.now(timezone.utc).date()
    d = max(start, today)
    while d <= end:
        # Goes through the automation path, not a bare revert: an already-approved
        # leave may have substitutes assigned against it, and cancelling the leave
        # has to stand them down too.
        clear_teacher_absence(teacher["schoolId"], teacher["teacherId"], d.isoformat(), ac)
        d += timedelta(days=1)

    return {"leaves": fetch_teacher_availability_for_teacher(teacher["teacherId"], today.isoformat(), ac)}


# ── Notifications ───────────────────────────────────────────────────────────

# GET /api/teacher/notifications
@router.get("/notifications")
def get_notifications(teacher_id: str = Depends(require_teacher)):
    ac = create_admin_client()
    return {"notifications": fetch_notifications(teacher_id, ac)}


# PATCH /api/teacher/notifications/{id}/read
@router.patch("/notifications/{id}/read")
def read_notification(id: str, teacher_id: str = Depends(require_teacher)):
    mark_notification_read(id, teacher_id, create_admin_client())
    return {"ok": True}


# PATCH /api/teacher/notifications/read-all
@router.patch("/notifications/read-all")
def read_all_notifications(teacher_id: str = Depends(require_teacher)):
    mark_all_notifications_read(teacher_id, create_admin_client())
    return {"ok": True}
