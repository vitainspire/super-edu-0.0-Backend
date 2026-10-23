"""Links a teacher's test to an admin-scheduled exam calendar event, and
builds the student-facing "scheduled exams" view for the progress report.

See migrations/025_exam_scoping.sql for the two columns this relies on:
academic_events.grades (nullable, JSON-encoded grade list — None means every
grade) and tests.exam_event_id (nullable, no FK — see that migration's
comment for why)."""
import json
from typing import Optional

from .student_overview import resolve_student_subjects


def _event_grades(e: dict) -> Optional[list]:
    raw = e.get("grades")
    return json.loads(raw) if raw else None


def find_matching_exam_event(ac, class_id: str, conducted_on: str) -> Optional[dict]:
    """The scheduled "exam" event whose date range contains conducted_on and
    whose grade scope includes this class's grade (or applies to every
    grade). Called right after a teacher's test is saved, so a test dated
    inside an admin-scheduled exam window auto-attaches with no manual step
    — teachers' own smaller tests (quizzes, unit checks) simply find no
    match here and stay standalone, exactly like before this feature.

    Only matches PUBLISHED events — same gating the teacher-facing calendar
    already uses (fetch_published_academic_events). A draft an admin hasn't
    circulated yet shouldn't silently attach itself to a test."""
    cls_res = ac.table("classes").select("grade, school_id").eq("id", class_id).maybe_single().execute()
    cls = cls_res.data if cls_res else None
    if not cls or not cls.get("school_id"):
        return None

    events = (
        ac.table("academic_events").select("id, title, grades")
        .eq("school_id", cls["school_id"]).eq("category", "exam").eq("published", True)
        .lte("start_date", conducted_on).gte("end_date", conducted_on)
        .execute().data or []
    )
    for e in events:
        grades = _event_grades(e)
        if grades is None or cls.get("grade") in grades:
            return {"id": e["id"], "title": e["title"]}
    return None


def build_student_exam_schedule(ac, student: dict, cls: dict) -> list[dict]:
    """Every grade-scoped, PUBLISHED exam event for this student's grade,
    each with one row per subject the student takes: a real score where a
    matching test + mark already exists, otherwise "scheduled" — the
    progress report's placeholder section, filled in automatically as
    teachers create tests that find_matching_exam_event links above. A
    draft the admin hasn't published yet stays invisible here too, same as
    on the teacher-facing calendar."""
    if not cls.get("school_id"):
        return []

    events = (
        ac.table("academic_events").select("id, title, start_date, end_date, grades")
        .eq("school_id", cls["school_id"]).eq("category", "exam").eq("published", True)
        .order("start_date", desc=False)
        .execute().data or []
    )
    relevant = [e for e in events if (lambda g: g is None or cls.get("grade") in g)(_event_grades(e))]
    if not relevant:
        return []

    subjects = resolve_student_subjects(ac, student, cls)

    schedule = []
    for e in relevant:
        subject_rows = []
        for s in subjects:
            test_res = (
                ac.table("tests").select("id, total_marks")
                .eq("exam_event_id", e["id"]).eq("class_id", s["classId"])
                .maybe_single().execute()
            )
            test = test_res.data if test_res else None
            row: dict = {"subjectName": s["subjectName"], "teacherName": s["teacherName"], "status": "scheduled"}
            if test:
                mark_res = (
                    ac.table("marks").select("score")
                    .eq("test_id", test["id"]).eq("student_id", s["studentRowId"])
                    .maybe_single().execute()
                )
                mark = mark_res.data if mark_res else None
                if mark:
                    row["status"] = "completed"
                    row["score"] = mark["score"]
                    row["totalMarks"] = test["total_marks"]
            subject_rows.append(row)
        schedule.append({
            "examTitle": e["title"], "startDate": e["start_date"], "endDate": e["end_date"],
            "subjects": subject_rows,
        })
    return schedule
