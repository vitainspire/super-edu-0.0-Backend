from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_school_teachers, fetch_school_classes, fetch_teacher_availability, fetch_substitutions_for_date,
    update_substitute_assignment, mark_teacher_unavailable, revert_teacher_availability, fetch_teacher_eligibility_data,
)
from ..lib.substitute_finder import suggest_swap
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
        revert_teacher_availability(body.teacherId, body.date, ac)
    else:
        mark_teacher_unavailable(schoolId, body.teacherId, body.date, body.reason, "admin", body.note, ac)

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
