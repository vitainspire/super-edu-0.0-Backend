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
