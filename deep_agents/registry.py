"""The three agents - architecture section 12, as constructors.

    CurriculumReasoner   what this chapter requires, and what it gets wrong
    LessonDesigner       the path a child takes through one period
    ContextReinforcer    what this room changes about that path

THERE IS NO FOURTH AGENT, and that is section 13 of the architecture rather than
an omission. By the time generation runs, every judgement has been made: it
composes approved decisions into six sections. A constrained structured call is
the right shape for that and `generation/compose.py` already is one. Giving
generation autonomy would let it re-decide things two agents and a gate already
settled.

WHAT EACH AGENT GETS, AND WHY IT vIS SO LITTLE. Every agent below is given the
narrowest toolset that lets it answer its question:

  * the curriculum agent gets the book, because its question is about the book;
  * the lesson designer does NOT get the book, because its question is about the
    trajectory through content curriculum reasoning has already settled, and a
    designer that can re-read the page will re-derive the content instead of
    designing the route through it;
  * the context agent gets neither the book nor the knowledge library, because
    section 9 says it may not redefine the academic contract and the cheapest way
    to guarantee that is to withhold the means.

Tools are additive in `create_deep_agent` and the filesystem tools always come
with it, so "narrow" here means narrow in DOMAIN tools. The permissions in
`permissions.py` are what bound the filesystem ones.

NO DECLARED SUBAGENTS IN V1. Architecture section 8: introduce prerequisite /
misconception / anchor analysts only if the reasoning becomes hard enough to
justify them, and it has not been measured to be yet. `deepagents` still offers
its built-in `general-purpose` delegate, which is left in place: it inherits this
agent's tools and permissions exactly, so it can isolate a long sub-investigation
into its own context window without being able to reach anything the parent
could not. Delegation that CHANGES what is reachable is what section 8 is
cautious about, and there is none of that here.
"""
from __future__ import annotations

from typing import Optional

from deepagents import create_deep_agent

from . import permissions as perms
from . import schemas
from . import skillset
from .middleware import AgentRun, build_middleware
from .model import build_model
from .tools import RunContext, book, knowledge, pedagogy, room
from .workspace import (MEMORY_SOURCES, SKILL_SOURCES, build_backend,
                        curriculum_artifact, experience_artifact,
                        notes_dir, reinforcement_artifact)

def _common(ctx: RunContext, *, record: AgentRun, max_model_calls: int,
            run_dir: Optional[str], temperature: float) -> dict:
    return {
        # `record.label` is the routing key. It is the same string the
        # provenance trace attributes the loop to and the same one
        # `prep_flow.routing` already carries on its HARD tier, so the
        # agents route by the name they were already known by.
        "model": build_model(temperature=temperature, label=record.label),
        "backend": build_backend(run_dir),
        "skills": SKILL_SOURCES,
        "memory": MEMORY_SOURCES,
        "middleware": build_middleware(record, max_model_calls=max_model_calls),
        "subagents": [],
        "checkpointer": False,
    }


# ── Agent 1: CurriculumReasoner ──────────────────────────────────────────────

_CURRICULUM_PROMPT = """You are a curriculum specialist reading one chapter of a Grade {grade} {subject} \
textbook before anybody plans a lesson from it.

You are NOT planning lessons and you are NOT writing material for children. You \
are answering the four questions the textbook itself does not answer:

    what must the class ALREADY be able to do before page 1 means anything
    which real objects in this room make it concrete
    what will the child wrongly conclude
    what must each topic LEAVE so the next one has a floor

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
TOPICS: {topic_count}, already cut and ordered from the book's own headings.

THE SKILLS FOR THIS STAGE, in load order:

{skills}

Load the first two before you do anything else, and the rest at the moment the
work needs them. Do not load all of them up front — a skill read before you have
a draft to hold it against is a skill you have skimmed.

HOW TO WORK

1. Load `learning-goals` and `core-pedagogy`, then `curriculum-reasoning`. The
   third has the rules, the chain-slip failure and the bounds.
2. Call `get_book_order`, then `get_cached_reasoning`. If a previous run already
   derived this chapter and it still lines up with the topic list, your job is to
   CHECK AND CORRECT it, not to replace it.
3. Call `lookup_grade_constraints`. What you write in `gained` is bound by it.
4. READ THE ACTUAL PAGES. `get_page_range` for the stretches you are reasoning
   about, `list_figures` for what is printed beside them, and `search_book`
   before every prerequisite - a "prerequisite" this chapter teaches on a later
   page is content, and listing it sends the class backwards.
5. Write your working notes to {notes} if it helps you think. Write your
   conclusion to {artifact}. Neither is your answer.
6. {gate}
7. Answer with the structured schema. That is the only thing that leaves you.

WHAT THIS STAGE OWES THE FIVE GOALS

The chain is where three of them are won or lost before anybody plans a period.
`assumes` is the below-grade child's starting line (G1) and a `gained` that
restates it has given the period no floor to stand on. An anchor object a child
cannot find at home cannot carry a take-home question (G4), so prefer the ones
that can. And exam coverage is only visible at chapter scale (G3) - you are the
only stage that sees all {topic_count} topics at once, so a printed question no
topic will ever meet is yours to notice.

WHAT GOOD LOOKS LIKE

Concrete. "Which side of the bottle you can see" helps a teacher; "develops
spatial reasoning" helps nobody. Every clause under 15 words. Misconceptions in
the child's own voice. One chain entry per topic, all {topic_count} of them, and
the middle line of every entry must MOVE - a topic whose `gained` restates its
own `assumes` has taught nothing.

Budget: about {budget} steps. Spend them reading the pages that decide something."""


def build_curriculum_agent(ctx: RunContext, *, run_dir: Optional[str] = None,
                           hitl: bool = False, allow_library_writes: bool = False,
                           max_model_calls: int = 14):
    """Agent 1. Chapter-scale pedagogical reasoning, grounded in the actual pages.

    `allow_library_writes` adds the one tool in this system with a side effect
    on shared data. Off unless a human is present to approve it — see
    `tools/knowledge.py`.
    """
    record = AgentRun(label="deep:curriculum")
    key = ctx.chapter_key()
    prompt = _CURRICULUM_PROMPT.format(
        grade=ctx.grade, subject=ctx.subject, chapter_title=ctx.chapter_title,
        topic_count=len(ctx.topics), artifact=curriculum_artifact(key),
        notes=notes_dir(key), budget=max_model_calls,
        skills=skillset.prompt_block(skillset.CURRICULUM, subject=ctx.subject),
        gate=skillset.gate_instruction(skillset.CURRICULUM, subject=ctx.subject))

    agent = create_deep_agent(
        tools=[*book.build(ctx),
               *knowledge.build(ctx, allow_writes=allow_library_writes),
               *pedagogy.build(ctx)],
        system_prompt=prompt,
        permissions=perms.curriculum_permissions(hitl=hitl),
        response_format=schemas.CurriculumReasoning,
        name="curriculum-reasoner",
        # Reasoning about what a chapter requires should not vary run to run.
        # Generation writes prose at 0.6; this decides facts.
        **_common(ctx, record=record, max_model_calls=max_model_calls,
                  run_dir=run_dir, temperature=0.25),
    )
    return agent, record


# ── Agent 2: LessonDesigner ──────────────────────────────────────────────────

_EXPERIENCE_PROMPT = """You are designing the LEARNING EXPERIENCE for topics of a Grade {grade} {subject} \
chapter.

You are not writing lessons and not choosing activities. You answer, per topic, \
the two questions standing between a learning goal and a lesson:

    1. what sequence of mental operations must a child actually go through
    2. which real object in this room makes that sequence happen

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}

WHAT IS ALREADY SETTLED AND IS NOT YOURS TO CHANGE

Curriculum reasoning has decided the prerequisites, the anchor pools, the
misconceptions and the knowledge chain. Each topic arrives carrying its own
`gained`, its `masteryTarget`, and an audited list of what the printed page does
not give. You design the route; you do not re-derive the destination.

You have no access to the textbook here, deliberately. If you find yourself
needing it, the thing you actually need is already in the topic you were given.

THE SKILLS FOR THIS STAGE, in load order:

{skills}

This is the longest list in the pipeline and that is not an accident: every one
of the five learning goals is won or lost in the period you are designing. Load
the first two before anything else, then the rest as the work reaches them.

HOW TO WORK

1. Load `learning-goals` and `core-pedagogy`, then `experience-design`. Load
   `lesson-design` for the period around the trajectory and `activity-design`
   before naming anything the class does.
2. Call `lookup_grade_constraints` and `get_room_constraints` before writing any
   `studentAction`. Up to sixty children, about 22 real teaching minutes,
   resource level {resource_level}.
3. Check that what you are about to plan has resourcing: `lookup_competencies`
   then `lookup_activities_for_competency`. An empty answer means the outcome has
   no activity behind it - say so rather than planning around a fiction.
4. Write to {artifact} if it helps.
5. {gate}
6. Answer with the schema.

THE THREE MARKS EVERY TRAJECTORY CARRIES

A trajectory is not just the hard move. Mark all three on it, because the two
you are not thinking about are the two that get dropped:

    the JUMP     the conceptual move this period exists for
    the FLOOR    what the slowest child leaves able to demonstrate - one
                 sentence, in a child's voice, physically checkable. It reaches
                 the SAME target by a shorter road; it is not a smaller target.
    the STRETCH  what the child who finishes at minute five is invited to do,
                 on the same content with the same materials

`scaffolding` is where the floor is built and `transferTask` is where the
stretch lives. One task, three depths - never a second track for the strugglers,
which is a period one teacher cannot run and which tells thirty children which
track they are on.

THE TWO FAILURES THAT MATTER

If two topics get the same trajectory with the nouns swapped, at least one is
wrong - go back to what the topic actually gains.

`inference` must BE this topic's `gained`, not the chapter's mastery target. The
target is the more interesting sentence and that is exactly why it gets written
here by mistake.

Pick ONE gap to close, from the audit you were given. Not three. Three closers in
one period delivers none of them.

Budget: about {budget} steps."""


def build_lesson_designer(ctx: RunContext, *, run_dir: Optional[str] = None,
                          hitl: bool = False, max_model_calls: int = 10):
    """Agent 2. Curriculum reasoning in, cognitive trajectories out.

    No book tools, on purpose — see the module docstring.
    """
    record = AgentRun(label="deep:experience")
    key = ctx.chapter_key()
    prompt = _EXPERIENCE_PROMPT.format(
        grade=ctx.grade, subject=ctx.subject, chapter_title=ctx.chapter_title,
        resource_level=ctx.resource_level, artifact=experience_artifact(key),
        budget=max_model_calls,
        skills=skillset.prompt_block(skillset.EXPERIENCE, subject=ctx.subject),
        gate=skillset.gate_instruction(skillset.EXPERIENCE, subject=ctx.subject))

    agent = create_deep_agent(
        tools=[*pedagogy.build(ctx), *knowledge.build(ctx, allow_writes=False)],
        system_prompt=prompt,
        permissions=perms.lesson_design_permissions(hitl=hitl),
        response_format=schemas.ExperienceBatch,
        name="lesson-designer",
        # A shade warmer than curriculum: a trajectory is a design and two good
        # ones can differ, where two prerequisite lists should not.
        **_common(ctx, record=record, max_model_calls=max_model_calls,
                  run_dir=run_dir, temperature=0.4),
    )
    return agent, record


# ── Agent 3: ContextReinforcer ───────────────────────────────────────────────

_CONTEXT_PROMPT = """You decide what THIS ROOM changes about how a lesson is delivered - and nothing else.

You produce a PATCH. Node 1 has already settled the mastery target, the required
concepts, the competencies and the textbook grounding, and you may not change any
of them. Generation composes the final material; you do not write it.

GRADE: {grade} | SUBJECT: {subject} | TOPICS IN THIS WINDOW: {topics}

THE SKILLS FOR THIS STAGE, in load order:

{skills}

A SHORTER LIST THAN THE OTHER STAGES, deliberately. What the period assesses,
sends home and offers a fast finisher is settled before you run. You adapt the
route; you do not touch the destination, so the skills that own those decisions
are not yours to act on.

HOW TO WORK

1. Load `learning-goals` and `core-pedagogy`, then `contextual-reinforcement`.
   The third lists every check the equity gate will run on your proposal, and
   knowing them is cheaper than being refused. Load `real-world-connection`
   before any local example and `language-support` if language is in play.
2. For each topic: `get_academic_contract` to see what must survive, then
   `get_active_context` to see what you may propose from.
3. `get_active_context` is the COMPLETE set of admissible factors. A factor not
   listed was ruled out deterministically before you were asked. Proposing from
   it will be rejected.
4. Before naming any material, `get_resource_profile`. Before any language
   adaptation, `get_language_profile`. Before any local example,
   `get_verified_local_context` - and if it is empty, use the textbook's own
   examples and invent no village detail.
5. Write to {artifact} if it helps.
6. {gate}
7. Answer with the schema.

THE RULES YOU WILL BE JUDGED ON

Never lower the standard. Adapt delivery, never reduce what a child ends up able
to do. `lowers_standard` defaults to TRUE so that forgetting it stops the plan.

A SECTION THAT IS TOO HARD FOR SOME CHILDREN IS NOT A SECTION THAT IS TOO HARD.
It is a differentiation problem, and the adaptation that fixes it WIDENS the
section at both ends - a real next step for the child who has not got it, a real
extension for the one who already has. Making it easier for the whole class to
lift a struggling group is the most common way this stage lowers a standard
while believing it did not.

The room and the child's own body need no provenance and are always available to
you (`real-world-connection`, tier 1). The village does. When
`get_verified_local_context` is empty, tier 1 is not a fallback - it is the
better answer, because it is a thing every child in the room can actually see.

Cite the exact profile field names you actually read. The gate looks them up. A
local claim on assumed evidence is a fabrication about a real place, and citing
nothing is better than citing a field you did not read.

Maximum three adaptations per topic. Exactly one section each.

AN EMPTY LIST IS A CORRECT ANSWER. A room with nothing recorded about it gets no
adaptations, and that is the system working. Say why in `note`.

Budget: about {budget} steps."""


def build_context_reinforcer(ctx: RunContext, *, topic_indexes: list[int],
                             run_dir: Optional[str] = None, hitl: bool = False,
                             max_model_calls: int = 8):
    """Agent 3. Node 2's one structured reasoning boundary.

    ONE call per lesson window, not a swarm — architecture section 19 and the
    closing paragraph of the blueprint both say so, and `context_flow` is built
    around it. The equity gate downstream is what makes this safe, and it is
    deterministic code, not another agent.
    """
    record = AgentRun(label="deep:context")
    key = f"{ctx.chapter_key()}-w{topic_indexes[0] if topic_indexes else 0}"
    prompt = _CONTEXT_PROMPT.format(
        grade=ctx.grade, subject=ctx.subject,
        topics=", ".join(f"T{i}" for i in topic_indexes) or "(none)",
        artifact=reinforcement_artifact(key), budget=max_model_calls,
        skills=skillset.prompt_block(skillset.CONTEXT, subject=ctx.subject),
        gate=skillset.gate_instruction(skillset.CONTEXT, subject=ctx.subject))

    agent = create_deep_agent(
        tools=room.build(ctx),
        system_prompt=prompt,
        permissions=perms.context_permissions(hitl=hitl),
        response_format=schemas.ContextReinforcement,
        name="context-reinforcer",
        **_common(ctx, record=record, max_model_calls=max_model_calls,
                  run_dir=run_dir, temperature=0.3),
    )
    return agent, record


# ── Agent 4: RepairInvestigator ──────────────────────────────────────────────
#
# THE GAP THIS FILLS. Generation's reasoning stage can read the book; the thing
# that FIXES its mistakes could not. `compose.repair_node` was handed the
# findings ("the Concept states a number that is not on these pages") and then
# rewrote the section without ever being able to look at the page and find the
# real one. So a grounding failure was repaired by guessing again, and the
# second guess failed the same check as often as not.
#
# This agent does not rewrite anything. It reads, and reports what the page
# actually says, and the existing repair prompt writes the fix with that in
# hand. Splitting it that way keeps the prose with the writer that is good at
# prose, and gives it the one thing it was missing.

_REPAIR_PROMPT = """A prep sheet for ONE period of a Grade {grade} {subject} chapter failed review, \
and you are the only stage that can open the book and check why.

CHAPTER: {chapter_title} | TOPIC: {topic} | PAGES: {pages}

WHAT FAILED — each line is a reviewer's finding against this sheet:
{findings}

THE SHEET AS WRITTEN:
{sheet}

YOUR JOB IS TO LOOK THINGS UP, NOT TO REWRITE. Someone else rewrites the sheet
using what you report. Every minute you spend phrasing a lesson is wasted; every
fact you bring back off the actual page is not.

For each finding that the BOOK can settle:
  * use get_book_page / get_page_range to read the pages this topic covers, and
    search_book when a fact might be printed somewhere else in the chapter
  * use list_figures / get_figure before saying anything about a picture — a
    sheet that points thirty children at a picture which is not on that page is
    the failure a teacher meets mid-lesson
  * copy what the page actually prints into "verbatim", exactly. A number, a
    term, a caption. Do not paraphrase it and do not improve it.
  * say in "correction" what the sheet should say instead — the real value, in
    one sentence

WHEN THE CHAPTER SIMPLY DOES NOT CONTAIN IT, say so: empty "verbatim", and a
"correction" that tells the rewrite to drop the claim rather than restate it.
An invented fact here is worse than the failure you were sent to fix, because it
arrives wearing the authority of having been checked.

FINDINGS THE BOOK CANNOT SETTLE — pacing, staging, whether sixty children fit
the activity, whether the language is too hard — are not yours. List them under
"unverifiable" with one line saying why, and move on. You have {budget} model
steps; spend them reading, not deliberating."""


def build_repair_agent(ctx: RunContext, *, topic: str, pages: str,
                       findings: str, sheet: str,
                       run_dir: Optional[str] = None,
                       max_model_calls: int = 8):
    """Agent 4. Reads the pages behind a failed sheet and reports the facts.

    Read-only by permission (`repair_permissions`) and by output shape: it
    returns `RepairEvidence`, never a sheet. Budget is deliberately smaller
    than the two reasoners' — this answers a handful of specific questions
    about a handful of pages, where they reason about a whole chapter.

    Given only the `book` tools. `pedagogy` and `room` are withheld on purpose:
    the findings this agent is meant to settle are the ones the PAGE decides,
    and an agent holding the activity bank starts redesigning the lesson it was
    sent to fact-check.
    """
    record = AgentRun(label="deep:repair")
    prompt = _REPAIR_PROMPT.format(
        grade=ctx.grade, subject=ctx.subject, chapter_title=ctx.chapter_title,
        topic=topic, pages=pages, findings=findings, sheet=sheet,
        budget=max_model_calls)

    agent = create_deep_agent(
        tools=[*book.build(ctx)],
        system_prompt=prompt,
        permissions=perms.repair_permissions(),
        response_format=schemas.RepairEvidence,
        name="repair-investigator",
        # Lower than either reasoner. This reports what a page says; two runs
        # disagreeing about a printed number is a defect, not variation.
        **_common(ctx, record=record, max_model_calls=max_model_calls,
                  run_dir=run_dir, temperature=0.1),
    )
    return agent, record
