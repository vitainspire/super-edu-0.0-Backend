"""Pedagogy tools — architecture §17(C). The activity library and the grade's ceiling.

WHAT §8 SAYS ABOUT THIS AND WHY IT IS RIGHT. Activity selection stays
deterministic: competencies go into the Pedagogy Library and a matching activity
comes out, and only an uncovered case reaches a model. These tools do not change
that. `prep_flow/agents/activity_selection.py` is still what CHOOSES, with its
no-repeat window and its variety rules intact.

What these are for is the question that comes BEFORE the choice — *is there any
activity in the library for what I am about to ask this topic to do?* An agent
that plans a period around an outcome the library cannot resource has written a
lesson the teacher cannot run, and it will find that out here or it will find it
out from a teacher.

`lookup_grade_constraints` is the one tool an agent should call every time.
`prep_flow/bands.py` bounds the DEMAND — what a Grade 3 child can be asked to
DO, as distinct from what the textbook covers — and over-pitching is the most
common way a technically correct lesson fails in a real room.
"""
from __future__ import annotations

from langchain_core.tools import BaseTool, tool

from prep_flow import bands
from prep_flow import tools as gateway

from .runtime import MAX_ROWS, RunContext, truncated

# Through `deps` like every other borrowed module. The try/except that used to
# be here guessed between a bare `subject_prompts` and the packaged path, which
# is exactly the drift `prep_flow/deps.py` exists to prevent — and it guessed
# wrong the moment the module moved to `app/lib/`.
from prep_flow.deps import subject_prompts_module as subject_prompts


def build(ctx: RunContext) -> list[BaseTool]:
    """Pedagogy tools bound to this grade, subject and resource level."""

    @tool
    def lookup_grade_constraints() -> str:
        """What a child in this grade band can be asked to DO.

        Call this before writing any outcome, any misconception probe, or any
        activity. It bounds the cognitive demand, never the content: if the
        textbook teaches three-digit numbers you still teach three-digit
        numbers — you just do not ask this class to justify a generalisation
        about them.
        """
        block = bands.band_block(ctx.grade)
        if not block.strip():
            return (f"No cognitive band is defined for grade {ctx.grade!r} "
                    "(the table covers grades 1-5). Use your own judgement, "
                    "and pitch conservatively.")
        return block

    @tool
    def lookup_pedagogy_rules() -> str:
        """The subject's pedagogy guidance — how this subject is taught well.

        Routed by subject: mathematics, language, EVS or a generic fallback.
        This is the guidance the generator itself reasons over, so a lesson
        designed against it and a sheet written against it agree.
        """
        module = subject_prompts.resolve_subject_module(ctx.subject)
        return (f"Subject module: {module.key}\n\n{module.pedagogy}")

    @tool
    def lookup_activity_bank() -> str:
        """The named activity formats available for this subject.

        These are the formats the final material is allowed to name. Design
        toward one of them. Inventing a format the bank does not hold means the
        teacher gets an instruction with no procedure behind it.
        """
        module = subject_prompts.resolve_subject_module(ctx.subject)
        return truncated(module.bank)

    @tool
    async def lookup_activities_for_competency(competency_ids: list[str]) -> str:
        """Activity templates the library holds for these competency ids.

        Ids come from `lookup_competencies` — raw phrases will not match. The
        answer is filtered to this class's resource level, so an activity that
        comes back is one this room can actually run.

        An empty answer is the signal that matters: it means the outcome you
        were planning has no resourcing behind it. Move the outcome toward one
        that does, or say plainly that the topic needs an invented activity.
        """
        if not competency_ids:
            return "Pass at least one competency id."
        try:
            rows = await gateway.pedagogy_activities(
                list(competency_ids)[:MAX_ROWS], ctx.grade, ctx.resource_level)
        except Exception as exc:
            return f"Activity lookup unavailable ({exc})."
        if not rows:
            return ("No library activity covers those competencies at resource "
                    f"level {ctx.resource_level}. Treat this topic as needing an "
                    "invented activity and say so in your output.")
        lines = []
        for row in rows[:MAX_ROWS]:
            lines.append(f"  [{row.get('id')}] {row.get('name')} "
                         f"({row.get('category', 'uncategorised')})"
                         f" — {(row.get('description') or '')[:160]}")
        return f"{len(rows)} activity template(s):\n" + "\n".join(lines)

    @tool
    def get_room_constraints() -> str:
        """The physical facts of the room this lesson runs in.

        Period length, class size, resource level and language, as Node 1 was
        given them. A trajectory that needs eight minutes of group work in a
        thirty-minute period with forty children is a trajectory that will not
        happen.
        """
        settings = ctx.teacher_settings or {}
        return (
            f"Period length: {settings.get('duration', 30)} minutes\n"
            # 60, not 40: this is the shared-batch default when no specific
            # classroom's settings are known, and the realism gate that judges
            # the output grades against up to 60 -- a plan sized for 40 that the
            # gate then measures against 60 is a mismatch between two stages
            # that are supposed to agree on the same room.
            f"Class size: {settings.get('classSize', 60)} children\n"
            f"Resource level: {ctx.resource_level} "
            f"(0 = nothing but a blackboard and what the children bring)\n"
            f"Language of instruction: {settings.get('language', 'English')}\n"
            f"Teaching style: {settings.get('teachingStyle', 'interactive')}\n"
            f"Objective: {settings.get('learningObjective', 'new lesson')}"
        )

    return [lookup_grade_constraints, lookup_pedagogy_rules, lookup_activity_bank,
            lookup_activities_for_competency, get_room_constraints]
