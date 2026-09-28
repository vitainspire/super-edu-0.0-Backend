"""The two new agents, built the same way `deep_agents/registry.py` builds
its four -- same `create_deep_agent`, same model/middleware plumbing, same
`response_format`-as-structured-answer contract. Nothing in that module is
edited; this is a sibling registry for a sibling pair of agents.

    Checker   decides WHICH of tools_checks.py's checks this sheet needs,
              runs them, and rules pass/fail -- generalises deep_agents's
              Agent 4 (RepairInvestigator) pattern from "investigate a
              finding" to "decide what to investigate in the first place".
    Fixer     given the checker's findings, may look up the real textbook
              pages (deep_agents' own book tools, reused as-is) before
              rewriting only the sections that were actually flagged.

Both are read-only against the filesystem (`_deny_all_writes`) -- this
experiment writes nothing to `/workspace/`, and StateBackend means there is
nothing on disk to clean up either.

NO `/skills/` MOUNT FOR EITHER, ON PURPOSE -- unlike the Writer in
`agent_prep_pipeline_task_agents`, which reuses `deep_agents`' EXPERIENCE
stage's full skill routing because it is making the same open-ended
compositional judgement calls LessonDesigner makes. The Checker and Fixer
are not: by the time either runs, every pedagogical judgement (the floor,
the anchor, the trajectory, the gap) is already settled and handed down on
the contract, and both agents' job is to audit or patch compliance with
`SECTION_POLICY`'s own already-concrete rule text -- the same reason
`deep_agents/registry.py`'s own docstring gives for generation staying
un-agentic in the first place, one level down. `/memory/AGENTS.md` (the
system-orientation file) IS mounted for both, though -- cheap, and it
reinforces exactly the boundary that matters here: don't redecide what Node
1 already settled.
"""
from __future__ import annotations

import json
from typing import Optional

from deepagents import create_deep_agent
from deepagents.backends import StateBackend

from deep_agents.workspace import MEMORY_SOURCES

from deep_agents.middleware import AgentRun, build_middleware
from deep_agents.model import build_model
from deep_agents.tools import book as book_tools
from deep_agents.tools.runtime import RunContext as BookContext
from prep_flow.sections import BULLETS_PER_SECTION, SECTION_LABELS, SECTION_POLICY

from . import schemas
from .tools_checks import CHEAP_CHECK_NAMES, COSTLY_CHECK_NAMES, TopicContext
from .tools_checks import build as build_check_tools

# A finding's `detail` over this many words is blocking in the live
# `validation_flow/checks.py` -- restated here so the Fixer is held to the
# same ceiling the sheet was originally written under, not a looser one.
_WORD_CAP = 40


def _deny_all_writes() -> list:
    from deepagents import FilesystemPermission
    return [FilesystemPermission(operations=["write"], paths=["/**"], mode="deny")]


async def invoke_structured(agent, instruction: str):
    """Same contract as `deep_agents/bridge.py`'s `_run`: raise rather than
    return None on a budget-exhausted or refused loop, so the caller's own
    error handling (not a silent empty answer) is what a run degrades to."""
    result = await agent.ainvoke({"messages": [{"role": "user", "content": instruction}]})
    answer = result.get("structured_response")
    if answer is None:
        raise RuntimeError(
            "agent produced no structured answer (step budget exhausted or a refusal)")
    return answer


# ── Checker ───────────────────────────────────────────────────────────────

_CHECKER_PROMPT = """You are the quality gate for ONE topic's prep-material sheet -- \
Grade {grade} {subject}, "{topic}".

You are NOT the writer and you have no tool to rewrite anything. Your only job is to \
decide, using the tools below, whether this sheet is ready to hand a teacher as printed.

UNLIKE A FIXED CHECKLIST that always runs every check in the same order, you decide \
which checks THIS sheet actually needs, and in what order -- but a check you skip needs \
a real reason stated in your final "reasoning", not just haste.

FREE checks (no extra cost beyond reading the material) -- normally run all of them: \
{cheap}

CHECKS THAT COST A REAL MODEL CALL EACH -- reach for these when the free checks are \
clean and you still have a specific, stated doubt, or when the topic profile below \
makes one obviously relevant (e.g. only call check_diagnostic_thinking if this topic \
actually has a named gap to close): {costly}

TOPIC PROFILE:
{profile}

A finding is BLOCKING if a teacher reading this sheet as printed would hit a real \
problem in the room: a wrong fact, an instruction that cannot actually be followed, a \
section that reads as homework when the rule forbids that, a missing or malformed \
bullet. Mark something ADVISORY, not blocking, when it is a genuine improvement but the \
sheet is still usable without it -- inventing blocking findings to look thorough helps \
no one and costs a real repair cycle.

Call check_structure first; it is free and catches the most common failures outright. \
Then decide what else this specific sheet needs. When you are done, answer with the \
schema. passed=True only when no blocking finding remains outstanding."""


def build_checker_agent(ctx: TopicContext, *, grade: str, subject: str,
                        max_model_calls: int = 8):
    record = AgentRun(label="agentpipe:checker")
    tools = build_check_tools(ctx)
    gap = (ctx.contract_row.get("experiencePlan") or {}).get("closesGap") or {}
    profile = (
        f"materialsUsed: {ctx.material.get('materialsUsed')}\n"
        f"named gap to close: {gap.get('gap') or '(none for this topic)'}"
    )
    prompt = _CHECKER_PROMPT.format(
        grade=grade, subject=subject, topic=ctx.topic,
        cheap=", ".join(CHEAP_CHECK_NAMES), costly=", ".join(COSTLY_CHECK_NAMES),
        profile=profile)

    agent = create_deep_agent(
        tools=tools,
        system_prompt=prompt,
        permissions=_deny_all_writes(),
        response_format=schemas.CheckerVerdict,
        name="prep-checker",
        model=build_model(temperature=0.1, label=record.label),
        backend=StateBackend(),
        skills=[],
        memory=MEMORY_SOURCES,
        middleware=build_middleware(record, max_model_calls=max_model_calls),
        subagents=[],
        checkpointer=False,
    )
    return agent, record


# ── Fixer ─────────────────────────────────────────────────────────────────

_FIXER_PROMPT = """A prep sheet for ONE period of a Grade {grade} {subject} chapter failed \
review, and you are the one who rewrites it.

CHAPTER: {chapter_title} | TOPIC: {topic} | PAGES: {pages}

WHAT THE CHECKER FOUND (fix every BLOCKING one; advisory ones are your judgement call):
{findings}

THE RULE FOR EVERY SECTION -- follow the rule for whichever section(s) you touch, \
character for character in spirit:

{section_rules}

THE SHEET AS WRITTEN (JSON):
{sheet}

You may look up the real textbook pages (get_page_range, get_book_page, search_book) \
before rewriting anything a grounding finding touched -- a grounding failure is repaired \
by finding the real number or fact on the page, never by guessing a plausible one. A \
voice/structure/duplication finding needs no book lookup at all -- just rewrite the \
section to the rule above.

TWO DIFFERENT KINDS OF FINDING NEED TWO DIFFERENT RESPONSES. A structural, voice, \
grounding, or duplication finding means the section itself is wrong -- rewrite it and put \
it in `revisions`. A check_timing finding (a section that genuinely finishes early with \
real minutes unfilled) is NOT a defect in the section -- do not rewrite it. Instead \
propose an optional, never-required stretch task in the separate `extra` field. Never put \
the same section in both places for the same reason.

Return ONLY the sections you actually rewrote in `revisions` -- leave every section the \
checker did not flag out of the list entirely. Keep exactly {bullets} points per section \
you touch, and keep every point's detail under {word_cap} words."""


def build_fixer_agent(ctx: TopicContext, *, grade: str, subject: str,
                      chapter_title: str, chapter_markdown: str,
                      chapter_figures: Optional[list], findings_text: str,
                      max_model_calls: int = 8):
    record = AgentRun(label="agentpipe:fixer")
    book_ctx = BookContext(
        chapter_markdown=chapter_markdown, chapter_title=chapter_title,
        chapter_figures=chapter_figures or [], grade=grade, subject=subject,
    )
    section_rules = "\n\n".join(
        f"{SECTION_LABELS[key]} ({key}): {policy['rule']}"
        for key, policy in SECTION_POLICY.items()
    )
    grounding = ctx.spec.get("grounding") or ctx.contract_row.get("grounding") or {}
    pages = f"{grounding.get('pageStart', '?')}-{grounding.get('pageEnd', '?')}"
    prompt = _FIXER_PROMPT.format(
        grade=grade, subject=subject, chapter_title=chapter_title, topic=ctx.topic,
        pages=pages, findings=findings_text, section_rules=section_rules,
        sheet=json.dumps(ctx.material, indent=2, ensure_ascii=False),
        bullets=BULLETS_PER_SECTION, word_cap=_WORD_CAP)

    agent = create_deep_agent(
        tools=book_tools.build(book_ctx),
        system_prompt=prompt,
        permissions=_deny_all_writes(),
        response_format=schemas.FixerOutput,
        name="prep-fixer",
        # A shade warmer than the checker: rewriting prose is a judgement
        # call, not a fact lookup -- matches generation's own 0.55-0.6 band
        # for repair/composition rather than the checker's near-zero.
        model=build_model(temperature=0.5, label=record.label),
        backend=StateBackend(),
        skills=[],
        memory=MEMORY_SOURCES,
        middleware=build_middleware(record, max_model_calls=max_model_calls),
        subagents=[],
        checkpointer=False,
    )
    return agent, record
