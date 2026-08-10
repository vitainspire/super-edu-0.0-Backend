"""Mirrors frontend lib/prep/gather-context.ts (plus the one function of
lib/logic/teaching-profile.ts the workbook prompt needs) — the same
class-specific inputs the Prep Sheet is built from (interests, weak topics,
textbook grounding, teacher profile), plus the already-generated prep lesson
for a topic, so the AI-generated workbook can be grounded in what was
actually taught. Used only by POST /api/generate-workbook (app/routes/ai_routes2.py).
"""
from typing import Optional


def _fetch_grounding(ac, topic_definition_id: str) -> Optional[dict]:
    try:
        topic_res = ac.table("syllabus_topics").select("id, chapter_id").eq("definition_id", topic_definition_id).execute()
        topic_rows = topic_res.data or []
        if not topic_rows:
            return None

        topic_ids = [t["id"] for t in topic_rows]
        chapter_id = topic_rows[0].get("chapter_id")

        chapter_title = page_start = page_end = None
        if chapter_id:
            chapter_res = (
                ac.table("syllabus_chapters").select("title, page_start, page_end").eq("id", chapter_id).maybe_single().execute()
            )
            chapter = chapter_res.data if chapter_res else None
            if chapter:
                chapter_title = chapter.get("title")
                page_start = chapter.get("page_start")
                page_end = chapter.get("page_end")

        ex_res = ac.table("syllabus_exercises").select("text, exercise_type, definition_id").in_("topic_id", topic_ids).execute()
        seen_ex: set = set()
        exercises = []
        for e in (ex_res.data or []):
            did = e.get("definition_id")
            if did in seen_ex:
                continue
            seen_ex.add(did)
            exercises.append({"text": e.get("text"), "type": e.get("exercise_type")})

        sb_res = ac.table("syllabus_sidebars").select("text, definition_id").in_("topic_id", topic_ids).execute()
        seen_sb: set = set()
        sidebars = []
        for s in (sb_res.data or []):
            did = s.get("definition_id")
            if did in seen_sb:
                continue
            seen_sb.add(did)
            sidebars.append(s.get("text"))

        if not chapter_title and not exercises and not sidebars:
            return None
        return {
            "chapterTitle": chapter_title,
            "pageStart": page_start,
            "pageEnd": page_end,
            "exercises": exercises,
            "sidebars": sidebars,
        }
    except Exception:
        return None


def _fetch_prep_lesson(ac, class_id: str, topic: str, subtopic: Optional[str]) -> Optional[dict]:
    try:
        res = ac.table("prep_materials").select("lesson, subtopic").eq("class_id", class_id).ilike("topic", topic).execute()
        rows = res.data or []
        if not rows:
            return None
        want_sub = (subtopic or "").strip().lower()
        exact = next((r for r in rows if (r.get("subtopic") or "").strip().lower() == want_sub), None)
        chosen = exact or rows[0]
        return chosen.get("lesson")
    except Exception:
        return None


def gather_class_context(ac, class_id: str, teacher_id: Optional[str] = None) -> dict:
    """Class-wide signals (topic-independent) — gathered ONCE per workbook."""
    students_res = ac.table("students").select("id, interests").eq("class_id", class_id).eq("is_active", True).execute()
    students = students_res.data or []
    total_students = len(students)

    interest_counts: dict[str, int] = {}
    for s in students:
        for raw in (s.get("interests") or []):
            i = (raw or "").strip()
            if not i:
                continue
            interest_counts[i] = interest_counts.get(i, 0) + 1
    class_interests = [i for i, _ in sorted(interest_counts.items(), key=lambda kv: kv[1], reverse=True)][:5]

    teaching_profile = None
    if teacher_id:
        t_res = ac.table("teachers").select("teaching_profile").eq("id", teacher_id).maybe_single().execute()
        teaching_profile = (t_res.data or {}).get("teaching_profile") if t_res and t_res.data else None

    student_ids = [s["id"] for s in students]
    marks = []
    if student_ids:
        marks_res = ac.table("marks").select("student_id, score, tests(topic, total_marks)").in_("student_id", student_ids).execute()
        marks = marks_res.data or []

    topic_stats: dict[str, dict] = {}
    for mark in marks:
        tests = mark.get("tests")
        if not tests or not tests.get("total_marks"):
            continue
        t = tests["topic"]
        pct = mark["score"] / tests["total_marks"]
        cur = topic_stats.setdefault(t, {"totalPct": 0.0, "count": 0})
        cur["totalPct"] += pct
        cur["count"] += 1

    weak_topics = [t for t, s in topic_stats.items() if s["totalPct"] / s["count"] < 0.65][:3]

    return {
        "totalStudents": total_students,
        "classInterests": class_interests,
        "weakTopics": weak_topics,
        "teachingProfile": teaching_profile,
    }


def gather_topic_context(
    ac, class_id: str, topic: str, subtopic: Optional[str] = None, topic_definition_id: Optional[str] = None,
) -> dict:
    """Per-topic signals — textbook grounding + any existing prep lesson for that topic."""
    grounding = _fetch_grounding(ac, topic_definition_id) if topic_definition_id else None
    prep_lesson = _fetch_prep_lesson(ac, class_id, topic, subtopic)
    return {"topic": topic, "subtopic": subtopic, "grounding": grounding, "prepLesson": prep_lesson}


# ─── The one piece of lib/logic/teaching-profile.ts the workbook prompt needs ──

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
    """Renders the teacher's always/often/never personalization choices as a
    single prompt line ('sometimes' — the neutral default — is omitted)."""
    p = personalization or {}
    always: list[str] = []
    often: list[str] = []
    never: list[str] = []
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
