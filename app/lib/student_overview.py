"""Whole-child, cross-subject aggregation for a student — every subject the
student is taking (not just the viewing teacher's own class), with
attendance/avg-score/tests-taken per subject.

Extracted from teacher.py's student_overview route so the same aggregation
can back both the authenticated /api/teacher/student-overview/{id} endpoint
and the public (unauthenticated, token-gated) share-link endpoint, with no
duplication of the subject-matching logic."""


def resolve_student_subjects(ac, student: dict, cls: dict) -> list[dict]:
    """Every subject this student is taking, with the (classId, teacherId,
    studentRowId) needed to query that subject's attendance/tests/marks —
    shared by build_student_subject_overview below and
    app/lib/exam_matching.py's build_student_exam_schedule, so the
    subject-discovery logic lives in exactly one place."""
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

    return subjects


def build_student_subject_overview(ac, student: dict, cls: dict) -> dict:
    subjects = resolve_student_subjects(ac, student, cls)

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
