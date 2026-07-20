import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import fetch_school_classes
from ..deps import require_admin

router = APIRouter()


def _find_class_teacher(class_id: str, ac) -> Optional[str]:
    """Same lookup order used by the per-class syllabus route: explicit assignment first, else timetable."""
    asg_res = ac.table("teacher_class_assignments").select("teacher_id").eq("class_id", class_id).limit(1).maybe_single().execute()
    teacher_id = (asg_res.data or {}).get("teacher_id") if asg_res else None
    if not teacher_id:
        stp_res = ac.table("school_timetable_periods").select("teacher_id").eq("class_id", class_id).limit(1).maybe_single().execute()
        teacher_id = (stp_res.data or {}).get("teacher_id") if stp_res else None
    return teacher_id


def _map_topic(r: dict) -> dict:
    return {
        "definitionId": r.get("definition_id"), "subject": r.get("subject") or "",
        "topic": r["topic"], "description": r.get("description") or "",
        "weekNumber": r.get("week_number"), "orderIndex": r.get("order_index") or 0,
    }


@router.get("/{schoolId}/grade-syllabus")
def get_grade_syllabus(schoolId: str, grade: Optional[str] = None, subject: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade:
        raise HTTPException(status_code=400, detail="grade is required")
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")

    ac = create_admin_client()
    data = (
        ac.table("syllabus_topics").select("*").eq("school_id", schoolId).eq("grade", grade)
        .eq("subject", subject).order("order_index").execute().data or []
    )

    by_def: dict[str, dict] = {}
    for r in data:
        def_id = r.get("definition_id") or r["id"]
        if def_id not in by_def:
            by_def[def_id] = {**r, "definition_id": def_id}

    topics = sorted((_map_topic(r) for r in by_def.values()), key=lambda t: t["orderIndex"])
    return {"topics": topics}


class GradeSyllabusTopicBody(BaseModel):
    grade: str
    subject: str
    topic: str
    description: Optional[str] = None
    weekNumber: Optional[int] = None


@router.post("/{schoolId}/grade-syllabus", status_code=201)
def post_grade_syllabus(schoolId: str, body: GradeSyllabusTopicBody, admin: dict = Depends(require_admin)):
    subject = body.subject.strip()
    topic = body.topic.strip()
    if not body.grade or not subject or not topic:
        raise HTTPException(status_code=400, detail="grade, subject and topic are required")

    ac = create_admin_client()
    all_classes = fetch_school_classes(schoolId, ac)
    sections = [c for c in all_classes if c["grade"] == body.grade]
    if not sections:
        raise HTTPException(status_code=400, detail=f"No classes found for grade {body.grade}.")

    existing = (
        ac.table("syllabus_topics").select("order_index").eq("school_id", schoolId).eq("grade", body.grade)
        .eq("subject", subject).order("order_index", desc=True).limit(1).execute().data or []
    )
    next_index = (existing[0]["order_index"] if existing and existing[0].get("order_index") is not None else -1) + 1

    definition_id = str(uuid.uuid4())
    teacher_ids = [_find_class_teacher(s["id"], ac) for s in sections]

    rows = [
        {
            "id": str(uuid.uuid4()), "class_id": sec["id"], "teacher_id": teacher_ids[i],
            "grade": body.grade, "subject": subject, "school_id": schoolId, "definition_id": definition_id,
            "topic": topic, "description": (body.description or "").strip(), "week_number": body.weekNumber,
            "order_index": next_index, "is_completed": False,
        }
        for i, sec in enumerate(sections)
    ]
    ac.table("syllabus_topics").insert(rows).execute()
    return {"definitionId": definition_id}


class DefinitionIdBody(BaseModel):
    definitionId: str


@router.delete("/{schoolId}/grade-syllabus")
def delete_grade_syllabus(schoolId: str, body: DefinitionIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    topics = ac.table("syllabus_topics").select("id").eq("definition_id", body.definitionId).execute().data or []
    topic_ids = [t["id"] for t in topics]
    if topic_ids:
        ac.table("syllabus_sub_topics").delete().in_("topic_id", topic_ids).execute()
    ac.table("syllabus_topics").delete().eq("definition_id", body.definitionId).execute()
    return {"ok": True}
