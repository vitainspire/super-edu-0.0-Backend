import random
import string
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client, retry_supabase
from ..lib.admin_queries import (
    fetch_school_teachers, remove_teacher_from_school,
    update_teacher_workload_limits, update_teacher_subjects,
)
from ..deps import require_admin

router = APIRouter()

CODE_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def _gen_code(length: int = 6) -> str:
    return "".join(random.choices(CODE_CHARS, k=length))


@router.get("/{schoolId}/teachers")
def get_teachers(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"teachers": fetch_school_teachers(schoolId, ac)}


class TeacherBody(BaseModel):
    name: str
    email: str
    password: str
    subject: Optional[str] = None
    subjects: Optional[list[str]] = None


@router.post("/{schoolId}/teachers")
def post_teacher(schoolId: str, body: TeacherBody, admin: dict = Depends(require_admin)):
    if not body.name.strip() or not body.email.strip() or len(body.password) < 6:
        raise HTTPException(status_code=400, detail="Name, email and password (min 6 chars) are required.")

    subject_list = (
        [s.strip() for s in body.subjects if s.strip()] if body.subjects
        else ([body.subject.strip()] if body.subject and body.subject.strip() else [])
    )

    ac = create_admin_client()
    try:
        auth_res = retry_supabase(lambda: ac.auth.admin.create_user({
            "email": body.email.strip().lower(), "password": body.password, "email_confirm": True,
            "user_metadata": {"name": body.name.strip(), "role": "teacher"},
        }))
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))

    teacher_id = auth_res.user.id
    school_res = retry_supabase(lambda: ac.table("schools").select("name").eq("id", schoolId).maybe_single().execute())
    school_name = (school_res.data or {}).get("name", "") if school_res else ""

    try:
        retry_supabase(lambda: ac.table("teachers").insert({
            "id": teacher_id, "user_id": teacher_id, "name": body.name.strip(),
            "school_name": school_name, "school_id": schoolId,
            "subject": subject_list[0] if subject_list else "", "subjects": subject_list,
            "grade": "", "phone": "", "language_preference": "english", "teacher_code": _gen_code(),
        }).execute())
    except Exception as e:
        # Best-effort: if even the retried cleanup fails, the auth user is
        # left orphaned (no teachers row) rather than the account silently
        # existing with no way to sign in -- the next attempt with this same
        # email hits "already registered" and needs manual cleanup.
        try:
            retry_supabase(lambda: ac.auth.admin.delete_user(teacher_id))
        except Exception:
            pass
        raise HTTPException(status_code=500, detail=str(e))

    return {"ok": True}


class TeacherPatchBody(BaseModel):
    teacherId: str
    maxPeriodsPerDay: Optional[int] = None
    maxPeriodsPerWeek: Optional[int] = None
    subjects: Optional[list[str]] = None


@router.patch("/{schoolId}/teachers")
def patch_teacher(schoolId: str, body: TeacherPatchBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    if body.subjects is not None:
        update_teacher_subjects(body.teacherId, [s.strip() for s in body.subjects if s.strip()], ac)
    if "maxPeriodsPerDay" in body.model_fields_set or "maxPeriodsPerWeek" in body.model_fields_set:
        update_teacher_workload_limits(body.teacherId, body.maxPeriodsPerDay, body.maxPeriodsPerWeek, ac)
    return {"ok": True}


class TeacherIdBody(BaseModel):
    teacherId: str


@router.delete("/{schoolId}/teachers")
def delete_teacher(schoolId: str, body: TeacherIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    remove_teacher_from_school(body.teacherId, ac)
    return {"ok": True}
