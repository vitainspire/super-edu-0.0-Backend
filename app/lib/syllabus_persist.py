"""Persists a fully-extracted textbook ontology (chapters, topics, subtopics,
exercises, sidebars, and concept-dependency edges) into the syllabus_* tables
in one pass — see db/migrations/019_full_syllabus_ontology.sql for the schema.

Fan-out rules mirror the manual authoring endpoints in admin_schools.py:
  - chapters and dependencies are grade+subject scoped — ONE row each, since
    they're reference/relationship data, not something a section tracks
    completion against.
  - topics, subtopics, exercises, and sidebars are duplicated ONE ROW PER
    SECTION (class_id), all sharing a `definition_id` across sections — the
    same pattern syllabus_topics/syllabus_sub_topics already use, so a
    prerequisite or estimate set on one section's row resolves in every
    other section too.

Only vision_extraction.generate_ontology_vision()'s ontology shape is
understood here (entities.chapters/topics/subtopics/exercises/sidebars,
graphs.concept_dependencies). Text-pasted imports (extract-syllabus route)
have no such ontology and keep using the older per-topic POST /syllabus flow.
"""

import json
import uuid
from datetime import datetime, timezone


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _grade_class_ids(ac, school_id: str, grade: str) -> list[str]:
    res = ac.table("classes").select("id").eq("school_id", school_id).eq("grade", grade).execute()
    return [c["id"] for c in (res.data or [])]


def _next_order_index(ac, table: str, class_ids: list[str]) -> int:
    if not class_ids:
        return 0
    existing = (
        ac.table(table).select("order_index").in_("class_id", class_ids)
        .order("order_index", desc=True).limit(1).execute().data or []
    )
    return (existing[0]["order_index"] if existing and existing[0].get("order_index") is not None else -1) + 1


def persist_extraction(
    ac,
    school_id: str,
    grade: str,
    subject: str,
    ontology: dict,
    filename: str,
    exclude_topic_ids: set[str],
) -> dict:
    """Write one PDF extraction's full ontology into the DB. Returns a summary
    of how many logical (pre-fan-out) rows were saved, for a confirmation message.
    Raises ValueError if the grade has no classes to fan out into.
    """
    entities = ontology.get("entities", {}) or {}
    graphs = ontology.get("graphs", {}) or {}
    chapters = entities.get("chapters", []) or []

    class_ids = _grade_class_ids(ac, school_id, grade)
    if not class_ids:
        raise ValueError("No classes found for this grade")

    # ── 1. Chapters — one row per chapter, not fanned per section ───────────
    chapter_db_id: dict[str, str] = {}   # ontology chapter id ("C_2") -> syllabus_chapters.id
    chapter_rows = []
    for c in chapters:
        new_id = str(uuid.uuid4())
        chapter_db_id[c.get("id")] = new_id
        chapter_rows.append({
            "id": new_id,
            "school_id": school_id,
            "grade": grade,
            "subject": subject,
            "definition_id": str(uuid.uuid4()),
            "chapter_number": c.get("number") or 0,
            "title": c.get("title") or "",
            "page_start": c.get("page_start"),
            "page_end": c.get("page_end"),
            "created_at": _now(),
        })
    if chapter_rows:
        ac.table("syllabus_chapters").insert(chapter_rows).execute()

    # ── 2. Topics — fanned per section, sharing definition_id ───────────────
    topics = [
        t for t in (entities.get("topics", []) or [])
        if t.get("id") not in exclude_topic_ids and (t.get("name") or "").strip()
    ]

    def chap_number(t: dict) -> int:
        c = next((c for c in chapters if c.get("id") == t.get("chapter_id")), None)
        num = c.get("number") if c else None
        return num if isinstance(num, int) else 999

    topics.sort(key=lambda t: (chap_number(t), t.get("page_start") or 0, t.get("id") or ""))

    next_topic_order = _next_order_index(ac, "syllabus_topics", class_ids)

    topic_definition_id: dict[str, str] = {}                    # ontology topic id -> definition_id
    topic_row_id_by_def_and_class: dict[str, dict[str, str]] = {}  # definition_id -> {class_id: row id}
    topic_rows = []
    for i, t in enumerate(topics):
        onto_id = t.get("id")
        def_id = str(uuid.uuid4())
        topic_definition_id[onto_id] = def_id
        chapter_row_id = chapter_db_id.get(t.get("chapter_id"))
        num = chap_number(t)
        week_number = num if num != 999 else (i + 1)
        order = next_topic_order + i

        per_class_ids: dict[str, str] = {}
        for class_id in class_ids:
            row_id = str(uuid.uuid4())
            per_class_ids[class_id] = row_id
            topic_rows.append({
                "id": row_id,
                "class_id": class_id,
                "teacher_id": None,
                "grade": grade,
                "subject": subject,
                "definition_id": def_id,
                "topic": t.get("name") or "",
                "description": (t.get("summary") or "").strip(),
                "week_number": week_number,
                "order_index": order,
                "is_completed": False,
                "chapter_id": chapter_row_id,
                "page_start": t.get("page_start"),
                "page_end": t.get("page_end"),
                "created_at": _now(),
            })
        topic_row_id_by_def_and_class[def_id] = per_class_ids
    if topic_rows:
        ac.table("syllabus_topics").insert(topic_rows).execute()

    # ── 3. Subtopics — fanned per section ────────────────────────────────────
    subs_by_topic: dict[str, list] = {}
    for s in (entities.get("subtopics", []) or []):
        tid = s.get("topic_id")
        if tid in topic_definition_id:
            subs_by_topic.setdefault(tid, []).append(s)

    subtopic_count = 0
    subtopic_rows = []
    for onto_topic_id, subs in subs_by_topic.items():
        def_id = topic_definition_id[onto_topic_id]
        per_class_ids = topic_row_id_by_def_and_class[def_id]
        order = 0
        for s in subs:
            name = (s.get("name") or "").strip()
            if not name:
                continue
            sub_def_id = str(uuid.uuid4())
            for class_id, topic_row_id in per_class_ids.items():
                subtopic_rows.append({
                    "id": str(uuid.uuid4()),
                    "topic_id": topic_row_id,
                    "class_id": class_id,
                    "teacher_id": None,
                    "definition_id": sub_def_id,
                    "name": name,
                    "description": (s.get("summary") or "").strip() or None,
                    "order_index": order,
                    "is_completed": False,
                    "skill_type": s.get("skill_type"),
                    "page_start": s.get("page_start"),
                    "page_end": s.get("page_end"),
                    "created_at": _now(),
                })
            order += 1
            subtopic_count += 1
    if subtopic_rows:
        ac.table("syllabus_sub_topics").insert(subtopic_rows).execute()

    # ── 4. Exercises — fanned per section ────────────────────────────────────
    _VALID_EXERCISE_TYPES = {
        "writing_practice", "art_activity", "matching_exercise", "reading_exercise",
        "comprehension", "listening_activity", "counting_activity", "general_activity",
        "fill_in_blank", "multiple_choice", "true_false", "short_answer", "essay",
        "problem_solving", "group_activity",
    }
    exercises_by_topic: dict[str, list] = {}
    for e in (entities.get("exercises", []) or []):
        tid = e.get("topic_id")
        if tid in topic_definition_id:
            exercises_by_topic.setdefault(tid, []).append(e)

    exercise_count = 0
    exercise_rows = []
    for onto_topic_id, exs in exercises_by_topic.items():
        def_id = topic_definition_id[onto_topic_id]
        per_class_ids = topic_row_id_by_def_and_class[def_id]
        order = 0
        for e in exs:
            text = (e.get("text") or "").strip()
            if not text:
                continue
            ex_def_id = str(uuid.uuid4())
            etype = e.get("exercise_type") or "general_activity"
            if etype not in _VALID_EXERCISE_TYPES:
                etype = "general_activity"
            for class_id, topic_row_id in per_class_ids.items():
                exercise_rows.append({
                    "id": str(uuid.uuid4()),
                    "topic_id": topic_row_id,
                    "definition_id": ex_def_id,
                    "text": text,
                    "exercise_type": etype,
                    "page": e.get("page"),
                    "order_index": order,
                    "created_at": _now(),
                })
            order += 1
            exercise_count += 1
    if exercise_rows:
        ac.table("syllabus_exercises").insert(exercise_rows).execute()

    # ── 5. Sidebars — fanned per section ─────────────────────────────────────
    sidebars_by_topic: dict[str, list] = {}
    for sb in (entities.get("sidebars", []) or []):
        tid = sb.get("topic_id")
        if tid in topic_definition_id:
            sidebars_by_topic.setdefault(tid, []).append(sb)

    sidebar_count = 0
    sidebar_rows = []
    for onto_topic_id, sbs in sidebars_by_topic.items():
        def_id = topic_definition_id[onto_topic_id]
        per_class_ids = topic_row_id_by_def_and_class[def_id]
        order = 0
        for sb in sbs:
            text = (sb.get("text") or "").strip()
            if not text:
                continue
            sb_def_id = str(uuid.uuid4())
            for class_id, topic_row_id in per_class_ids.items():
                sidebar_rows.append({
                    "id": str(uuid.uuid4()),
                    "topic_id": topic_row_id,
                    "definition_id": sb_def_id,
                    "text": text,
                    "page": sb.get("page"),
                    "sidebar_type": "general",
                    "order_index": order,
                    "created_at": _now(),
                })
            order += 1
            sidebar_count += 1
    if sidebar_rows:
        ac.table("syllabus_sidebars").insert(sidebar_rows).execute()

    # ── 6. Dependencies — grade+subject scoped, not fanned ───────────────────
    dep_rows = []
    seen_edges: set[tuple[str, str]] = set()

    def add_dep(from_onto_id, to_onto_id, strength):
        from_def = topic_definition_id.get(from_onto_id)
        to_def = topic_definition_id.get(to_onto_id)
        if not from_def or not to_def or from_def == to_def:
            return
        key = (from_def, to_def)
        if key in seen_edges:
            return
        seen_edges.add(key)
        dep_rows.append({
            "id": str(uuid.uuid4()),
            "school_id": school_id,
            "grade": grade,
            "subject": subject,
            "from_definition_id": from_def,
            "to_definition_id": to_def,
            "dependency_type": "depends_on",
            "strength": strength,
            "created_at": _now(),
        })

    for t in topics:
        for prereq_onto_id in (t.get("prerequisites") or []):
            add_dep(t.get("id"), prereq_onto_id, "required")

    for edge in (graphs.get("concept_dependencies", []) or []):
        add_dep(edge.get("from"), edge.get("to"), "recommended")

    if dep_rows:
        ac.table("syllabus_dependencies").upsert(
            dep_rows, on_conflict="from_definition_id,to_definition_id,dependency_type"
        ).execute()

    # ── 7. Raw ontology — kept for debugging / future re-processing ─────────
    safe_ontology = json.loads(json.dumps(ontology, default=str))
    ac.table("syllabus_ontology_extractions").insert({
        "id": str(uuid.uuid4()),
        "school_id": school_id,
        "grade": grade,
        "subject": subject,
        "file_name": filename,
        "ontology_data": safe_ontology,
        "extraction_metadata": {
            "language": ontology.get("language"),
            "chapterCount": len(chapters),
            "topicCount": len(topics),
        },
        "created_at": _now(),
    }).execute()

    return {
        "chapters": len(chapter_rows),
        "topics": len(topics),
        "subtopics": subtopic_count,
        "exercises": exercise_count,
        "sidebars": sidebar_count,
        "dependencies": len(dep_rows),
    }
