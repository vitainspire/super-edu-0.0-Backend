from fastapi import APIRouter, Depends, HTTPException

from ..lib.supabase_clients import create_admin_client
from ..deps import require_admin

router = APIRouter()


@router.get("/{schoolId}/students/{studentId}/full-record")
def get_full_record(schoolId: str, studentId: str, admin: dict = Depends(require_admin)):
    """The complete picture of one student: every subject's
    attendance/marks/mastery/syllabus progress, plus doubts, teacher notes,
    and catch-up materials — aggregated across every sibling `students` row
    the legacy per-subject data model creates for the same physical child."""
    try:
        ac = create_admin_client()

        student_res = ac.table("students").select("*").eq("id", studentId).maybe_single().execute()
        student = student_res.data if student_res else None
        if not student:
            raise HTTPException(status_code=404, detail="Student not found")

        cls_res = ac.table("classes").select("*").eq("id", student["class_id"]).maybe_single().execute()
        cls = cls_res.data if cls_res else None
        if not cls:
            raise HTTPException(status_code=404, detail="Class not found")
        if cls.get("school_id") != schoolId:
            raise HTTPException(status_code=403, detail="Forbidden")

        subject_refs = []
        assignments = ac.table("teacher_class_assignments").select("teacher_id, subject").eq("class_id", cls["id"]).execute().data or []

        if assignments and any(a.get("subject") for a in assignments):
            for a in assignments:
                t_res = ac.table("teachers").select("name").eq("id", a["teacher_id"]).maybe_single().execute()
                t = t_res.data if t_res else None
                subject_refs.append({
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
                subject_refs.append({
                    "classId": c["id"], "subjectName": (t or {}).get("subject") or c["name"],
                    "teacherName": (t or {}).get("name") or "Unknown Teacher",
                    "teacherId": c.get("teacher_id"), "studentRowId": st["id"],
                })

        sibling_student_ids = list({s["studentRowId"] for s in subject_refs})

        subjects = []
        for s in subject_refs:
            att_rows = ac.table("attendance").select("status").eq("student_id", s["studentRowId"]).eq("class_id", s["classId"]).execute().data or []
            total_sessions = len(att_rows)
            present_count = sum(1 for a in att_rows if a["status"] in ("present", "late"))
            attendance_rate = present_count / total_sessions if total_sessions > 0 else 0

            tests = (
                ac.table("tests").select("id, topic, total_marks, conducted_on").eq("teacher_id", s["teacherId"]).eq("class_id", s["classId"]).execute().data
                if s.get("teacherId") else []
            ) or []

            marks: list[dict] = []
            avg_score = 0
            if tests:
                test_ids = [t["id"] for t in tests]
                mark_rows = ac.table("marks").select("test_id, score").eq("student_id", s["studentRowId"]).in_("test_id", test_ids).execute().data or []
                test_map = {t["id"]: t for t in tests}
                marks = sorted(
                    (
                        {"topic": test_map[m["test_id"]]["topic"], "score": m["score"], "totalMarks": test_map[m["test_id"]]["total_marks"], "date": test_map[m["test_id"]]["conducted_on"]}
                        for m in mark_rows if m["test_id"] in test_map
                    ),
                    key=lambda x: x["date"], reverse=True,
                )
                if marks:
                    avg_score = sum((m["score"] / m["totalMarks"]) if m["totalMarks"] > 0 else 0 for m in marks) / len(marks)

            mastery_rows = ac.table("student_topic_mastery").select("topic, mastery, attempts").eq("student_id", s["studentRowId"]).eq("subject", s["subjectName"]).execute().data or []
            syllabus_rows = ac.table("syllabus_topics").select("is_completed").eq("class_id", s["classId"]).execute().data or []

            subjects.append({
                "classId": s["classId"], "subjectName": s["subjectName"], "teacherName": s["teacherName"],
                "attendanceRate": attendance_rate, "totalSessions": total_sessions, "avgScore": avg_score,
                "totalTests": len(marks), "marks": marks,
                "mastery": [{"topic": m["topic"], "mastery": m["mastery"], "attempts": m["attempts"]} for m in mastery_rows],
                "syllabus": {"done": sum(1 for t in syllabus_rows if t.get("is_completed")), "total": len(syllabus_rows)},
            })

        doubts = (
            ac.table("student_doubts").select("subject, question, answer, status, created_at")
            .in_("student_id", sibling_student_ids).order("created_at", desc=True).execute().data
            if sibling_student_ids else []
        ) or []
        notes = (
            ac.table("interventions").select("note, date, teacher_id")
            .in_("student_id", sibling_student_ids).order("date", desc=True).execute().data
            if sibling_student_ids else []
        ) or []
        catchup = (
            ac.table("catchup_materials").select("topic, subject, status, created_at")
            .in_("student_id", sibling_student_ids).order("created_at", desc=True).execute().data
            if sibling_student_ids else []
        ) or []

        note_teacher_ids = list({n["teacher_id"] for n in notes if n.get("teacher_id")})
        note_teachers = ac.table("teachers").select("id, name").in_("id", note_teacher_ids).execute().data if note_teacher_ids else []
        teacher_name_by_id = {t["id"]: t["name"] for t in (note_teachers or [])}

        return {
            "student": {
                "id": student["id"], "name": student["name"], "rollNumber": student["roll_number"],
                "studentCode": student.get("student_code"), "grade": cls.get("grade") or "", "section": cls.get("section") or "",
            },
            "subjects": subjects,
            "doubts": [{"subject": d.get("subject"), "question": d["question"], "answer": d.get("answer"), "status": d["status"], "createdAt": d["created_at"]} for d in doubts],
            "interventionNotes": [{"note": n["note"], "date": n["date"], "teacherName": teacher_name_by_id.get(n.get("teacher_id"), "Teacher")} for n in notes],
            "catchupMaterials": [{"topic": c["topic"], "subject": c.get("subject"), "status": c["status"], "createdAt": c["created_at"]} for c in catchup],
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[admin/students/full-record GET] failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")
