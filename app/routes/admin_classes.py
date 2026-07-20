import uuid
import random
import string
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_school_classes, admin_create_class, admin_delete_class,
    fetch_class_students, delete_student, bulk_insert_students,
    assign_teacher_to_class, remove_teacher_from_class, fetch_class_assignments,
)
from ..lib.student_code import gen_student_code
from ..deps import require_admin

router = APIRouter()


@router.get("/{schoolId}/classes")
def get_classes(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"classes": fetch_school_classes(schoolId, ac)}


class ClassBody(BaseModel):
    name: str
    grade: str
    section: str
    academicYear: Optional[str] = None


@router.post("/{schoolId}/classes")
def post_class(schoolId: str, body: ClassBody, admin: dict = Depends(require_admin)):
    if not body.name or not body.grade or not body.section:
        raise HTTPException(status_code=400, detail="name, grade, section required")

    ac = create_admin_client()
    dupe = (
        ac.table("classes").select("id").eq("school_id", schoolId).eq("grade", body.grade)
        .ilike("section", body.section).maybe_single().execute()
    )
    if dupe.data if dupe else None:
        raise HTTPException(status_code=409, detail=f"Grade {body.grade} Section {body.section} already exists")

    school_res = ac.table("schools").select("name").eq("id", schoolId).maybe_single().execute()
    school_name = (school_res.data or {}).get("name", "") if school_res else ""
    class_code = "".join(random.choices(string.ascii_uppercase + string.digits, k=6))
    now = datetime.now(timezone.utc).isoformat()

    new_class = {
        "id": str(uuid.uuid4()), "teacherId": admin["id"], "schoolName": school_name, "schoolId": schoolId,
        "name": body.name, "grade": body.grade, "section": body.section,
        "academicYear": body.academicYear or str(datetime.now(timezone.utc).year), "createdAt": now, "classCode": class_code,
    }
    try:
        admin_create_class(new_class, ac)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"class": new_class}


class ClassIdBody(BaseModel):
    classId: str


@router.delete("/{schoolId}/classes")
def delete_class(schoolId: str, body: ClassIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    admin_delete_class(body.classId, ac)
    return {"ok": True}


@router.get("/{schoolId}/classes/{classId}/students")
def get_class_students(schoolId: str, classId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"students": fetch_class_students(classId, ac)}


class DeleteStudentBody(BaseModel):
    studentId: str


@router.delete("/{schoolId}/classes/{classId}/students")
def delete_class_student(schoolId: str, classId: str, body: DeleteStudentBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    try:
        delete_student(body.studentId, ac)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


class BulkStudent(BaseModel):
    name: str
    rollNumber: str


class BulkStudentsBody(BaseModel):
    students: list[BulkStudent]


def _unique_student_code(ac) -> str:
    for _ in range(10):
        code = gen_student_code()
        existing = ac.table("students").select("id").eq("student_code", code).maybe_single().execute()
        if not (existing.data if existing else None):
            return code
    return gen_student_code()


@router.post("/{schoolId}/classes/{classId}/students/bulk")
def bulk_add_students(schoolId: str, classId: str, body: BulkStudentsBody, admin: dict = Depends(require_admin)):
    if not body.students:
        raise HTTPException(status_code=400, detail="students array required")

    ac = create_admin_client()
    # Sequential code generation avoids concurrent duplicate-check races.
    rows = [{"id": str(uuid.uuid4()), "name": s.name, "rollNumber": s.rollNumber, "studentCode": _unique_student_code(ac)} for s in body.students]

    try:
        bulk_insert_students(classId, rows, ac)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    return {"inserted": len(rows), "students": [{"name": r["name"], "studentCode": r["studentCode"]} for r in rows]}


@router.get("/{schoolId}/classes/{classId}/assign-teacher")
def get_assignments(schoolId: str, classId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"assignments": fetch_class_assignments(classId, ac)}


class AssignTeacherBody(BaseModel):
    teacherId: str
    subject: Optional[str] = None


@router.post("/{schoolId}/classes/{classId}/assign-teacher")
def post_assign_teacher(schoolId: str, classId: str, body: AssignTeacherBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    assign_teacher_to_class(str(uuid.uuid4()), body.teacherId, classId, ac, body.subject)
    return {"ok": True}


class RemoveTeacherBody(BaseModel):
    teacherId: str


@router.delete("/{schoolId}/classes/{classId}/assign-teacher")
def delete_assign_teacher(schoolId: str, classId: str, body: RemoveTeacherBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    remove_teacher_from_class(body.teacherId, classId, ac)
    return {"ok": True}
