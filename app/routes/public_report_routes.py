"""Public (no-login) parent progress report. A parent opens this from a link
a teacher shares over WhatsApp/print/etc — there is deliberately no
require_user/require_teacher dependency here.

Only factual, cross-subject performance data goes out (same shape as
/api/teacher/student-overview/{id}): no intervention notes, no internal
warnings, and no AI/LLM call, since an unauthenticated route must never be
able to trigger a billed model call on demand. The "how's this subject
going" label is computed from the same score thresholds the rest of the app
already uses to color-code mastery, not generated text."""
from fastapi import APIRouter, HTTPException, Request

from ..lib.supabase_clients import create_admin_client
from ..lib.report_share import verify_report_token
from ..lib.student_overview import build_student_subject_overview
from ..lib.exam_matching import build_student_exam_schedule
from ..lib.rate_limit import check_rate_limit
from ..lib.logger import get_client_ip

router = APIRouter()


def _status_label(pct: float) -> str:
    if pct >= 0.75:
        return "Doing well"
    if pct >= 0.5:
        return "Needs some help"
    return "Needs attention"


@router.get("/report/{token}")
def get_public_report(token: str, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    student_id = verify_report_token(token)
    if not student_id:
        raise HTTPException(status_code=404, detail="This link has expired or is invalid.")

    ac = create_admin_client()
    student_res = ac.table("students").select("*").eq("id", student_id).maybe_single().execute()
    student = student_res.data if student_res else None
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    cls_res = ac.table("classes").select("*").eq("id", student["class_id"]).maybe_single().execute()
    cls = cls_res.data if cls_res else None
    if not cls:
        raise HTTPException(status_code=404, detail="Class not found")

    overview = build_student_subject_overview(ac, student, cls)
    subjects = overview["subjects"]

    weighted_tests = sum(s["totalTests"] for s in subjects)
    overall_avg_score = (
        sum(s["avgScore"] * s["totalTests"] for s in subjects) / weighted_tests if weighted_tests > 0 else 0
    )
    weighted_sessions = sum(s["totalSessions"] for s in subjects)
    overall_attendance = (
        sum(s["attendanceRate"] * s["totalSessions"] for s in subjects) / weighted_sessions if weighted_sessions > 0 else 0
    )

    return {
        "student": overview["student"],
        "overall": {
            "attendanceRate": overall_attendance,
            "avgScore": overall_avg_score,
            "statusLabel": _status_label(overall_avg_score),
        },
        "subjects": [
            {**s, "statusLabel": _status_label(s["avgScore"])}
            for s in subjects
        ],
        "examSchedule": build_student_exam_schedule(ac, student, cls),
    }
