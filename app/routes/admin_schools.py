import uuid
from datetime import datetime, timezone
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import (
    fetch_grade_subjects,
    upsert_grade_subject,
    delete_grade_subject,
    fetch_academic_events,
    upsert_academic_event,
    delete_academic_event,
    publish_academic_events,
)
from ..lib.schemas import AcademicEventSchema, SeedHolidaysSchema
from ..lib.academic_calendar import compute_subject_session_availability
from ..lib.indian_holidays import get_indian_holidays_for_year
from ..lib.indian_festivals import get_major_festival_suggestions
from ..deps import require_admin

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ─── Grade subjects ───────────────────────────────────────────────────────────

@router.get("/{schoolId}/grade-subjects")
def get_grade_subjects(schoolId: str, grade: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade:
        raise HTTPException(status_code=400, detail="grade is required")
    ac = create_admin_client()
    subjects = fetch_grade_subjects(schoolId, grade, ac)
    return {"subjects": subjects}


class GradeSubjectBody(BaseModel):
    id: Optional[str] = None
    grade: str
    subject: str
    periodsPerWeek: Optional[int] = None
    category: Optional[str] = None
    orderIndex: Optional[int] = None


@router.post("/{schoolId}/grade-subjects")
def post_grade_subject(schoolId: str, body: GradeSubjectBody, admin: dict = Depends(require_admin)):
    if not body.grade or not body.subject.strip():
        raise HTTPException(status_code=400, detail="grade and subject are required")

    ac = create_admin_client()
    order_index = body.orderIndex
    if order_index is None:
        existing = fetch_grade_subjects(schoolId, body.grade, ac)
        order_index = len(existing)

    subject = {
        "id": body.id or str(uuid.uuid4()),
        "schoolId": schoolId,
        "grade": body.grade,
        "subject": body.subject.strip(),
        "periodsPerWeek": body.periodsPerWeek if isinstance(body.periodsPerWeek, int) else 0,
        "category": "special" if body.category == "special" else "core",
        "orderIndex": order_index,
        "createdAt": _now(),
    }
    upsert_grade_subject(subject, ac)
    return {"subject": subject}


class DeleteIdBody(BaseModel):
    id: str


@router.delete("/{schoolId}/grade-subjects")
def delete_grade_subject_route(schoolId: str, body: DeleteIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_grade_subject(body.id, ac)
    return {"ok": True}


# ─── Academic events (calendar) ───────────────────────────────────────────────

@router.get("/{schoolId}/academic-events")
def get_academic_events(schoolId: str, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    return {"events": fetch_academic_events(schoolId, ac)}


class AcademicEventBody(AcademicEventSchema):
    id: Optional[str] = None


@router.post("/{schoolId}/academic-events", status_code=201)
def post_academic_event(schoolId: str, body: AcademicEventBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()

    published = False
    if body.id:
        existing = fetch_academic_events(schoolId, ac)
        match = next((e for e in existing if e["id"] == body.id), None)
        published = match["published"] if match else False

    event = {
        "id": body.id or str(uuid.uuid4()),
        "schoolId": schoolId,
        "title": body.title,
        "category": body.category,
        "holidaySubtype": body.holidaySubtype,
        "countsAsNonWorking": body.countsAsNonWorking if body.countsAsNonWorking is not None else True,
        "published": published,
        "startDate": body.startDate,
        "endDate": body.endDate,
        "description": body.description,
        "createdAt": _now(),
    }
    upsert_academic_event(event, ac)
    return {"event": event}


@router.delete("/{schoolId}/academic-events")
def delete_academic_event_route(schoolId: str, body: DeleteIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    delete_academic_event(body.id, schoolId, ac)
    return {"ok": True}


@router.post("/{schoolId}/academic-events/publish")
def publish_academic_events_route(schoolId: str, admin: dict = Depends(require_admin)):
    """Makes every current draft event visible to teachers in one deliberate action."""
    ac = create_admin_client()
    published_count = publish_academic_events(schoolId, ac)
    return {"ok": True, "publishedCount": published_count}


@router.post("/{schoolId}/academic-events/seed-holidays")
def seed_holidays(schoolId: str, body: SeedHolidaysSchema, admin: dict = Depends(require_admin)):
    """Fills in India's public holidays for a given year (computed, not
    AI-guessed) as draft 'holiday' events. Skips any date already covered by
    an existing holiday event for this school, so it's safe to run twice."""
    ac = create_admin_client()
    existing = fetch_academic_events(schoolId, ac)
    existing_holiday_dates = {e["startDate"] for e in existing if e["category"] == "holiday"}

    all_holidays = get_indian_holidays_for_year(body.year)
    holidays_to_add = [h for h in all_holidays if h["startDate"] not in existing_holiday_dates]

    created = []
    for h in holidays_to_add:
        event = {
            "id": str(uuid.uuid4()), "schoolId": schoolId, "title": h["title"], "category": "holiday",
            "holidaySubtype": "public", "countsAsNonWorking": True, "published": False,
            "startDate": h["startDate"], "endDate": h["endDate"], "description": None, "createdAt": _now(),
        }
        upsert_academic_event(event, ac)
        created.append(event)

    # Festival dates are best-effort suggestions, never auto-inserted — the
    # admin confirms individually, excluding anything already on the calendar.
    existing_title_dates = {f"{e['title']}|{e['startDate']}" for e in existing}
    suggested = [f for f in get_major_festival_suggestions(body.year) if f"{f['title']}|{f['date']}" not in existing_title_dates]

    return {
        "created": created,
        "skipped": len(all_holidays) - len(holidays_to_add),
        "suggested": suggested,
    }


# ─── Syllabus (admin-authored, fanned out per section via definitionId) ──────

def _grade_class_ids(ac, school_id: str, grade: str) -> list[str]:
    res = ac.table("classes").select("id").eq("school_id", school_id).eq("grade", grade).execute()
    return [c["id"] for c in (res.data or [])]


@router.get("/{schoolId}/syllabus")
def get_syllabus(schoolId: str, grade: Optional[str] = None, subject: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade or not subject:
        raise HTTPException(status_code=400, detail="grade and subject required")

    ac = create_admin_client()
    class_ids = _grade_class_ids(ac, schoolId, grade)
    if not class_ids:
        return {"topics": []}

    # Exact subject match only. This used to also match subject IS NULL and
    # silently tag every untagged topic for the class with whatever subject
    # was opened first — that assumed a class only ever had one untagged
    # subject's worth of legacy topics, which isn't true in general (a class
    # holds every subject's topics, distinguished only by this column) and
    # led to real mislabeling (e.g. an EVS syllabus permanently retagged as
    # Mathematics just because Mathematics was viewed first). Untagged
    # legacy topics now need a deliberate, explicit fix rather than an
    # automatic guess.
    res = (
        ac.table("syllabus_topics")
        .select("*")
        .in_("class_id", class_ids)
        .eq("subject", subject)
        .order("order_index")
        .execute()
    )
    data = res.data or []

    seen: set[str] = set()
    topics = []
    for t in data:
        key = t.get("definition_id") or t["id"]
        if key in seen:
            continue
        seen.add(key)
        topics.append(
            {
                "id": t["id"],
                "definitionId": t.get("definition_id") or t["id"],
                "subject": subject,
                "topic": t["topic"],
                "description": t.get("description") or "",
                "weekNumber": t.get("week_number"),
                "orderIndex": t.get("order_index") or 0,
                "estimatedSessions": t.get("estimated_sessions"),
                "prerequisiteDefinitionId": t.get("prerequisite_definition_id"),
                "wasLegacy": False,
            }
        )
    return {"topics": topics}


@router.get("/{schoolId}/syllabus-overview")
def get_syllabus_overview(schoolId: str, admin: dict = Depends(require_admin)):
    """All saved syllabus for the school, grouped grade → subject → topics(+subtopics).
    Powers the admin Syllabus tab's per-grade cards + neat read view. Only grades
    that actually have saved topics appear."""
    ac = create_admin_client()
    classes = ac.table("classes").select("id").eq("school_id", schoolId).execute().data or []
    class_ids = [c["id"] for c in classes]
    if not class_ids:
        return {"grades": []}

    topic_rows = (
        ac.table("syllabus_topics").select("*").in_("class_id", class_ids).order("order_index").execute().data or []
    )
    if not topic_rows:
        return {"grades": []}

    # Topics are fanned out one row per section, sharing definition_id — dedup on it.
    topic_by_def: dict[str, dict] = {}
    topic_ids_by_def: dict[str, list[str]] = {}
    for t in topic_rows:
        def_id = t.get("definition_id") or t["id"]
        topic_ids_by_def.setdefault(def_id, []).append(t["id"])
        topic_by_def.setdefault(def_id, t)

    all_topic_ids = [tid for ids in topic_ids_by_def.values() for tid in ids]
    topicid_to_def = {tid: def_id for def_id, ids in topic_ids_by_def.items() for tid in ids}

    sub_rows = (
        ac.table("syllabus_sub_topics").select("*").in_("topic_id", all_topic_ids).order("order_index").execute().data or []
        if all_topic_ids else []
    )
    subs_by_topicdef: dict[str, list[str]] = {}
    seen_sub: dict[str, set] = {}
    for s in sub_rows:
        parent_def = topicid_to_def.get(s.get("topic_id"))
        if not parent_def:
            continue
        key = s.get("definition_id") or s["id"]
        seen = seen_sub.setdefault(parent_def, set())
        if key in seen:
            continue
        seen.add(key)
        subs_by_topicdef.setdefault(parent_def, []).append(s.get("name") or "")

    # Exercises + sidebars (PDF-extracted content, fanned per section like subtopics —
    # dedup on their own definition_id) and cross-topic dependency edges (grade+subject
    # scoped, not fanned) — all optional: manually-authored topics simply have none.
    ex_rows = (
        ac.table("syllabus_exercises").select("topic_id, definition_id").in_("topic_id", all_topic_ids).execute().data or []
        if all_topic_ids else []
    )
    ex_count_by_def: dict[str, int] = {}
    seen_ex: dict[str, set] = {}
    for e in ex_rows:
        parent_def = topicid_to_def.get(e.get("topic_id"))
        if not parent_def:
            continue
        key = e.get("definition_id")
        seen = seen_ex.setdefault(parent_def, set())
        if key in seen:
            continue
        seen.add(key)
        ex_count_by_def[parent_def] = ex_count_by_def.get(parent_def, 0) + 1

    sb_rows = (
        ac.table("syllabus_sidebars").select("topic_id, definition_id").in_("topic_id", all_topic_ids).execute().data or []
        if all_topic_ids else []
    )
    sb_count_by_def: dict[str, int] = {}
    seen_sb: dict[str, set] = {}
    for sb in sb_rows:
        parent_def = topicid_to_def.get(sb.get("topic_id"))
        if not parent_def:
            continue
        key = sb.get("definition_id")
        seen = seen_sb.setdefault(parent_def, set())
        if key in seen:
            continue
        seen.add(key)
        sb_count_by_def[parent_def] = sb_count_by_def.get(parent_def, 0) + 1

    dep_rows = (
        ac.table("syllabus_dependencies").select("from_definition_id, to_definition_id")
        .in_("from_definition_id", list(topic_by_def.keys())).execute().data or []
        if topic_by_def else []
    )
    prereqs_by_def: dict[str, list[str]] = {}
    for d in dep_rows:
        prereq_topic = topic_by_def.get(d.get("to_definition_id"))
        if not prereq_topic:
            continue
        prereqs_by_def.setdefault(d["from_definition_id"], []).append(prereq_topic.get("topic") or "")

    # grade -> subject -> [topics]
    grades: dict[str, dict[str, list]] = {}
    for def_id, t in topic_by_def.items():
        grade = t.get("grade") or ""
        subject = t.get("subject") or "General"
        grades.setdefault(grade, {}).setdefault(subject, []).append({
            "definitionId": def_id,
            "topic": t.get("topic"),
            "description": t.get("description") or "",
            "weekNumber": t.get("week_number"),
            "orderIndex": t.get("order_index") or 0,
            "subtopics": [n for n in subs_by_topicdef.get(def_id, []) if n],
            "exerciseCount": ex_count_by_def.get(def_id, 0),
            "sidebarCount": sb_count_by_def.get(def_id, 0),
            "prerequisites": [n for n in prereqs_by_def.get(def_id, []) if n],
        })

    def grade_key(g: str):
        return (int(g), "") if g.isdigit() else (10_000, g)

    result = []
    for grade in sorted(grades, key=grade_key):
        subjects = [
            {
                "subject": subject,
                "topicCount": len(topics),
                "topics": sorted(topics, key=lambda t: t["orderIndex"]),
            }
            for subject, topics in sorted(grades[grade].items())
        ]
        result.append({"grade": grade, "subjects": subjects})
    return {"grades": result}


class SyllabusTopicBody(BaseModel):
    grade: str
    subject: str
    topic: str
    description: Optional[str] = None
    weekNumber: Optional[int] = None


@router.post("/{schoolId}/syllabus")
def post_syllabus_topic(schoolId: str, body: SyllabusTopicBody, admin: dict = Depends(require_admin)):
    topic = body.topic.strip()
    if not body.grade or not body.subject or not topic:
        raise HTTPException(status_code=400, detail="grade, subject, and topic required")

    ac = create_admin_client()
    class_ids = _grade_class_ids(ac, schoolId, body.grade)
    if not class_ids:
        raise HTTPException(status_code=404, detail="No classes found for this grade")

    existing_res = (
        ac.table("syllabus_topics").select("order_index").in_("class_id", class_ids).order("order_index", desc=True).limit(1).execute()
    )
    existing = existing_res.data or []
    next_order = (existing[0]["order_index"] if existing and existing[0].get("order_index") is not None else -1) + 1
    definition_id = str(uuid.uuid4())
    created_at = _now()

    rows = [
        {
            "id": str(uuid.uuid4()),
            "class_id": class_id,
            "teacher_id": None,
            "grade": body.grade,
            "subject": body.subject,
            "definition_id": definition_id,
            "topic": topic,
            "description": (body.description or "").strip(),
            "week_number": body.weekNumber,
            "order_index": next_order,
            "is_completed": False,
            "created_at": created_at,
        }
        for class_id in class_ids
    ]
    ac.table("syllabus_topics").insert(rows).execute()
    return {"definitionId": definition_id}


class SyllabusTopicPatchBody(BaseModel):
    definitionId: str
    estimatedSessions: Optional[int] = None
    description: Optional[str] = None
    weekNumber: Optional[int] = None
    prerequisiteDefinitionId: Optional[str] = None


@router.patch("/{schoolId}/syllabus")
def patch_syllabus_topic(schoolId: str, body: SyllabusTopicPatchBody, admin: dict = Depends(require_admin)):
    update: dict = {}
    if isinstance(body.estimatedSessions, int):
        update["estimated_sessions"] = body.estimatedSessions
    if body.description is not None:
        update["description"] = body.description
    if isinstance(body.weekNumber, int):
        update["week_number"] = body.weekNumber
    if "prerequisiteDefinitionId" in body.model_fields_set:
        update["prerequisite_definition_id"] = body.prerequisiteDefinitionId or None
    if not update:
        raise HTTPException(status_code=400, detail="Nothing to update")

    ac = create_admin_client()
    ac.table("syllabus_topics").update(update).eq("definition_id", body.definitionId).execute()
    return {"ok": True}


class DefinitionIdBody(BaseModel):
    definitionId: str


@router.delete("/{schoolId}/syllabus")
def delete_syllabus_topic(schoolId: str, body: DefinitionIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    topic_rows = ac.table("syllabus_topics").select("id").eq("definition_id", body.definitionId).execute().data or []
    topic_ids = [t["id"] for t in topic_rows]
    if topic_ids:
        ac.table("syllabus_sub_topics").delete().in_("topic_id", topic_ids).execute()
    ac.table("syllabus_topics").delete().eq("definition_id", body.definitionId).execute()
    return {"ok": True}


# ─── Syllabus availability (calendar + timetable grounded session count) ──────

def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


@router.get("/{schoolId}/syllabus/availability")
def get_syllabus_availability(schoolId: str, grade: Optional[str] = None, subject: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not grade or not subject:
        raise HTTPException(status_code=400, detail="grade and subject required")

    ac = create_admin_client()
    classes_res = ac.table("classes").select("id").eq("school_id", schoolId).eq("grade", grade).execute()
    class_ids = [c["id"] for c in (classes_res.data or [])]
    if not class_ids:
        return {"availableSessions": 0, "academicYearEnd": None}

    raw_events = ac.table("academic_events").select("*").eq("school_id", schoolId).execute().data or []
    events = [
        {
            "category": e["category"],
            "startDate": e["start_date"],
            "endDate": e["end_date"],
            "countsAsNonWorking": e.get("counts_as_non_working", True),
        }
        for e in raw_events
    ]
    year_event = next((e for e in raw_events if e["category"] == "term" and e.get("title") == "Academic Year"), None)
    if not year_event:
        return {"availableSessions": 0, "academicYearEnd": None, "error": "No Academic Year set in the calendar yet"}

    # Read the draft/working timetable (school_timetable_periods), not the
    # published copy (timetable) — session availability is a planning figure
    # for the admin and shouldn't require Publish first, nor should a period
    # need a teacher assigned yet to count toward its subject's weekly total.
    timetable = (
        ac.table("school_timetable_periods").select("class_id, day_of_week, label")
        .in_("class_id", class_ids).execute().data or []
    )
    representative_class_id = next((cid for cid in class_ids if any(t["class_id"] == cid for t in timetable)), class_ids[0])
    representative_timetable = [t for t in timetable if t["class_id"] == representative_class_id]

    target = subject.strip().lower()
    periods_per_weekday: dict[int, int] = {}
    other_labels_seen: set[str] = set()
    matched_periods = 0
    for t in representative_timetable:
        label = str(t.get("label") or "").strip()
        if label.lower() == target:
            periods_per_weekday[t["day_of_week"]] = periods_per_weekday.get(t["day_of_week"], 0) + 1
            matched_periods += 1
        elif label:
            other_labels_seen.add(label)

    from_date = _today_str() if _today_str() > year_event["start_date"] else year_event["start_date"]
    available_sessions = compute_subject_session_availability(from_date, year_event["end_date"], events, periods_per_weekday)

    return {
        "availableSessions": available_sessions,
        "academicYearEnd": year_event["end_date"],
        "matchedPeriodsPerWeek": matched_periods,
        "otherTimetableLabels": list(other_labels_seen)[:10] if matched_periods == 0 else None,
    }


# ─── Syllabus sub-topics ───────────────────────────────────────────────────────

@router.get("/{schoolId}/syllabus/subtopics")
def get_subtopics(schoolId: str, topicDefinitionId: Optional[str] = None, admin: dict = Depends(require_admin)):
    if not topicDefinitionId:
        raise HTTPException(status_code=400, detail="topicDefinitionId required")

    ac = create_admin_client()
    topic_rows = ac.table("syllabus_topics").select("id").eq("definition_id", topicDefinitionId).execute().data or []
    topic_ids = [t["id"] for t in topic_rows]
    if not topic_ids:
        return {"subtopics": []}

    data = ac.table("syllabus_sub_topics").select("*").in_("topic_id", topic_ids).order("order_index").execute().data or []
    seen: set[str] = set()
    subtopics = []
    for s in data:
        key = s.get("definition_id") or s["id"]
        if key in seen:
            continue
        seen.add(key)
        subtopics.append(
            {
                "id": s["id"],
                "definitionId": s.get("definition_id") or s["id"],
                "name": s["name"],
                "description": s.get("description") or "",
                "orderIndex": s.get("order_index") or 0,
                "estimatedSessions": s.get("estimated_sessions"),
            }
        )
    return {"subtopics": subtopics}


class SubtopicBody(BaseModel):
    topicDefinitionId: str
    name: str
    description: Optional[str] = None


@router.post("/{schoolId}/syllabus/subtopics")
def post_subtopic(schoolId: str, body: SubtopicBody, admin: dict = Depends(require_admin)):
    name = body.name.strip()
    if not body.topicDefinitionId or not name:
        raise HTTPException(status_code=400, detail="topicDefinitionId and name required")

    ac = create_admin_client()
    topic_rows = ac.table("syllabus_topics").select("id, class_id").eq("definition_id", body.topicDefinitionId).execute().data or []
    if not topic_rows:
        raise HTTPException(status_code=404, detail="Topic not found")

    topic_ids = [t["id"] for t in topic_rows]
    existing_res = (
        ac.table("syllabus_sub_topics").select("order_index").in_("topic_id", topic_ids).order("order_index", desc=True).limit(1).execute()
    )
    existing = existing_res.data or []
    next_order = (existing[0]["order_index"] if existing and existing[0].get("order_index") is not None else -1) + 1
    definition_id = str(uuid.uuid4())
    created_at = _now()

    rows = [
        {
            "id": str(uuid.uuid4()),
            "topic_id": t["id"],
            "class_id": t["class_id"],
            "teacher_id": None,
            "definition_id": definition_id,
            "name": name,
            "description": (body.description or "").strip() or None,
            "order_index": next_order,
            "is_completed": False,
            "created_at": created_at,
        }
        for t in topic_rows
    ]
    ac.table("syllabus_sub_topics").insert(rows).execute()
    return {"definitionId": definition_id}


class SubtopicPatchBody(BaseModel):
    definitionId: str
    estimatedSessions: int


@router.patch("/{schoolId}/syllabus/subtopics")
def patch_subtopic(schoolId: str, body: SubtopicPatchBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    ac.table("syllabus_sub_topics").update({"estimated_sessions": body.estimatedSessions}).eq("definition_id", body.definitionId).execute()
    return {"ok": True}


@router.delete("/{schoolId}/syllabus/subtopics")
def delete_subtopic(schoolId: str, body: DefinitionIdBody, admin: dict = Depends(require_admin)):
    ac = create_admin_client()
    ac.table("syllabus_sub_topics").delete().eq("definition_id", body.definitionId).execute()
    return {"ok": True}


