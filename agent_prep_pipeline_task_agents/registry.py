"""The Writer agent. Timing and duplication are NOT separate agents here --
the reused Checker already has `check_timing`/`check_duplication` as tools
it may choose to call (see `agent_prep_pipeline/tools_checks.py`), and the
Fixer already knows how to act on what either one finds (a section rewrite
for duplication, the `extra` field for timing -- see
`agent_prep_pipeline/schemas.py`'s `FixerOutput`). Building separate
Timing/Duplication agents here would just be a second, competing way to do
a job the Checker/Fixer pair already does.
"""
from __future__ import annotations

from typing import Optional

from deepagents import create_deep_agent, FilesystemPermission
from deepagents.backends import StateBackend

from deep_agents import skillset
from deep_agents.middleware import AgentRun, build_middleware
from deep_agents.model import build_model
from deep_agents.tools import book as book_tools
from deep_agents.tools.runtime import RunContext as BookContext
from deep_agents.workspace import MEMORY_SOURCES, SKILL_SOURCES
from prep_flow.sections import SECTION_LABELS, SECTION_ORDER, SECTION_POLICY

from . import schemas
from .topic_spec import TopicSpec


def _deny_all_writes() -> list:
    return [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]


async def invoke_structured(agent, instruction: str):
    result = await agent.ainvoke({"messages": [{"role": "user", "content": instruction}]})
    answer = result.get("structured_response")
    if answer is None:
        raise RuntimeError(
            "agent produced no structured answer (step budget exhausted or a refusal)")
    return answer


def _topic_block(spec: TopicSpec) -> str:
    lines = [
        f"TOPIC: {spec.topic}" + (f" / {spec.subtopic}" if spec.subtopic else ""),
        f"MASTERY TARGET: {spec.mastery_target or '(none stated)'}",
        f"CONCEPTS: {', '.join(spec.concepts) or '(none)'}",
        f"COMPETENCIES: {', '.join(spec.competencies) or '(none)'}",
        f"VOCABULARY: {', '.join(spec.vocabulary) or '(none)'}",
        f"ANCHOR OBJECT (from Node 1's experience design): {spec.anchor or '(none given)'}",
        f"COGNITIVE TRAJECTORY: {spec.trajectory or '(none given)'}",
        f"SELECTED ACTIVITY for Challenge: {spec.activity_name or '(none -- follow the book task if one is set)'}",
    ]
    if spec.floor:
        lines.append(f"THE FLOOR (what the weakest child must do): {spec.floor}")
    if spec.gap and spec.gap_closer:
        lines.append(f"NAMED GAP: {spec.gap}")
        lines.append(f"GAP CLOSER: {spec.gap_closer}")
    return "\n".join(lines)


_ALL_RULES = "\n\n".join(
    f"{SECTION_LABELS[k]} ({k}): {SECTION_POLICY[k]['rule']}" for k in SECTION_ORDER)

_WRITER_PROMPT = """You are writing ONE FULL PERIOD of a Grade {grade} {subject} chapter -- \
all six sections yourself: Refresher, Concept, Real Life, Challenge, Level Set, Explore.

{topic_block}

TEXTBOOK EXCERPT FOR THIS TOPIC (the ONLY source for any fact/number in Concept):
{excerpt}

{previous_explore_block}

THE ROOM THIS IS WRITTEN FOR: up to sixty children, one teacher, a blackboard and chalk, \
no projector. Anything the class must SEE (an object changing, a comparison) goes on the \
blackboard, not just held up.

THE RULE FOR EACH SECTION:

{rules}

THE SKILLS FOR THIS STAGE, in load order (the same set `deep_agents`' own lesson-design \
stage uses -- composing all six sections in one pass is the same job at the same scope: \
every one of the five learning goals is decided in this period, so almost the whole set \
is required, not optional):

{skills}

Load the first two before anything else, and the rest as the work reaches them -- do not \
load all of them up front and none of them again.

Look up the actual textbook pages (get_page_range, get_book_page, search_book) before \
stating any number or fact in Concept -- it may not contain anything the book does not. \
Because you are writing all six sections yourself, you are the one place that can keep \
them consistent: do not demonstrate the same object the same way in both Concept and \
Challenge (see Concept's own rule on this), and make sure Explore's board sketch names \
something Explore's own points actually mention, not a different section's object.

{gate}

Answer with the schema."""


def build_writer_agent(spec: TopicSpec, *, grade: str, subject: str, chapter_title: str,
                       chapter_markdown: str, chapter_figures: Optional[list],
                       max_model_calls: int = 20):
    record = AgentRun(label="taskagents:writer")
    book_ctx = BookContext(
        chapter_markdown=chapter_markdown, chapter_title=chapter_title,
        chapter_figures=chapter_figures or [], grade=grade, subject=subject,
    )
    previous_block = (
        "This is the FIRST topic of the chapter -- there is no previous Explore, so "
        "refresher_points must be an empty list."
        if spec.is_first else
        "THE PREVIOUS TOPIC'S EXPLORE (Refresher must be built ENTIRELY from this, and "
        f"nothing else): previous topic = '{spec.previous_topic}'\n" +
        "\n".join(f"  {i+1}. {p.get('text','')} -- {p.get('detail','')}"
                  for i, p in enumerate(spec.previous_explore_points))
    )
    # Reusing deep_agents' EXPERIENCE stage's routing as-is, not a parallel
    # list -- LessonDesigner's own skillset.py comment explains why this
    # scope needs nearly its whole routed set rather than a narrow slice:
    # "ALL FIVE GOALS ARE DECIDED IN THE PERIOD". A Writer composing the
    # actual prose for that same period is answerable to the same five.
    prompt = _WRITER_PROMPT.format(
        grade=grade, subject=subject, topic_block=_topic_block(spec),
        excerpt=spec.excerpt or "(no excerpt available)",
        previous_explore_block=previous_block, rules=_ALL_RULES,
        skills=skillset.prompt_block(skillset.EXPERIENCE, subject=subject),
        gate=skillset.gate_instruction(skillset.EXPERIENCE, subject=subject))

    agent = create_deep_agent(
        tools=book_tools.build(book_ctx),
        system_prompt=prompt,
        permissions=_deny_all_writes(),
        response_format=schemas.WriterOutput,
        name="task-writer",
        # Matches generation/compose.py's own 0.6 -- writing prose, not
        # deciding facts.
        model=build_model(temperature=0.6, label=record.label),
        backend=StateBackend(),
        # EXPERIENCE's routing marks up to 10 skills REQUIRED (open before
        # starting) -- default bumped from 14 to 20 to give the Writer room
        # to actually read them before it starts drafting, not just before
        # it runs out of budget. `deep_agents/middleware.py`'s own comment
        # documents the real failure mode of under-budgeting this: an agent
        # that loaded zero skills and produced output that looked correct
        # anyway.
        skills=SKILL_SOURCES, memory=MEMORY_SOURCES,
        middleware=build_middleware(record, max_model_calls=max_model_calls),
        subagents=[], checkpointer=False,
    )
    return agent, record
