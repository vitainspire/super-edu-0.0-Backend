"""Lightweight "skill" selection for the live per-class prep-sheet generator
(smart_lesson_routes.py).

This is NOT the deep_agents/skills/*/SKILL.md library used by the separate,
offline agentic pipeline (prep_flow -> generation -> validation_flow). Those
files are written against THAT pipeline's own fields (`floor`, `gained`,
`exercisesCovered`, a take-home question) which the live six-section bullet
schema (concept/explore/challenge/levelSet/previousTopicRefresher) has no
place for — dropping them into this prompt as-is would tell the model to
reason about fields that don't exist here. These four angles cover the same
underlying ideas (a floor for a child behind, real headroom for one ahead,
first-exposure misconceptions, genuine continuity) in this schema's own
vocabulary instead.

Selection is DETERMINISTIC, not a model call — every signal it needs
(below_grade_level, weak_topics, previous_topic) is already gathered by
gather_class_context()/fetch_previous_topic() before generation runs, so
picking an angle costs nothing extra in latency or money. A real LLM-driven
selection step would be the obvious next upgrade if a fixed rule ever proves
too coarse, but nothing observed yet asks for one.
"""
from typing import Optional


def _differentiation(ctx: dict) -> Optional[dict]:
    return {
        "key": "differentiation",
        "label": "Differentiation (floor for a child behind)",
        "guidance": (
            "SKILL — DIFFERENTIATION: this class has a real gap right now "
            "(below grade level and/or weak on prior basics), so Concept and "
            "Explore must each let a child who is behind grab the idea through "
            "something smaller and more concrete before the main task — not a "
            "separate remedial step, never named as one (see the WEAK-TOPIC "
            "SUPPORT IS INVISIBLE rule). levelSet's self-check must be answerable "
            "by that same child without them feeling singled out."
        ),
    }


def _stretch(ctx: dict) -> Optional[dict]:
    return {
        "key": "stretch",
        "label": "Stretch (headroom for a class on pace)",
        "guidance": (
            "SKILL — STRETCH: this class isn't carrying a known gap right now, "
            "so Challenge and levelSet's final either/or choice should carry real "
            "headroom — a genuinely harder second case, a 'find another way' "
            "extension, or a question with more than one valid path — not just a "
            "bigger number in the same template."
        ),
    }


def _misconception_first(ctx: dict) -> Optional[dict]:
    return {
        "key": "misconception_first",
        "label": "Misconception-first (fresh concept, nothing to bridge from)",
        "guidance": (
            "SKILL — MISCONCEPTION-FIRST: there is no prior topic to anchor this "
            "one, so treat first contact with the idea as the highest-risk moment. "
            "sectionWatch.concept must name the single most likely WRONG idea a "
            "child forms on first exposure — a genuine misunderstanding of what "
            "the idea means, not a careless slip — and the one line that corrects "
            "it without lecturing."
        ),
    }


def _continuity(ctx: dict) -> Optional[dict]:
    previous_topic = ctx.get("previous_topic")
    return {
        "key": "continuity",
        "label": "Continuity (real predecessor topic)",
        "guidance": (
            f'SKILL — CONTINUITY: this topic has a real predecessor, "{previous_topic}". '
            "The refresher must concretely reuse the EXACT skill from that topic — "
            "not just sit near it in the syllabus — and its closing bridge line "
            "should make the connection explicit enough that a child feels today's "
            "idea growing out of yesterday's, not starting over."
        ),
    }


# Each pair is mutually exclusive and exhaustive on its own axis, so exactly
# one angle per pair always fires — two angles total per generation, never
# zero, never a model call needed to decide.
_RULES = [
    (lambda ctx: ctx.get("below_grade_level") or ctx.get("weak_topics"), _differentiation),
    (lambda ctx: not ctx.get("below_grade_level") and not ctx.get("weak_topics"), _stretch),
    (lambda ctx: not ctx.get("previous_topic"), _misconception_first),
    (lambda ctx: bool(ctx.get("previous_topic")), _continuity),
]


def select_prep_skills(
    *, below_grade_level: bool, weak_topics: list, previous_topic: Optional[str],
) -> list[dict]:
    ctx = {
        "below_grade_level": below_grade_level,
        "weak_topics": weak_topics,
        "previous_topic": previous_topic,
    }
    selected = []
    for trigger, make in _RULES:
        if trigger(ctx):
            entry = make(ctx)
            if entry:
                selected.append(entry)
    return selected


def skills_prompt_block(selected: list[dict]) -> str:
    if not selected:
        return ""
    return "\n\n".join(s["guidance"] for s in selected)
