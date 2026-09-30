import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import fetch_school_classes
from ..lib.generation_mode import recompute_grade_subject_mode
from ..deps import require_admin

router = APIRouter()


# GET /{schoolId}/grade-syllabus/generation-mode?grade=&subject= — always
# recomputes fresh (cheap, deterministic, no AI call) before returning, so an
# admin viewing this never sees a stale suggestion.
@router.get("/{schoolId}/grade-syllabus/generation-mode")
def get_generation_mode(schoolId: str, grade: Optional[str] = None, subject: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade or not subject:
        raise HTTPException(status_code=400, detail="grade and subject are required")
    ac = create_admin_client()
    mode = recompute_grade_subject_mode(ac, schoolId, grade, subject)
    row = (
        ac.table("grade_subject_feedback_profiles").select("generation_mode, mode_set_by, mode_updated_at")
        .eq("school_id", schoolId).eq("grade", grade).eq("subject", subject).maybe_single().execute()
    )
    data = row.data if row else None
    return {
        "mode": mode or "opt_in",
        "setBy": (data or {}).get("mode_set_by") or "auto",
        "updatedAt": (data or {}).get("mode_updated_at"),
    }


class GenerationModeBody(BaseModel):
    grade: str
    subject: str
    mode: str  # 'opt_in' | 'full_personalization'


# PATCH /{schoolId}/grade-syllabus/generation-mode — an admin's deliberate
# choice. Marked mode_set_by='admin' so recompute_grade_subject_mode never
# silently overwrites it again — from here on the auto-similarity check is
# advisory only for this grade+subject.
@router.patch("/{schoolId}/grade-syllabus/generation-mode")
def set_generation_mode(schoolId: str, body: GenerationModeBody, admin: dict = Depends(require_admin)):
    if body.mode not in ("opt_in", "full_personalization"):
        raise HTTPException(status_code=400, detail="mode must be opt_in or full_personalization")
    ac = create_admin_client()
    ac.table("grade_subject_feedback_profiles").upsert({
        "school_id": schoolId, "grade": body.grade, "subject": body.subject,
        "generation_mode": body.mode, "mode_set_by": "admin",
        "mode_updated_at": _iso_now(),
    }, on_conflict="school_id,grade,subject").execute()
    return {"ok": True}


def _iso_now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


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


# GET /{schoolId}/grade-syllabus/status?grade=&subject= — completion per
# SECTION, not one grade-wide verdict. is_completed lives on each section's
# own syllabus_topics row (the definition_id fan-out shares the topic
# definition, never its completion state), and sections of the same grade
# can genuinely be at different paces — collapsing that into a single
# "on track" answer would hide exactly the thing an admin needs to see
# (which section is falling behind), not just confirm a shaky average.
@router.get("/{schoolId}/grade-syllabus/status")
def get_grade_syllabus_status(schoolId: str, grade: Optional[str] = None, subject: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade:
        raise HTTPException(status_code=400, detail="grade is required")
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")

    ac = create_admin_client()
    sections = [c for c in fetch_school_classes(schoolId, ac) if c["grade"] == grade]
    if not sections:
        return {"sections": [], "totalTopics": 0}

    class_ids = [s["id"] for s in sections]
    rows = (
        ac.table("syllabus_topics").select("class_id, is_completed")
        .in_("class_id", class_ids).eq("subject", subject)
        .execute().data or []
    )
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(r["class_id"], []).append(r)

    total_topics = max((len(v) for v in by_class.values()), default=0)
    return {
        "sections": [
            {
                "classId": s["id"], "section": s["section"] or s["name"],
                "completed": sum(1 for r in by_class.get(s["id"], []) if r.get("is_completed")),
                "total": len(by_class.get(s["id"], [])),
            }
            for s in sections
        ],
        "totalTopics": total_topics,
    }


@router.get("/{schoolId}/grade-syllabus")
def get_grade_syllabus(schoolId: str, grade: Optional[str] = None, subject: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade:
        raise HTTPException(status_code=400, detail="grade is required")
    if not subject:
        raise HTTPException(status_code=400, detail="subject is required")

    ac = create_admin_client()
    # Scoped through classes, not syllabus_topics' own school_id — see the
    # comment on patch_grade_syllabus_progression below: that column is a
    # denormalised copy that a write path used to omit, so a query filtered on
    # it directly can miss real rows (or, here, the column doesn't even exist
    # on this deployment's syllabus_topics table).
    class_ids = [c["id"] for c in fetch_school_classes(schoolId, ac) if c["grade"] == grade]
    if not class_ids:
        return {"topics": []}

    data = (
        ac.table("syllabus_topics").select("*").in_("class_id", class_ids)
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
    class_ids = [s["id"] for s in sections]

    # Same class-scoping as the GET route above — syllabus_topics.school_id
    # is not a reliable (or, here, not even an existing) filter.
    existing = (
        ac.table("syllabus_topics").select("order_index").in_("class_id", class_ids)
        .eq("subject", subject).order("order_index", desc=True).limit(1).execute().data or []
    )
    next_index = (existing[0]["order_index"] if existing and existing[0].get("order_index") is not None else -1) + 1

    definition_id = str(uuid.uuid4())
    teacher_ids = [_find_class_teacher(s["id"], ac) for s in sections]

    rows = [
        {
            "id": str(uuid.uuid4()), "class_id": sec["id"], "teacher_id": teacher_ids[i],
            "grade": body.grade, "subject": subject, "definition_id": definition_id,
            "topic": topic, "description": (body.description or "").strip(), "week_number": body.weekNumber,
            "order_index": next_index, "is_completed": False,
        }
        for i, sec in enumerate(sections)
    ]
    ac.table("syllabus_topics").insert(rows).execute()
    return {"definitionId": definition_id}


class TopicProgressionItem(BaseModel):
    definitionId: str
    orderIndex: int
    weekNumber: Optional[int] = None


class TopicProgressionBody(BaseModel):
    grade: str
    subject: str
    topics: list[TopicProgressionItem]


@router.patch("/{schoolId}/grade-syllabus/progression")
def patch_grade_syllabus_progression(
    schoolId: str, body: TopicProgressionBody, admin: dict = Depends(require_admin)
):
    """Set the teaching order, and which week each topic belongs to.

    Until now order_index was only ever assigned at creation -- next_index on
    the way in, never changed again -- so the sequence a class is taught in was
    whatever order the topics happened to be added or extracted in. That
    sequence is not cosmetic: the teacher portal opens each lesson on the first
    incomplete topic of the current week, so the order here IS what teachers
    are told to teach next.

    Written against definition_id rather than row id. A topic exists once per
    section of the grade, and a progression that applied to one section would
    leave the others being taught in a different order.
    """
    subject = body.subject.strip()
    if not body.grade or not subject:
        raise HTTPException(status_code=400, detail="grade and subject are required")
    if not body.topics:
        return {"updated": 0}

    ac = create_admin_client()

    # Only touch topics that really belong to this grade+subject in this school:
    # definitionId arrives from the client, and an id from elsewhere would
    # otherwise let one grade's reorder rewrite another's.
    #
    # Ownership is derived through the CLASS rather than syllabus_topics'
    # own school_id. The class is the real link; the column on the topic is a
    # denormalised copy, and one write path used to omit it entirely, leaving
    # 128 rows that no tenant-scoped query could see. Going through classes
    # cannot be defeated that way.
    sections = [c for c in fetch_school_classes(schoolId, ac) if c["grade"] == body.grade]
    class_ids = [c["id"] for c in sections]
    if not class_ids:
        raise HTTPException(status_code=400, detail=f"No classes found for grade {body.grade}.")

    owned = (
        ac.table("syllabus_topics").select("definition_id")
        .in_("class_id", class_ids).eq("subject", subject)
        .execute().data or []
    )
    allowed = {r["definition_id"] for r in owned if r.get("definition_id")}

    updated = 0
    for item in body.topics:
        if item.definitionId not in allowed:
            continue
        ac.table("syllabus_topics").update(
            {"order_index": item.orderIndex, "week_number": item.weekNumber}
        ).eq("definition_id", item.definitionId).execute()
        updated += 1

    return {"updated": updated, "skipped": len(body.topics) - updated}


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
