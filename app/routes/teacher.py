from typing import Optional
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client, get_anon_client
from ..lib.admin_queries import fetch_published_academic_events, fetch_school_announcements, mark_teacher_unavailable, revert_teacher_availability
from ..lib.schemas import PeerPairDissolveSchema
from ..lib.peer_pair_progress import compute_avg_mastery, compute_progress_status
from ..deps import require_user, require_teacher

router = APIRouter()


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

        class_id_set = {r["class_id"] for r in asg_rows} | {r["class_id"] for r in timetable_rows}
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
