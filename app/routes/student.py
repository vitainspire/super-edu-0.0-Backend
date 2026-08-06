import time
import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_auth_rate_limit
from ..lib.student_auth import sign_student_id, verify_student_access
from ..lib.peer_pair_progress import compute_avg_mastery, compute_progress_status
from ..lib.peer_pair_activity import generate_peer_activity
from ..lib.schemas import StudentDoubtSchema, StudentPollSchema, PeerPairRequestSchema, PeerPairActionSchema
from ..deps import require_student_token

router = APIRouter()


class LoginBody(BaseModel):
    studentCode: str


# POST /api/student/login
# Port note: the Next.js original also set an httpOnly `edu-student-id` cookie
# for the Next.js frontend's own page-routing gate — that stays there,
# unchanged, since it's a same-origin Next.js-only concern. This backend has
# no cookie relationship with the frontend's origin, so it returns the signed
# token in the body; the frontend stores it and sends it back as
# X-Student-Token on calls to this backend.
@router.post("/login")
def login(body: LoginBody, request: Request):
    ip = get_client_ip(request)
    t0 = time.time()

    allowed, _ = check_auth_rate_limit(ip)
    if not allowed:
        api_log("student/login", ip, (time.time() - t0) * 1000, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    if not body.studentCode.strip():
        raise HTTPException(status_code=400, detail="Please enter your Student ID.")

    supabase = create_admin_client()
    res = (
        supabase.table("students").select("*, classes(*)")
        .eq("student_code", body.studentCode.strip().upper())
        .eq("is_active", True).maybe_single().execute()
    )
    student = res.data if res else None
    if not student:
        raise HTTPException(status_code=404, detail="Student ID not found. Ask your teacher or school admin for your Student ID.")

    cls = student.get("classes") or {}
    session = {
        "studentId": student["id"], "classId": student["class_id"], "studentName": student["name"],
        "grade": cls.get("grade") or "", "section": cls.get("section") or "", "subject": cls.get("name") or "",
    }

    api_log("student/login", ip, (time.time() - t0) * 1000, False, "ok", user_id=student["id"])
    return {"ok": True, "session": session, "studentToken": sign_student_id(student["id"])}


# POST /api/student/doubt
@router.post("/doubt")
def post_doubt(body: StudentDoubtSchema, request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()

    try:
        supabase = create_admin_client()
        id_ = str(uuid.uuid4())
        created_at = datetime.now(timezone.utc).isoformat()

        supabase.table("student_doubts").insert(
            {
                "id": id_, "student_id": student_id, "student_name": body.studentName or "",
                "class_id": body.classId, "subject": body.subject or "", "question": body.question.strip(),
                "status": "pending", "created_at": created_at,
            }
        ).execute()

        api_log("student/doubt", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
        return {
            "id": id_, "studentId": student_id, "studentName": body.studentName or "", "classId": body.classId,
            "subject": body.subject or "", "question": body.question.strip(), "createdAt": created_at, "status": "pending",
        }
    except Exception as e:
        api_log("student/doubt", ip, (time.time() - t0) * 1000, False, "error", user_id=student_id, error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# GET /api/student/peer-matches?classId=...
@router.get("/peer-matches")
async def peer_matches(classId: str, request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()

    try:
        supabase = create_admin_client()

        me_res = supabase.table("students").select("id, name, interests").eq("id", student_id).maybe_single().execute()
        me = me_res.data if me_res else None
        classmates = supabase.table("students").select("id, name, interests").eq("class_id", classId).eq("is_active", True).neq("id", student_id).execute().data or []
        pairings = (
            supabase.table("peer_pairings").select("*").eq("class_id", classId)
            .or_(f"requester_student_id.eq.{student_id},target_student_id.eq.{student_id}").execute().data or []
        )

        if not me:
            raise HTTPException(status_code=404, detail="Student not found")

        candidates = classmates
        rows = pairings

        incoming = [
            {"id": r["id"], "fromStudentId": r["requester_student_id"], "subject": r.get("subject"), "createdAt": r["created_at"]}
            for r in rows if r["status"] == "pending" and r["target_student_id"] == student_id
        ]
        outgoing = [
            {"id": r["id"], "toStudentId": r["target_student_id"], "subject": r.get("subject"), "createdAt": r["created_at"]}
            for r in rows if r["status"] == "pending" and r["requester_student_id"] == student_id
        ]
        active_rows = [r for r in rows if r["status"] == "active"]

        progress_note_by_id: dict[str, str] = {}
        for r in active_rows:
            if r.get("responded_at"):
                cur_req = await compute_avg_mastery(supabase, r["requester_student_id"], r.get("subject"))
                cur_tgt = await compute_avg_mastery(supabase, r["target_student_id"], r.get("subject"))
                status = compute_progress_status(r["responded_at"], r.get("baseline_requester_mastery"), r.get("baseline_target_mastery"), cur_req, cur_tgt)
                if status == "improving":
                    progress_note_by_id[r["id"]] = "You two are making great progress together!"

        active = [
            {
                "id": r["id"],
                "partnerStudentId": r["target_student_id"] if r["requester_student_id"] == student_id else r["requester_student_id"],
                "subject": r.get("subject"), "activity": r.get("activity"),
                "createdAt": r["created_at"], "progressNote": progress_note_by_id.get(r["id"]),
            }
            for r in active_rows
        ]

        referenced_ids = {r["fromStudentId"] for r in incoming} | {r["toStudentId"] for r in outgoing} | {r["partnerStudentId"] for r in active}
        name_by_id = {c["id"]: c["name"] for c in candidates}
        missing_ids = [i for i in referenced_ids if i not in name_by_id]
        if missing_ids:
            extra = supabase.table("students").select("id, name").in_("id", missing_ids).execute().data or []
            for s in extra:
                name_by_id[s["id"]] = s["name"]

        excluded = {
            (r["target_student_id"] if r["requester_student_id"] == student_id else r["requester_student_id"])
            for r in rows if r["status"] != "dissolved"
        }

        all_ids = [student_id] + [c["id"] for c in candidates]
        mastery_rows = supabase.table("student_topic_mastery").select("student_id, subject, mastery").in_("student_id", all_ids).execute().data or []
        by_subject: dict[str, dict[str, float]] = {}
        for m in mastery_rows:
            if not m.get("subject"):
                continue
            by_subject.setdefault(m["student_id"], {})
            by_subject[m["student_id"]][m["subject"]] = max(by_subject[m["student_id"]].get(m["subject"], 0), m["mastery"])
        my_mastery = by_subject.get(student_id, {})

        scored = []
        for c in candidates:
            if c["id"] in excluded:
                continue
            their_interests = c.get("interests") or []
            shared_interests = [i for i in (me.get("interests") or []) if i in their_interests]
            their_mastery = by_subject.get(c["id"], {})
            complementary_subjects = []
            for subject in set(my_mastery.keys()) | set(their_mastery.keys()):
                mine = my_mastery.get(subject)
                theirs = their_mastery.get(subject)
                if mine is None or theirs is None:
                    continue
                if (mine >= 0.7 and theirs < 0.6) or (theirs >= 0.7 and mine < 0.6):
                    complementary_subjects.append(subject)
            if not shared_interests and not complementary_subjects:
                continue
            reason = (
                f"You both like {' and '.join(shared_interests[:2])}"
                if shared_interests else f"Great match for {complementary_subjects[0]} practice"
            )
            scored.append({"studentId": c["id"], "name": c["name"], "reason": reason, "score": len(shared_interests) * 2 + len(complementary_subjects)})

        matches = [
            {"studentId": m["studentId"], "name": m["name"], "reason": m["reason"]}
            for m in sorted(scored, key=lambda m: m["score"], reverse=True)[:8]
        ]

        api_log("student/peer-matches", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
        return {
            "matches": matches,
            "incoming": [{**r, "fromName": name_by_id.get(r["fromStudentId"], "Classmate")} for r in incoming],
            "outgoing": [{**r, "toName": name_by_id.get(r["toStudentId"], "Classmate")} for r in outgoing],
            "active": [{**r, "partnerName": name_by_id.get(r["partnerStudentId"], "Classmate")} for r in active],
        }
    except HTTPException:
        raise
    except Exception as e:
        api_log("student/peer-matches", ip, (time.time() - t0) * 1000, False, "error", user_id=student_id, error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# GET /api/student/init
@router.get("/init")
def init(request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()

    try:
        supabase = create_admin_client()

        student_res = supabase.table("students").select("*").eq("id", student_id).maybe_single().execute()
        student = student_res.data if student_res else None
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

        cls_res = supabase.table("classes").select("*").eq("id", student["class_id"]).maybe_single().execute()
        cls = cls_res.data if cls_res else None
        if not cls:
            raise HTTPException(status_code=404, detail="Class not found")

        tabs = []
        class_assignments = supabase.table("teacher_class_assignments").select("teacher_id, subject").eq("class_id", cls["id"]).execute().data or []

        if class_assignments and any(a.get("subject") for a in class_assignments):
            for a in class_assignments:
                label = a.get("subject") or "Subject"
                tabs.append({"classId": cls["id"], "studentId": student["id"], "subject": label, "teacherId": a["teacher_id"]})
        else:
            q = supabase.table("classes").select("*").eq("grade", cls["grade"])
            if cls.get("section"):
                q = q.eq("section", cls["section"])
            if cls.get("school_id"):
                q = q.eq("school_id", cls["school_id"])
            else:
                q = q.eq("school_name", cls.get("school_name"))
            all_classes = q.execute().data or []

            for c in all_classes:
                st_res = (
                    supabase.table("students").select("id").eq("class_id", c["id"])
                    .eq("roll_number", student["roll_number"]).eq("is_active", True).maybe_single().execute()
                )
                st = st_res.data if st_res else None
                if not st:
                    continue
                teacher_res = supabase.table("teachers").select("subject, id").eq("id", c.get("teacher_id")).maybe_single().execute()
                teacher = teacher_res.data if teacher_res else None
                label = (teacher or {}).get("subject") or c["name"]
                tabs.append({"classId": c["id"], "studentId": st["id"], "subject": label, "teacherId": (teacher or {}).get("id")})

        api_log("student/init", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
        return {
            "student": {
                "id": student["id"], "name": student["name"], "rollNumber": student["roll_number"],
                "goal": student.get("goal") or "", "interests": student.get("interests") or [],
            },
            "primaryClass": {
                "id": cls["id"], "grade": cls["grade"], "section": cls.get("section") or "",
                "schoolId": cls.get("school_id"), "schoolName": cls.get("school_name") or "",
            },
            "tabs": tabs,
        }
    except HTTPException:
        raise
    except Exception as e:
        api_log("student/init", ip, (time.time() - t0) * 1000, False, "error", user_id=student_id, error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# PATCH /api/student/profile
class ProfileBody(BaseModel):
    interests: list[str]


@router.patch("/profile")
def update_profile(body: ProfileBody, student_id: str = Depends(require_student_token)):
    cleaned = [s.strip() for s in body.interests if isinstance(s, str) and s.strip()][:10]
    supabase = create_admin_client()
    try:
        supabase.table("students").update({"interests": cleaned}).eq("id", student_id).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"interests": cleaned}


# POST /api/student/poll
@router.post("/poll")
def post_poll(body: StudentPollSchema, request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()
    try:
        supabase = create_admin_client()
        supabase.table("topic_polls").upsert(
            {
                "id": str(uuid.uuid4()), "student_id": student_id, "class_id": body.classId,
                "syllabus_topic_id": body.syllabusTopicId, "topic": body.topic or "", "subject": body.subject or "",
                "response": body.response, "responded_at": datetime.now(timezone.utc).isoformat(),
            },
            on_conflict="student_id,syllabus_topic_id",
        ).execute()
        api_log("student/poll", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
        return {"ok": True}
    except Exception as e:
        api_log("student/poll", ip, (time.time() - t0) * 1000, False, "error", user_id=student_id, error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# POST /api/student/peer-pairings — send a pairing request to a classmate.
@router.post("/peer-pairings")
def post_peer_pairing(body: PeerPairRequestSchema, request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()

    if body.targetStudentId == student_id:
        raise HTTPException(status_code=400, detail="You cannot pair with yourself")

    try:
        supabase = create_admin_client()

        # Idempotency — don't create a second request if any non-dissolved
        # relationship already exists between these two students in this class.
        existing_res = (
            supabase.table("peer_pairings").select("id, status").eq("class_id", body.classId).neq("status", "dissolved")
            .or_(
                f"and(requester_student_id.eq.{student_id},target_student_id.eq.{body.targetStudentId}),"
                f"and(requester_student_id.eq.{body.targetStudentId},target_student_id.eq.{student_id})"
            )
            .maybe_single().execute()
        )
        if existing_res.data if existing_res else None:
            raise HTTPException(status_code=409, detail="A request or pairing already exists with this classmate")

        id_ = str(uuid.uuid4())
        supabase.table("peer_pairings").insert({
            "id": id_, "class_id": body.classId, "subject": body.subject,
            "requester_student_id": student_id, "target_student_id": body.targetStudentId, "status": "pending",
        }).execute()

        api_log("student/peer-pairings", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
        return {"id": id_, "status": "pending"}
    except HTTPException:
        raise
    except Exception as e:
        api_log("student/peer-pairings", ip, (time.time() - t0) * 1000, False, "error", user_id=student_id, error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# PATCH /api/student/peer-pairings — accept an incoming request, or cancel
# your own still-pending outgoing one. No "decline" action is exposed — an
# unwanted request is simply left pending rather than surfacing rejection.
@router.patch("/peer-pairings")
async def patch_peer_pairing(body: PeerPairActionSchema, request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()

    try:
        supabase = create_admin_client()
        row_res = supabase.table("peer_pairings").select("*").eq("id", body.id).maybe_single().execute()
        row = row_res.data if row_res else None
        if not row or row["status"] != "pending":
            raise HTTPException(status_code=404, detail="Request not found or no longer pending")

        if body.action == "accept":
            if row["target_student_id"] != student_id:
                raise HTTPException(status_code=403, detail="Forbidden")

            students = supabase.table("students").select("id, name").in_("id", [row["requester_student_id"], row["target_student_id"]]).execute().data or []
            name_by_id = {s["id"]: s["name"] for s in students}

            activity = await generate_peer_activity(
                row.get("subject"), name_by_id.get(row["requester_student_id"], "Your buddy"), name_by_id.get(row["target_student_id"], "You"),
            )
            baseline_requester = await compute_avg_mastery(supabase, row["requester_student_id"], row.get("subject"))
            baseline_target = await compute_avg_mastery(supabase, row["target_student_id"], row.get("subject"))

            supabase.table("peer_pairings").update({
                "status": "active", "responded_at": datetime.now(timezone.utc).isoformat(), "activity": activity,
                "baseline_requester_mastery": baseline_requester, "baseline_target_mastery": baseline_target,
            }).eq("id", body.id).execute()

            api_log("student/peer-pairings", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
            return {"id": body.id, "status": "active", "activity": activity}

        # action == "cancel" — only the original requester can withdraw their own request.
        if row["requester_student_id"] != student_id:
            raise HTTPException(status_code=403, detail="Forbidden")

        supabase.table("peer_pairings").update({"status": "dissolved", "responded_at": datetime.now(timezone.utc).isoformat()}).eq("id", body.id).execute()

        api_log("student/peer-pairings", ip, (time.time() - t0) * 1000, False, "ok", user_id=student_id)
        return {"id": body.id, "status": "dissolved"}
    except HTTPException:
        raise
    except Exception as e:
        api_log("student/peer-pairings", ip, (time.time() - t0) * 1000, False, "error", user_id=student_id, error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# GET /api/student/tab-data?classId=...&studentId=...&teacherId=... (teacherId optional)
@router.get("/tab-data")
async def tab_data(request: Request, classId: Optional[str] = None, studentId: Optional[str] = None, teacherId: Optional[str] = None, cookie_student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    t0 = time.time()
    try:
        if not classId or not studentId:
            api_log("student/tab-data", ip, (time.time() - t0) * 1000, False, "bad_request", user_id=cookie_student_id)
            raise HTTPException(status_code=400, detail="classId and studentId required")

        # The requested (studentId, classId) must belong to the same physical student as
        # the authenticated cookie — closes an IDOR that let any student read any other
        # student's attendance/marks/mastery by just changing these query params.
        if not await verify_student_access(cookie_student_id, studentId, classId):
            api_log("student/tab-data", ip, (time.time() - t0) * 1000, False, "forbidden", user_id=cookie_student_id)
            raise HTTPException(status_code=403, detail="Forbidden")

        supabase = create_admin_client()

        # When teacherId is provided, filter teacher-owned data (sessions/syllabus/tests) by that teacher
        tests_q = supabase.table("tests").select("*").eq("class_id", classId)
        sessions_q = supabase.table("sessions").select("*").eq("class_id", classId)
        syllabus_q = supabase.table("syllabus_topics").select("*").eq("class_id", classId).order("order_index")
        if teacherId:
            tests_q = tests_q.eq("teacher_id", teacherId)
            sessions_q = sessions_q.eq("teacher_id", teacherId)
            syllabus_q = syllabus_q.eq("teacher_id", teacherId)

        today = datetime.now(timezone.utc).date().isoformat()

        attendance_data = supabase.table("attendance").select("*").eq("student_id", studentId).eq("class_id", classId).execute().data or []
        marks_data = supabase.table("marks").select("*").eq("student_id", studentId).execute().data or []
        tests_data = tests_q.execute().data or []
        mastery_data = supabase.table("student_topic_mastery").select("*").eq("student_id", studentId).execute().data or []
        catchup_data = supabase.table("catchup_materials").select("*").eq("student_id", studentId).execute().data or []
        sessions_data = sessions_q.execute().data or []
        syllabus_data = syllabus_q.execute().data or []
        timetable_data = supabase.table("timetable").select("*").eq("class_id", classId).execute().data or []
        doubts_data = supabase.table("student_doubts").select("*").eq("student_id", studentId).order("created_at", desc=True).execute().data or []
        polls_data = supabase.table("topic_polls").select("*").eq("student_id", studentId).eq("class_id", classId).execute().data or []
        substitutions_data = supabase.table("timetable_substitutions").select("*").eq("class_id", classId).eq("date", today).execute().data or []

        substitute_teacher_ids = list({r["substitute_teacher_id"] for r in substitutions_data if r.get("substitute_teacher_id")})
        substitute_names = supabase.table("teachers").select("id, name").in_("id", substitute_teacher_ids).execute().data if substitute_teacher_ids else []
        name_by_id = {t["id"]: t["name"] for t in substitute_names}

        tests_by_id = {t["id"]: t for t in tests_data}
        marks = []
        for r in marks_data:
            test = tests_by_id.get(r["test_id"])
            topic = test.get("topic") if test else ""
            if not topic:
                continue
            marks.append({
                "id": r["id"], "testId": r["test_id"], "studentId": r["student_id"], "score": r["score"],
                "feedback": r.get("feedback"), "enteredAt": r.get("entered_at"), "source": r.get("source"),
                "topic": topic, "totalMarks": test.get("total_marks") or 0, "conductedOn": test.get("conducted_on") or "",
            })

        api_log("student/tab-data", ip, (time.time() - t0) * 1000, False, "ok", user_id=cookie_student_id)
        return {
            "attendance": [
                {
                    "id": r["id"], "sessionId": r.get("session_id") or "", "studentId": r["student_id"],
                    "classId": r.get("class_id") or "", "syllabusTopicId": r.get("syllabus_topic_id") or "",
                    "date": r["date"], "status": r["status"],
                }
                for r in attendance_data
            ],
            "marks": marks,
            "tests": [
                {
                    "id": r["id"], "teacherId": r.get("teacher_id"), "classId": r["class_id"],
                    "subject": r["subject"], "topic": r["topic"], "totalMarks": r["total_marks"],
                    "conductedOn": r["conducted_on"], "term": r.get("term"),
                }
                for r in tests_data
            ],
            "mastery": [
                {
                    "id": r["id"], "studentId": r["student_id"], "topic": r["topic"], "subject": r["subject"],
                    "mastery": r["mastery"], "attempts": r["attempts"], "lastUpdated": r.get("last_updated"),
                }
                for r in mastery_data
            ],
            "catchupMaterials": [
                {
                    "id": r["id"], "teacherId": r.get("teacher_id"), "studentId": r["student_id"],
                    "studentName": r.get("student_name"), "topic": r["topic"], "subject": r["subject"],
                    "grade": r.get("grade"), "explanation": r.get("explanation"),
                    "practiceQuestions": r.get("practice_questions") or [],
                    "activity": r.get("activity"), "focusNote": r.get("focus_note"),
                    "status": r["status"], "createdAt": r.get("created_at"),
                    "reason": r.get("reason"),
                }
                for r in catchup_data
            ],
            "sessions": [
                {
                    "id": r["id"], "classId": r["class_id"], "teacherId": r.get("teacher_id"),
                    "syllabusTopicId": r.get("syllabus_topic_id"), "topic": r.get("topic"),
                    "date": r["date"], "createdAt": r.get("created_at") or "",
                }
                for r in sessions_data
            ],
            "syllabusTopics": [
                {
                    "id": r["id"], "classId": r["class_id"], "teacherId": r.get("teacher_id"),
                    "topic": r["topic"], "description": r.get("description") or "",
                    "weekNumber": r.get("week_number"), "orderIndex": r.get("order_index") or 0,
                    "isCompleted": r.get("is_completed") or False, "createdAt": r.get("created_at") or "",
                    "estimatedSessions": r.get("estimated_sessions"),
                    "definitionId": r.get("definition_id"),
                    "subject": r.get("subject"),
                    "prerequisiteDefinitionId": r.get("prerequisite_definition_id"),
                }
                for r in syllabus_data
            ],
            "timetable": [
                {
                    "id": r["id"], "teacherId": r.get("teacher_id"), "classId": r["class_id"],
                    "dayOfWeek": r["day_of_week"], "periodNumber": r["period_number"],
                    "startTime": r["start_time"], "endTime": r["end_time"],
                    "label": r.get("label"),
                }
                for r in timetable_data
            ],
            # Today's substitute coverage for this class, if any — periodNumber
            # matches a row in `timetable` above so the client can flag that period.
            "substitutions": [
                {
                    "periodNumber": r["period_number"],
                    "subject": r.get("subject"),
                    "substituteTeacherName": (name_by_id.get(r["substitute_teacher_id"], "a substitute teacher") if r.get("substitute_teacher_id") else None),
                    "status": r["status"],
                }
                for r in substitutions_data
            ],
            "doubts": [
                {
                    "id": r["id"], "studentId": r["student_id"], "studentName": r.get("student_name"),
                    "classId": r["class_id"], "subject": r["subject"], "question": r["question"],
                    "answer": r.get("answer"), "answeredAt": r.get("answered_at"),
                    "createdAt": r.get("created_at") or "", "status": r.get("status") or "pending",
                }
                for r in doubts_data
            ],
            "polls": [
                {
                    "id": r["id"], "studentId": r["student_id"], "classId": r["class_id"],
                    "syllabusTopicId": r["syllabus_topic_id"], "topic": r["topic"],
                    "subject": r["subject"], "response": r["response"], "respondedAt": r["responded_at"],
                }
                for r in polls_data
            ],
        }
    except HTTPException:
        raise
    except Exception as e:
        api_log("student/tab-data", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Server error")
