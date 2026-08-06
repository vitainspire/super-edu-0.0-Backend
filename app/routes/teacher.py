from typing import Optional
from datetime import date, datetime, timedelta, timezone
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client, get_anon_client
from ..lib.admin_queries import (
    fetch_published_academic_events, fetch_school_announcements, mark_teacher_unavailable,
    revert_teacher_availability, fetch_teacher_availability_for_teacher, create_pending_leave_request,
    fetch_school_schedule,
)
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

        # Today only, and only subject-matched coverage (status="assigned") —
        # a fallback substitute (status="assigned_fallback", no subject match)
        # deliberately does NOT get full class access this way; they only see
        # the period exists via /api/teacher/substitutes-today, with no
        # students/syllabus/prep-material behind it.
        today_str = datetime.now(timezone.utc).date().isoformat()
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


# GET /api/teacher/timetable — own entries only.
@router.get("/timetable")
def get_own_timetable(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("timetable").select("*").eq("teacher_id", teacher_id).execute().data or []
        return {"timetable": [_timetable_row_to_dto(r) for r in rows]}
    except Exception:
        return {"timetable": []}


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
        "lesson": r["lesson"], "createdAt": r["created_at"],
    }


# GET /api/teacher/prep-materials
@router.get("/prep-materials")
def get_own_prep_materials(teacher_id: str = Depends(require_teacher)):
    try:
        ac = create_admin_client()
        rows = ac.table("prep_materials").select("*").eq("teacher_id", teacher_id).execute().data or []
        return {"prepMaterials": [_prep_material_row_to_dto(r) for r in rows]}
    except Exception:
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
        }).execute()
    except Exception as e:
        print(f"[teacher/prep-materials] upsert failed: {e}")
    return {"ok": True}


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

        q = (
            ac.table("shared_prep_materials").select("lesson, subtopic")
            .eq("school_id", school_id).eq("grade", grade).eq("subject", subject)
        )
        q = q.eq("topic_definition_id", topicDefinitionId) if topicDefinitionId else q.ilike("topic", topic)
        rows = q.execute().data or []
        if not rows:
            return {"lesson": None}
        want_sub = (subtopic or "").strip().lower()
        exact = next((r for r in rows if (r.get("subtopic") or "").strip().lower() == want_sub), None)
        return {"lesson": (exact or rows[0]).get("lesson")}
    except Exception as e:
        print(f"[teacher/prep-materials/shared] fetch failed: {e}")
        return {"lesson": None}


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


# POST /api/teacher/recovery-attempts — write-only from the teacher portal
# (fetchPreviousApproaches, the paired read, has no live caller — skipped).
@router.post("/recovery-attempts")
def upsert_recovery_attempt(body: TeacherRecoveryAttemptUpsertSchema, teacher_id: str = Depends(require_teacher)):
    create_admin_client().table("recovery_attempts").upsert({
        "id": body.id, "student_id": body.studentId, "topic": body.topic,
        "approach_used": body.approachUsed, "helped": body.helped, "generated_at": body.generatedAt,
    }).execute()
    return {"ok": True}


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
    avail_res = ac.table("teacher_availability").select("reason").eq("teacher_id", teacher_id).eq("date", date).maybe_single().execute()
    avail = avail_res.data if avail_res else None
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
                # "assigned" = subject match, full class access; "assigned_fallback"
                # (or "manual", from an admin's own reassignment) = informational
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
            revert_teacher_availability(teacher["teacherId"], today, ac)
        else:
            mark_teacher_unavailable(teacher["schoolId"], teacher["teacherId"], today, body.reason, "teacher", None, ac)

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
        revert_teacher_availability(teacher["teacherId"], d.isoformat(), ac)
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
