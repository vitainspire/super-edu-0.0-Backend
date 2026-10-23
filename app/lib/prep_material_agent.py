"""The Prep Material Creation Agent — one focused job: turn a topic into a
textbook-grounded prep sheet, refusing rather than silently generating
ungrounded content when no real textbook match exists.

Not a tool-calling reasoning loop like argus_agent.py/teacher_agent.py — a
single-purpose generation step, same as how this codebase already names
prep_flow/agents/planning.py and prep_flow/agents/feedback.py "agents"
despite neither having a multi-step loop either. There's exactly one thing
to decide here (is this grounded enough to generate from), not an
open-ended "which tool next" question a reasoning loop would be for.

Grounding is checked BEFORE the expensive generation pipeline runs, not
after — refusing after spending the real LLM + image-generation calls would
make "stay token-efficient" and "refuse when ungrounded" contradict each
other. The already-fetched grounding is then handed into generate_smart_lesson
instead of being fetched a second time.
"""
from .supabase_clients import create_admin_client
from .textbook_grounding import fetch_textbook_grounding
from ..routes.smart_lesson_routes import generate_smart_lesson
from ..routes.teacher import get_shared_prep_material


async def create_prep_material(
    class_id: str, grade: str, subject: str, topic: str, teacher_id: str, school_id: str,
    subtopic: str = None, below_grade_level: bool = False,
) -> dict:
    ac = create_admin_client()
    textbook = await fetch_textbook_grounding(ac, school_id, str(grade), subject, topic, subtopic)
    if textbook is None:
        return {
            "grounded": False,
            "error": f'No textbook match found for "{topic}" ({subject}, grade {grade}) — nothing generated.',
        }

    lesson = await generate_smart_lesson(
        class_id=class_id, topic=topic, subject=subject, grade=grade, teacher_id=teacher_id,
        subtopic=subtopic, textbook=textbook, below_grade_level=below_grade_level,
    )
    return {"grounded": True, "chapterTitle": textbook.get("chapterTitle"), **lesson}


def find_existing_prep_material(
    teacher_id: str, class_id: str, subject: str, topic: str,
    subtopic: str = None, grade: str = None, ac=None,
) -> dict:
    """Checks two different tables, in order, same as before: this teacher's
    own prep_materials (source='personal') first, then the shared pool
    (shared_prep_materials). Zero generation cost — reads only. Shared by
    teacher_agent.py's get_prep_material tool and
    teacher_period_briefing.py's "show what's already ready" step, so there
    is exactly one real lookup, not two copies of it."""
    ac = ac or create_admin_client()
    if grade is None:
        class_row = ac.table("classes").select("grade").eq("id", class_id).maybe_single().execute().data
        grade = (class_row or {}).get("grade")

    personal = (
        ac.table("prep_materials").select("lesson")
        .eq("teacher_id", teacher_id).eq("class_id", class_id).ilike("topic", topic)
        .order("created_at", desc=True).limit(1).execute().data or []
    )
    if personal:
        return {"lesson": personal[0]["lesson"], "source": "personal"}

    if grade:
        shared = get_shared_prep_material(
            classId=class_id, subject=subject, grade=grade, topic=topic,
            subtopic=subtopic, teacher_id=teacher_id,
        )
        if shared.get("lesson") is not None:
            return {"lesson": shared["lesson"], "source": "shared"}
    return {"lesson": None}
