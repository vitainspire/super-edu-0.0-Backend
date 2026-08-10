from datetime import date, datetime, timedelta, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_school_teachers, fetch_school_classes, fetch_teacher_availability, fetch_substitutions_for_date,
    update_substitute_assignment, fetch_teacher_eligibility_data,
    fetch_pending_leave_requests,
)
from ..lib.substitute_finder import suggest_swap
from ..lib.substitute_automation import apply_teacher_absence, clear_teacher_absence
from ..lib.notifications import create_notification
from ..deps import require_admin

router = APIRouter()


def _build_payload(school_id: str, date: str, ac) -> dict:
    teachers = fetch_school_teachers(school_id, ac)
    classes = fetch_school_classes(school_id, ac)
    availability = fetch_teacher_availability(school_id, date, ac)
    substitutions = fetch_substitutions_for_date(school_id, date, ac)

    teacher_name = {t["id"]: t["name"] for t in teachers}
    class_name = {c["id"]: c["name"] for c in classes}

    # For any unresolved period, check whether swapping this class's schedule
    # for the day would free up a qualified teacher — suggestion only, never
    # auto-applied. Only bothers with the extra queries when something's
    # actually unresolved.
    unresolved = [s for s in substitutions if s["status"] == "unresolved"]
    suggestion_by_teacher_slot: dict[str, dict] = {}
    if unresolved:
        candidates = fetch_teacher_eligibility_data(school_id, date, ac)
        exclude_teacher_ids = {a["teacherId"] for a in availability}
        used_by_slot: dict[int, set] = {}
        for s in substitutions:
            if not s.get("substituteTeacherId"):
                continue
            used_by_slot.setdefault(s["periodNumber"], set()).add(s["substituteTeacherId"])

        for s in unresolved:
            rows = (
                ac.table("timetable").select("period_number, label, teacher_id")
                .eq("class_id", s["classId"]).eq("day_of_week", s["dayOfWeek"]).neq("period_number", s["periodNumber"])
                .execute().data or []
            )
            class_periods = [{"periodNumber": r["period_number"], "subject": r.get("label") or "", "teacherId": r["teacher_id"]} for r in rows]

            suggestion = suggest_swap(
                {"dayOfWeek": s["dayOfWeek"], "periodNumber": s["periodNumber"], "subject": s.get("subject") or ""},
                class_periods, candidates, exclude_teacher_ids,
                lambda pn: used_by_slot.get(pn, set()),
            )
            if suggestion:
                suggestion_by_teacher_slot[s["id"]] = suggestion

    def _map_sub(s: dict) -> dict:
        suggestion = suggestion_by_teacher_slot.get(s["id"])
        return {
            **s,
            "className": class_name.get(s["classId"], "Class"),
            "originalTeacherName": teacher_name.get(s["originalTeacherId"], "Teacher"),
            "substituteTeacherName": teacher_name.get(s["substituteTeacherId"], "Teacher") if s.get("substituteTeacherId") else None,
            "suggestion": ({
                "swapPeriodNumber": suggestion["swapPeriodNumber"],
                "swapSubject": suggestion["swapSubject"],
                "movingTeacherName": teacher_name.get(suggestion["movingTeacherId"], "a teacher"),
                "freeingTeacherName": teacher_name.get(suggestion["freeingTeacherId"], "a teacher"),
            } if suggestion else None),
        }

    return {
        "teachers": teachers,
        "availability": availability,
        "substitutions": [_map_sub(s) for s in substitutions],
    }


@router.get("/{schoolId}/substitutes")
def get_substitutes(schoolId: str, date: Optional[str] = None, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    date = date or datetime.now(timezone.utc).date().isoformat()
    return _build_payload(schoolId, date, ac)


class MarkUnavailableBody(BaseModel):
    date: str
    teacherId: str
    reason: str
    note: Optional[str] = None


# Admin fallback override: marks a teacher unavailable (reason != 'available')
# or reverts them to available, then (re)computes substitute assignments.
# Teacher self-reporting is the primary path (see /api/teacher/substitutes-today);
# this exists for teachers who can't check in themselves (e.g. unreachable/sick).
@router.post("/{schoolId}/substitutes")
def post_substitutes(schoolId: str, body: MarkUnavailableBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()

    if body.reason == "available":
        clear_teacher_absence(schoolId, body.teacherId, body.date, ac)
    else:
        apply_teacher_absence(schoolId, body.teacherId, body.date, body.reason, "admin", body.note, ac)

    return _build_payload(schoolId, body.date, ac)


class UpdateAssignmentBody(BaseModel):
    substitutionId: str
    substituteTeacherId: str
    date: str


# Manual override of a single substitution's assigned substitute teacher.
@router.patch("/{schoolId}/substitutes")
def patch_substitutes(schoolId: str, body: UpdateAssignmentBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    update_substitute_assignment(body.substitutionId, body.substituteTeacherId, ac)
    return _build_payload(schoolId, body.date, ac)


# ── Leave-request approval ──────────────────────────────────────────────────
# The teacher-submitted multi-day /leaves request sits as status="pending"
# (see teacher.py's create_pending_leave_request) until an admin acts here.
# Substitute computation — and the notifications that follow it — only happen
# at approval time, never at submission time.

def _pending_requests_payload(school_id: str, ac) -> dict:
    rows = fetch_pending_leave_requests(school_id, ac)
    teachers = fetch_school_teachers(school_id, ac)
    teacher_name = {t["id"]: t["name"] for t in teachers}
    return {"requests": [{**r, "teacherName": teacher_name.get(r["teacherId"], "Teacher")} for r in rows]}


@router.get("/{schoolId}/leave-requests")
def get_leave_requests(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return _pending_requests_payload(schoolId, ac)


class LeaveRequestDecisionBody(BaseModel):
    teacherId: str
    startDate: str
    endDate: str


@router.post("/{schoolId}/leave-requests/approve")
def approve_leave_request(schoolId: str, body: LeaveRequestDecisionBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    start = date.fromisoformat(body.startDate)
    end = date.fromisoformat(body.endDate)

    d = start
    while d <= end:
        date_str = d.isoformat()
        pending = (
            ac.table("teacher_availability").select("*")
            .eq("teacher_id", body.teacherId).eq("date", date_str).eq("status", "pending")
            .maybe_single().execute()
        )
        row = pending.data if pending else None
        if row:
            # Flipping the row to approved is what makes the leave effective, and
            # apply_teacher_absence publishes the resulting coverage — assignment
            # notices, stand-downs and uncovered-period alerts all included.
            apply_teacher_absence(
                schoolId, body.teacherId, date_str, row["reason"], "teacher", row.get("note"), ac, status="approved",
            )
        d += timedelta(days=1)

    create_notification(
        body.teacherId, "leave_approved",
        f"Your leave request ({body.startDate}{f' to {body.endDate}' if body.endDate != body.startDate else ''}) was approved.",
        ac,
    )
    return _pending_requests_payload(schoolId, ac)


@router.post("/{schoolId}/leave-requests/reject")
def reject_leave_request(schoolId: str, body: LeaveRequestDecisionBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    start = date.fromisoformat(body.startDate)
    end = date.fromisoformat(body.endDate)

    d = start
    while d <= end:
        ac.table("teacher_availability").update({"status": "rejected"}).eq("teacher_id", body.teacherId).eq("date", d.isoformat()).eq("status", "pending").execute()
        d += timedelta(days=1)

    create_notification(
        body.teacherId, "leave_rejected",
        f"Your leave request ({body.startDate}{f' to {body.endDate}' if body.endDate != body.startDate else ''}) was not approved.",
        ac,
    )
    return _pending_requests_payload(schoolId, ac)
