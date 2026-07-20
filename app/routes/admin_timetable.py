import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_school_timetable, upsert_school_timetable_period, delete_school_timetable_period,
    publish_timetable_for_classes, fetch_school_schedule, fetch_all_grade_subjects, fetch_grade_subjects,
    fetch_school_classes, fetch_school_teachers, fetch_class_assignments,
    delete_school_timetable_periods_for_classes, bulk_insert_school_timetable_periods,
)
from ..lib.academic_calendar import SIX_DAY_WEEK, FIVE_DAY_WEEK
from ..lib.timetable_generator import generate_school_timetable
from ..lib.timetable_shuffle import generate_shuffled_timetable
from ..deps import require_admin

router = APIRouter()

DAY_NAMES = ["", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"]


@router.get("/{schoolId}/timetable")
def get_timetable(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"periods": fetch_school_timetable(schoolId, ac)}


class PeriodBody(BaseModel):
    id: Optional[str] = None
    dayOfWeek: int
    periodNumber: int
    startTime: str
    endTime: str
    classId: str
    teacherId: Optional[str] = None
    label: Optional[str] = None


@router.post("/{schoolId}/timetable")
def post_period(schoolId: str, body: PeriodBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()

    # Conflict check: same teacher already assigned to a different class at this day + period.
    if body.teacherId:
        conflict_res = (
            ac.table("school_timetable_periods").select("id, class_id, start_time, end_time")
            .eq("school_id", schoolId).eq("teacher_id", body.teacherId)
            .eq("day_of_week", body.dayOfWeek).eq("period_number", body.periodNumber)
            .neq("class_id", body.classId).maybe_single().execute()
        )
        conflict = conflict_res.data if conflict_res else None
        if conflict:
            cls_res = ac.table("classes").select("name").eq("id", conflict["class_id"]).maybe_single().execute()
            class_name = (cls_res.data or {}).get("name", "another class") if cls_res else "another class"
            day_name = DAY_NAMES[body.dayOfWeek] if 0 <= body.dayOfWeek < len(DAY_NAMES) else f"Day {body.dayOfWeek}"
            raise HTTPException(
                status_code=409,
                detail=f"This teacher is already assigned to {class_name} during Period {body.periodNumber} "
                       f"({conflict['start_time']}–{conflict['end_time']}) on {day_name}.",
            )

    period = {
        "id": body.id or str(uuid.uuid4()), "schoolId": schoolId, "dayOfWeek": body.dayOfWeek,
        "periodNumber": body.periodNumber, "startTime": body.startTime, "endTime": body.endTime,
        "classId": body.classId, "teacherId": body.teacherId, "label": body.label,
        "createdAt": datetime.now(timezone.utc).isoformat(),
    }
    upsert_school_timetable_period(period, ac)
    return {"period": period}


class PeriodIdBody(BaseModel):
    periodId: str


@router.delete("/{schoolId}/timetable")
def delete_period(schoolId: str, body: PeriodIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_school_timetable_period(body.periodId, ac)
    return {"ok": True}


class PublishBody(BaseModel):
    classIds: Optional[list[str]] = None


@router.post("/{schoolId}/timetable/publish")
def publish_timetable(schoolId: str, body: PublishBody = PublishBody(), admin: dict = Depends(require_admin)):
    # Only the per-class scoped publish is ported — the whole-school variant
    # (no classIds) did one all-or-nothing insert across every class, where a
    # single stale/orphaned row anywhere blocked every class's publish (see
    # the two bugs fixed in the Next.js version). The frontend's Publish
    # button now always sends classIds, so that path is what's actually used.
    ac = create_admin_client()
    if not body.classIds:
        raise HTTPException(status_code=400, detail="classIds required")
    count = publish_timetable_for_classes(body.classIds, ac)
    return {"ok": True, "published": count}


class GenerateBody(BaseModel):
    sixDayWeek: Optional[bool] = None


# POST /{schoolId}/timetable/generate — generates the ENTIRE school's
# timetable in one pass (every grade, every section), so teachers who cover
# multiple grades/subjects are scheduled as one shared resource instead of
# per-grade in isolation.
#
# STABLE, not a blind wipe-and-rebuild: any already-published period whose
# (class, subject, teacher) still matches what's currently required is kept
# in its exact slot. Only new/changed requirements get freshly placed — so
# re-running this after one small change doesn't reshuffle the whole school.
@router.post("/{schoolId}/timetable/generate")
def generate_timetable(schoolId: str, body: GenerateBody = GenerateBody(), admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    working_weekdays = FIVE_DAY_WEEK if body.sixDayWeek is False else SIX_DAY_WEEK

    schedule = fetch_school_schedule(schoolId, ac)
    lineup = fetch_all_grade_subjects(schoolId, ac)
    classes = fetch_school_classes(schoolId, ac)
    teachers = fetch_school_teachers(schoolId, ac)
    existing_timetable = fetch_school_timetable(schoolId, ac)

    if not schedule or len([s for s in schedule["slots"] if s.get("type") == "period"]) == 0:
        raise HTTPException(status_code=400, detail="Set up the school schedule template first.")
    if not lineup:
        raise HTTPException(status_code=400, detail="Add at least one subject to a grade's lineup first.")
    if not classes:
        raise HTTPException(status_code=400, detail="No classes found for this school.")

    assignments = []
    for c in classes:
        for a in fetch_class_assignments(c["id"], ac):
            if a.get("subject"):
                assignments.append({"classId": c["id"], "subject": a["subject"], "teacherId": a["teacherId"]})

    class_ids = {c["id"] for c in classes}
    existing_periods = [
        {
            "classId": p["classId"], "dayOfWeek": p["dayOfWeek"], "periodNumber": p["periodNumber"],
            "startTime": p["startTime"], "endTime": p["endTime"], "teacherId": p.get("teacherId"), "label": p.get("label") or "",
        }
        for p in existing_timetable if p["classId"] in class_ids
    ]

    result = generate_school_timetable(
        schedule["slots"],
        working_weekdays,
        [{"classId": c["id"], "className": c["name"], "grade": c["grade"]} for c in classes],
        [{"grade": l["grade"], "subject": l["subject"], "periodsPerWeek": l["periodsPerWeek"], "category": l["category"]} for l in lineup],
        assignments,
        [{"teacherId": t["id"], "teacherName": t["name"], "maxPeriodsPerDay": t.get("maxPeriodsPerDay"), "maxPeriodsPerWeek": t.get("maxPeriodsPerWeek")} for t in teachers],
        existing_periods,
    )

    delete_school_timetable_periods_for_classes([c["id"] for c in classes], ac)

    try:
        bulk_insert_school_timetable_periods(
            [
                {
                    "id": str(uuid.uuid4()), "schoolId": schoolId, "dayOfWeek": p["dayOfWeek"],
                    "periodNumber": p["periodNumber"], "startTime": p["startTime"], "endTime": p["endTime"],
                    "classId": p["classId"], "teacherId": p.get("teacherId"), "label": p["label"],
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                }
                for p in result["periods"]
            ],
            ac,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {
        "ok": True,
        "classStats": result["classStats"],
        "teacherWarnings": result["teacherWarnings"],
        "unplaced": result["unplaced"],
        "keptCount": result["keptCount"],
        "placedCount": result["placedCount"],
    }


class ShuffleBody(BaseModel):
    grade: str


@router.post("/{schoolId}/timetable/shuffle")
def shuffle_timetable(schoolId: str, body: ShuffleBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()

    schedule = fetch_school_schedule(schoolId, ac)
    lineup = fetch_grade_subjects(schoolId, body.grade, ac)
    all_classes = fetch_school_classes(schoolId, ac)
    all_periods = fetch_school_timetable(schoolId, ac)

    if not schedule or len([s for s in schedule["slots"] if s.get("type") == "period"]) == 0:
        raise HTTPException(status_code=400, detail="Set up the school schedule template first.")
    if not lineup:
        raise HTTPException(status_code=400, detail="Add at least one subject to this grade's lineup first.")

    sections_of_grade = [c for c in all_classes if c["grade"] == body.grade]
    if not sections_of_grade:
        raise HTTPException(status_code=400, detail=f"No classes found for grade {body.grade}.")

    section_class_ids = {c["id"] for c in sections_of_grade}

    sections = []
    for c in sections_of_grade:
        subject_teacher = {a["subject"]: a["teacherId"] for a in fetch_class_assignments(c["id"], ac) if a.get("subject")}
        sections.append({"classId": c["id"], "className": c["name"], "subjectTeacher": subject_teacher})

    # Teacher slots already booked by classes NOT part of this reshuffle
    busy_teacher_slots = {
        f"{p['teacherId']}|{p['dayOfWeek']}|{p['periodNumber']}"
        for p in all_periods if p.get("teacherId") and p["classId"] not in section_class_ids
    }

    result = generate_shuffled_timetable(
        schedule["slots"],
        sections,
        [{"subject": l["subject"], "periodsPerWeek": l["periodsPerWeek"]} for l in lineup],
        busy_teacher_slots,
    )

    delete_school_timetable_periods_for_classes(list(section_class_ids), ac)

    try:
        bulk_insert_school_timetable_periods(
            [
                {
                    "id": str(uuid.uuid4()), "schoolId": schoolId, "dayOfWeek": p["dayOfWeek"],
                    "periodNumber": p["periodNumber"], "startTime": p["startTime"], "endTime": p["endTime"],
                    "classId": p["classId"], "teacherId": p.get("teacherId"), "label": p["label"],
                    "createdAt": datetime.now(timezone.utc).isoformat(),
                }
                for p in result["periods"]
            ],
            ac,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "sections": result["sectionStats"]}
