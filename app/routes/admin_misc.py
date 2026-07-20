import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_exam_plan_items, upsert_exam_plan_item, delete_exam_plan_item,
    fetch_school_announcements, create_announcement, delete_announcement,
    fetch_school_teachers, fetch_school_classes, fetch_school_schedule, upsert_school_schedule,
)
from ..lib.schemas import AnnouncementSchema
from ..deps import require_admin

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Exam plan ─────────────────────────────────────────────────────────────────

@router.get("/{schoolId}/exam-plan")
def get_exam_plan(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"items": fetch_exam_plan_items(schoolId, ac)}


class ExamPlanItemBody(BaseModel):
    id: Optional[str] = None
    name: str
    count: Optional[float] = None
    orderIndex: Optional[int] = None


@router.post("/{schoolId}/exam-plan")
def post_exam_plan_item(schoolId: str, body: ExamPlanItemBody, admin: dict = Depends(require_admin)):
    if not body.name.strip():
        raise HTTPException(status_code=400, detail="name is required")

    ac = create_admin_client()
    order_index = body.orderIndex
    if order_index is None:
        order_index = len(fetch_exam_plan_items(schoolId, ac))

    item = {
        "id": body.id or str(uuid.uuid4()), "schoolId": schoolId, "name": body.name.strip(),
        "count": max(1, int(body.count)) if isinstance(body.count, (int, float)) else 1,
        "orderIndex": order_index, "createdAt": _now(),
    }
    upsert_exam_plan_item(item, ac)
    return {"item": item}


class DeleteIdBody(BaseModel):
    id: str


@router.delete("/{schoolId}/exam-plan")
def delete_exam_plan_item_route(schoolId: str, body: DeleteIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_exam_plan_item(body.id, ac)
    return {"ok": True}


# ─── Scanners ──────────────────────────────────────────────────────────────────

@router.get("/{schoolId}/scanners")
def get_scanners(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    data = (
        ac.table("scanner_profiles").select("id, name, email, created_at").eq("school_id", schoolId)
        .order("created_at", desc=False).execute().data or []
    )
    return {"scanners": data}


class ScannerBody(BaseModel):
    name: str
    email: str
    password: str


@router.post("/{schoolId}/scanners")
def post_scanner(schoolId: str, body: ScannerBody, admin: dict = Depends(require_admin)):
    if not body.name.strip() or not body.email.strip() or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Name, email and password (min 6 chars) are required.")

    ac = create_admin_client()
    try:
        auth_res = ac.auth.admin.create_user({
            "email": body.email.strip().lower(), "password": body.password, "email_confirm": True,
            "user_metadata": {"name": body.name.strip(), "role": "scanner"},
        })
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    scanner_id = auth_res.user.id
    school_res = ac.table("schools").select("name").eq("id", schoolId).maybe_single().execute()
    school_name = (school_res.data or {}).get("name", "") if school_res else ""

    try:
        ac.table("scanner_profiles").insert({
            "user_id": scanner_id, "school_id": schoolId, "name": body.name.strip(), "email": body.email.strip().lower(),
        }).execute()
    except Exception as e:
        ac.auth.admin.delete_user(scanner_id)
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True, "schoolName": school_name}


class ScannerIdBody(BaseModel):
    scannerId: str


@router.delete("/{schoolId}/scanners")
def delete_scanner(schoolId: str, body: ScannerIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    profile_res = ac.table("scanner_profiles").select("user_id").eq("id", body.scannerId).eq("school_id", schoolId).maybe_single().execute()
    profile = profile_res.data if profile_res else None

    ac.table("scanner_profiles").delete().eq("id", body.scannerId).execute()
    if profile and profile.get("user_id"):
        ac.auth.admin.delete_user(profile["user_id"])
    return {"ok": True}


# ─── Overview ──────────────────────────────────────────────────────────────────

@router.get("/{schoolId}/overview")
def get_overview(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    teachers = fetch_school_teachers(schoolId, ac)
    classes = fetch_school_classes(schoolId, ac)

    class_ids = [c["id"] for c in classes]
    students = ac.table("students").select("id").in_("class_id", class_ids).execute().data if class_ids else []
    timetable = ac.table("timetable").select("teacher_id").in_("class_id", class_ids).execute().data if class_ids else []

    total_periods = len(timetable)
    published_periods = len([t for t in timetable if t.get("teacher_id")])
    timetable_coverage = round((published_periods / total_periods) * 100) if total_periods > 0 else 0

    return {
        "teacherCount": len(teachers), "classCount": len(classes), "studentCount": len(students or []),
        "timetableCoverage": timetable_coverage, "totalPeriods": total_periods,
    }


# ─── Announcements ──────────────────────────────────────────────────────────────

@router.get("/{schoolId}/announcements")
def get_announcements(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"announcements": fetch_school_announcements(schoolId, ac)}


@router.post("/{schoolId}/announcements", status_code=201)
def post_announcement(schoolId: str, body: AnnouncementSchema, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    announcement = {
        "id": str(uuid.uuid4()), "schoolId": schoolId, "adminId": admin["id"], "adminName": admin["name"],
        "title": body.title, "body": body.body, "category": body.category or "general", "createdAt": _now(),
    }
    create_announcement(announcement, ac)
    return {"announcement": announcement}


@router.delete("/{schoolId}/announcements")
def delete_announcement_route(schoolId: str, body: DeleteIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_announcement(body.id, schoolId, ac)
    return {"ok": True}


# ─── School schedule template ────────────────────────────────────────────────

@router.get("/{schoolId}/schedule")
def get_schedule(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"schedule": fetch_school_schedule(schoolId, ac)}


class ScheduleBody(BaseModel):
    slots: list[dict]


@router.post("/{schoolId}/schedule")
def post_schedule(schoolId: str, body: ScheduleBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    existing = fetch_school_schedule(schoolId, ac)
    schedule = {
        "id": (existing or {}).get("id") or str(uuid.uuid4()), "schoolId": schoolId,
        "slots": body.slots, "createdAt": (existing or {}).get("createdAt") or _now(),
    }
    try:
        upsert_school_schedule(schedule, ac)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"schedule": schedule}
