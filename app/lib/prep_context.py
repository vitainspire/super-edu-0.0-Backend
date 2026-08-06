"""Shared prep/workbook context gathering — the same class-specific inputs the
Prep Sheet is built from: interests, weak topics, textbook/ontology grounding,
teacher profile, plus any already-generated prep lesson for a topic (so the
Workbook can be grounded in what was actually taught).

Mirrors frontend/lib/prep/gather-context.ts. Kept self-contained so each caller
(generate-workbook now, smart-lesson later) can use only what it needs.
"""
from typing import Optional


async def fetch_grounding(admin, topic_definition_id: str) -> Optional[dict]:
    try:
        topic_rows = (
            admin.table("syllabus_topics")
            .select("id, chapter_id")
            .eq("definition_id", topic_definition_id)
            .execute()
        ).data or []
        if not topic_rows:
            return None

        topic_ids = [t["id"] for t in topic_rows]
        chapter_id = topic_rows[0].get("chapter_id")

        chapter_title = page_start = page_end = None
        if chapter_id:
            chapter = (
                admin.table("syllabus_chapters")
                .select("title, page_start, page_end")
                .eq("id", chapter_id)
                .maybe_single()
                .execute()
            ).data
            if chapter:
                chapter_title = chapter.get("title")
                page_start = chapter.get("page_start")
                page_end = chapter.get("page_end")

        ex_rows = (
            admin.table("syllabus_exercises")
            .select("text, exercise_type, definition_id")
            .in_("topic_id", topic_ids)
            .execute()
        ).data or []
        seen_ex, exercises = set(), []
        for e in ex_rows:
            if e["definition_id"] in seen_ex:
                continue
            seen_ex.add(e["definition_id"])
            exercises.append({"text": e["text"], "type": e["exercise_type"]})

        sb_rows = (
            admin.table("syllabus_sidebars")
            .select("text, definition_id")
            .in_("topic_id", topic_ids)
            .execute()
        ).data or []
        seen_sb, sidebars = set(), []
        for s in sb_rows:
            if s["definition_id"] in seen_sb:
                continue
            seen_sb.add(s["definition_id"])
            sidebars.append(s["text"])

        if not chapter_title and not exercises and not sidebars:
            return None
        return {
            "chapterTitle": chapter_title, "pageStart": page_start, "pageEnd": page_end,
            "exercises": exercises, "sidebars": sidebars,
        }
    except Exception:
        return None


async def fetch_prep_lesson(admin, class_id: str, topic: str, subtopic: Optional[str]) -> Optional[dict]:
    try:
        rows = (
            admin.table("prep_materials")
            .select("lesson, subtopic")
            .eq("class_id", class_id)
            .ilike("topic", topic)
            .execute()
        ).data or []
        if not rows:
            return None
        want_sub = (subtopic or "").strip().lower()
        exact = next((r for r in rows if (r.get("subtopic") or "").strip().lower() == want_sub), None)
        return (exact or rows[0]).get("lesson")
    except Exception:
        return None


async def gather_class_context(
    admin, class_id: str, teacher_id: Optional[str],
    exclude_topic: Optional[str] = None, weak_topic_limit: int = 3,
) -> dict:
    students = (
        admin.table("students").select("id, interests")
        .eq("class_id", class_id).eq("is_active", True).execute()
    ).data or []
    total_students = len(students)

    interest_counts: dict[str, int] = {}
    for s in students:
        for raw in (s.get("interests") or []):
            i = (raw or "").strip()
            if not i:
                continue
            interest_counts[i] = interest_counts.get(i, 0) + 1
    class_interests = [i for i, _ in sorted(interest_counts.items(), key=lambda kv: -kv[1])][:5]

    teaching_profile = None
    if teacher_id:
        row = admin.table("teachers").select("teaching_profile").eq("id", teacher_id).maybe_single().execute().data
        teaching_profile = (row or {}).get("teaching_profile")

    student_ids = [s["id"] for s in students]
    all_marks = (
        admin.table("marks").select("student_id, score, tests(topic, total_marks)")
        .in_("student_id", student_ids).execute()
    ).data if student_ids else []

    topic_stats: dict[str, dict] = {}
    for mark in (all_marks or []):
        test = mark.get("tests")
        if not test or not test.get("total_marks"):
            continue
        t = test["topic"]
        pct = mark["score"] / test["total_marks"]
        cur = topic_stats.setdefault(t, {"totalPct": 0.0, "count": 0})
        cur["totalPct"] += pct
        cur["count"] += 1
    exclude = (exclude_topic or "").strip().lower()
    weak_topics = [
        t for t, s in sorted(topic_stats.items(), key=lambda kv: kv[0])
        if t.strip().lower() != exclude and s["count"] and (s["totalPct"] / s["count"]) < 0.65
    ][:weak_topic_limit]

    return {
        "totalStudents": total_students, "classInterests": class_interests,
        "weakTopics": weak_topics, "teachingProfile": teaching_profile,
    }


async def gather_topic_context(
    admin, class_id: str, topic: str, subtopic: Optional[str], topic_definition_id: Optional[str],
) -> dict:
    grounding = await fetch_grounding(admin, topic_definition_id) if topic_definition_id else None
    prep_lesson = await fetch_prep_lesson(admin, class_id, topic, subtopic)
    return {"topic": topic, "subtopic": subtopic, "grounding": grounding, "prepLesson": prep_lesson}


async def fetch_feedback_context(admin, class_id: str, topic: str) -> dict:
    """Post-class feedback -> next-lesson adjustment. Two different scopes: a
    per-topic insight (only relevant if this exact topic comes back around) and
    a per-class profile (this class's general tendencies, relevant no matter
    what's being generated). Best-effort — missing columns/rows just mean no
    adjustment context, never a failure."""
    try:
        feedback_row = (
            admin.table("lesson_feedback").select("insight")
            .eq("class_id", class_id).eq("topic", topic)
            .not_.is_("insight", "null")
            .order("created_at", desc=True).limit(1).maybe_single()
            .execute()
        ).data
        class_row = (
            admin.table("classes").select("feedback_profile")
            .eq("id", class_id).maybe_single().execute()
        ).data
        return {
            "topicInsight": (feedback_row or {}).get("insight"),
            "classProfile": (class_row or {}).get("feedback_profile"),
        }
    except Exception:
        return {}


async def fetch_recent_challenge_activities(admin, class_id: str) -> list[str]:
    """Rotation: the last few Challenge activities generated for this class, so
    the prompt can be told to pick something different instead of defaulting
    to the same one or two activities every time."""
    try:
        rows = (
            admin.table("prep_materials").select("lesson, created_at")
            .eq("class_id", class_id).order("created_at", desc=True).limit(5)
            .execute()
        ).data or []
        activities = []
        seen = set()
        for r in rows:
            a = ((r.get("lesson") or {}).get("challenge") or {}).get("activity")
            if isinstance(a, str) and a.strip() and a not in seen:
                seen.add(a)
                activities.append(a)
        return activities
    except Exception:
        return []


# ── Teaching-profile personalization line — mirrors lib/logic/teaching-profile.ts ──

_PERSONALIZATION_LABELS = {
    "stories": "storytelling",
    "games": "games",
    "handsOn": "hands-on activities",
    "criticalThinking": "critical thinking",
    "creativity": "creativity",
    "realLife": "real-life connections",
    "localCulture": "local culture references",
    "reflection": "student reflection",
    "exploration": "independent exploration",
}


def personalization_tier_line(personalization: Optional[dict]) -> str:
    p = personalization or {}
    always, often, never = [], [], []
    for key, label in _PERSONALIZATION_LABELS.items():
        value = p.get(key)
        if value == "always":
            always.append(label)
        elif value == "often":
            often.append(label)
        elif value == "never":
            never.append(label)
    parts = []
    if always:
        parts.append(f"Rely heavily on: {', '.join(always)}.")
    if often:
        parts.append(f"Use regularly: {', '.join(often)}.")
    if never:
        parts.append(f"Avoid: {', '.join(never)}.")
    return " ".join(parts)
