"""Where the agents meet the pipeline: three functions with `call_json`'s shape.

THE INTEGRATION IS DELIBERATELY TINY, and it is the most important design
decision in this package. Each function below returns the SAME raw dict that the
`call_json` it replaces returned - unnormalised, unbounded, unsealed. Everything
the existing nodes do around that call still happens:

    reasoning_node      cache read, chain gate, one re-derive, mastery pass,
                        cache write, the standing-still refusal, the metrics
    experience_node     the anchor snap-back, the mastery-target correction,
                        the per-topic fallback, the kind counts
    context propose()   plan.normalise, the confidence clamp, and then the whole
                        deterministic equity gate

So switching a run to agents changes WHERE the judgement came from and nothing
about what is done to it afterwards. That is what makes the A/B honest: two runs
of the same chapter differ in reasoning quality, not in plumbing, and every
invariant the pipeline already guarantees is still guaranteed.

It also means a bad agent cannot do novel damage. An agent that returns a
degenerate chain meets the same gate that catches a degenerate `call_json`
chain, and a chapter whose chain stands still on a quarter of its topics still
refuses to cache itself.

FAILURE IS NOT AN EXCEPTION HERE. Architecture section 21: every failure routes
to persist. These functions raise, because that is what `call_json` does and the
callers already have the `except` blocks that turn it into an error entry and a
degraded-but-usable state. Adding a second failure convention would mean the
callers needed two.
"""
from __future__ import annotations

from typing import Any, Optional

from .middleware import AgentRun
from .model import labelled
from .registry import (build_context_reinforcer, build_curriculum_agent,
                       build_lesson_designer, build_repair_agent)
from .tools import RunContext

# Recorded per process so a caller can fold agent shape into the run trace
# without threading a record object through four call sites. Appended to, never
# read for control flow — see `drain_runs`.
_RUNS: list[AgentRun] = []


def drain_runs() -> list[dict]:
    """Take the agent-shape records accumulated since the last drain.

    `prep_flow/provenance.py` builds its trace at persist time from state alone.
    Agent loops are not in state, so this is where their shape - calls, tools,
    tokens, whether the budget cut them short - is collected for it.
    """
    out = [record.as_dict() for record in _RUNS]
    _RUNS.clear()
    return out


def _context_from_state(state: dict, *, topics: Optional[list] = None) -> RunContext:
    """Node 1's ChapterState, as the tools need to see it."""
    settings = state.get("teacher_settings") or {}
    return RunContext(
        chapter_markdown=state.get("chapter_markdown") or "",
        chapter_title=state.get("chapter_title") or "",
        chapter_number=state.get("chapter_number"),
        chapter_figures=state.get("chapter_figures") or [],
        page_start=state.get("page_start"),
        page_end=state.get("page_end"),
        grade=str(state.get("grade") or ""),
        subject=state.get("subject") or "",
        school_id=state.get("school_id"),
        class_id=state.get("class_id"),
        resource_level=int(settings.get("resourceLevel", 0) or 0),
        teacher_settings=settings,
        topics=topics if topics is not None else (state.get("topics") or []),
    )


async def _run(agent, record: AgentRun, instruction: str) -> Any:
    """Invoke, label the model calls, keep the record, return the structured answer."""
    _RUNS.append(record)
    with labelled(record.label):
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": instruction}]})

    answer = result.get("structured_response")
    if answer is None:
        # The harness validates the schema, so "no structured response" means the
        # agent stopped without producing one — a budget cut, a refusal, or a
        # provider error that survived the retry. Raised rather than defaulted:
        # the caller's `except` writes an error the run carries, and a silently
        # empty answer would be indistinguishable from a chapter that genuinely
        # had nothing to say.
        raise RuntimeError(
            f"[{record.label}] produced no structured answer after "
            f"{record.model_calls} model call(s)"
            + (" (step budget exhausted)" if record.truncated else ""))
    return answer


# ── Node 1, curriculum ───────────────────────────────────────────────────────

async def derive_curriculum_reasoning(state: dict) -> dict:
    """Drop-in for `reasoning_node`'s `call_json`. Returns the raw data dict.

    Keys match `agents/reasoning._normalise` exactly: prerequisites, anchors,
    misconceptions, chain. The chain comes back as a LIST here, which is what
    `_normalise` reads (it indexes by `index` with a positional fallback).
    """
    topics = state.get("topics") or []
    ctx = _context_from_state(state, topics=topics)
    config = state.get("config") or {}
    agent, record = build_curriculum_agent(
        ctx,
        run_dir=config.get("deep_agents_workspace"),
        hitl=bool(config.get("deep_agents_hitl")),
        allow_library_writes=bool(config.get("deep_agents_library_writes")),
        max_model_calls=int(config.get("deep_agents_max_steps", 14)),
    )
    answer = await _run(agent, record, (
        f"Derive the curriculum reasoning for '{ctx.chapter_title}' "
        f"(Grade {ctx.grade} {ctx.subject}), covering all {len(topics)} topics. "
        "Read the actual pages before you assert anything."))

    return {
        "prerequisites": list(answer.prerequisites),
        "anchors": [group.model_dump() for group in answer.anchors],
        "misconceptions": [m.model_dump() for m in answer.misconceptions],
        "chain": [entry.model_dump() for entry in answer.chain],
    }


# ── Node 1, lesson design ────────────────────────────────────────────────────

async def derive_experience_plans(state: dict, window: list[dict]) -> dict:
    """Drop-in for `experience_node`'s `call_json`. Returns `{"plans": [...]}`.

    The window is passed rather than read off state because the node batches:
    the seam between two consecutive periods has to be written from both sides
    in one place, and that is a property of the window, not the chapter.
    """
    ctx = _context_from_state(state, topics=window)
    config = state.get("config") or {}
    agent, record = build_lesson_designer(
        ctx,
        run_dir=config.get("deep_agents_workspace"),
        hitl=bool(config.get("deep_agents_hitl")),
        max_model_calls=int(config.get("deep_agents_max_steps_design", 10)),
    )

    reasoning = state.get("reasoning") or {}
    lines = []
    for spec in window:
        slice_ = spec.get("reasoning") or {}
        lines.append(
            f"T{spec.get('index')}: {spec.get('topic')} / {spec.get('subtopic', '')}\n"
            f"    gained:        {slice_.get('gained') or '(not derived)'}\n"
            f"    masteryTarget: {slice_.get('masteryTarget') or '(not derived)'}\n"
            f"    anchors:       {', '.join(slice_.get('anchors') or []) or '(none)'}\n"
            f"    the page does not give: "
            f"{'; '.join(str(m) for m in (slice_.get('missing') or [])) or '(nothing audited)'}")

    answer = await _run(agent, record, (
        "Design the learning experience for these topics. One plan each, in this "
        "order. Everything below was already settled upstream and is not yours "
        "to change.\n\n"
        f"CHAPTER PREREQUISITES: {', '.join(reasoning.get('prerequisites') or []) or '(none)'}\n\n"
        + "\n\n".join(lines)))

    return {"plans": [plan.model_dump() for plan in answer.plans]}


# ── Node 2, context reinforcement ────────────────────────────────────────────

async def derive_context_plan(
    *, rows: list[dict], profile: dict, activation_summaries: dict,
    contract: dict, grade: str, subject: str,
    config: Optional[dict] = None, policy_block: str = "",
) -> dict:
    """Drop-in for `context_flow.reasoning.propose`'s `call_json`.

    Returns the raw dict `context_flow.plan.normalise` reads. The caller
    normalises it and then runs the equity gate, exactly as before - this
    function has no more authority than the model call it replaces.

    `activation_summaries` is `{topic index: activation.summarise(...)}`, the
    output of the deterministic activation the node already ran. Passing the
    SUMMARIES rather than the `Activation` objects is deliberate: the summary is
    what says which factors are proposable, and it is the only part of activation
    an agent is allowed to see.

    `policy_block` is the reinforcement policy's text, ALREADY RENDERED by
    `reinforcement.render.prompt_block` and handed over as a string. Passed
    rather than derived here for the reason that module's docstring gives: the
    liveness proof asserts that this text reaches the model, and it can only
    assert that honestly while there is one renderer. Without this the loop's
    text route would quietly stop working the moment the agent seam is turned
    on — the dials would keep re-ranking proposals, and the directives that
    change what gets proposed at all would reach nothing.
    """
    config = config or {}
    indexes = [int(r.get("index")) for r in rows if r.get("index") is not None]
    ctx = RunContext(
        grade=str(grade), subject=subject,
        contract=contract or {},
        profile=profile or {},
        activation=activation_summaries or {},
        topics=rows,
    )
    agent, record = build_context_reinforcer(
        ctx, topic_indexes=indexes,
        run_dir=config.get("deep_agents_workspace"),
        hitl=bool(config.get("deep_agents_hitl")),
        max_model_calls=int(config.get("deep_agents_max_steps_context", 8)),
    )
    # HOW MANY FACTORS ACTUALLY HAVE SOMETHING TO SAY, decided already by the
    # deterministic activation this agent is not allowed to second-guess.
    proposable = sorted({
        factor
        for summary in (activation_summaries or {}).values()
        for factor in (summary.get("proposable") or summary.get("active") or [])
    })

    # WHY THE ESCAPE CLAUSE IS NOW CONDITIONAL. This asked, unconditionally, for
    # a decision and then added "An empty list is a correct answer." On the
    # first real run the agent spent 40,615 prompt tokens across ten tool calls,
    # wrote 134 completion tokens, and returned nothing — with fifteen factors
    # active and a profile that the same prompt through a plain call turned into
    # seven adaptations, every one of which Node 3 later confirmed had landed.
    # Its note said "no relevant active context factors", which was false.
    #
    # The clause still exists, because a room that recorded nothing must produce
    # nothing and inventing context is the failure this whole layer guards
    # against. But it is now offered only when activation actually found nothing
    # to propose from. Where factors ARE active, passing over one is a judgement
    # the agent has to make in writing rather than a default it can fall into.
    if proposable:
        closing = (
            f"ACTIVATION HAS ALREADY DECIDED WHAT MAY SPEAK. These factors are "
            f"active for these topics because this school recorded evidence for "
            f"them: {', '.join(proposable)}. Work through them. Propose a change "
            f"wherever the recorded evidence genuinely bears on the topic, and "
            f"where an active factor warrants no change, say so and why in "
            f"`note`. Returning nothing at all, with factors active, is an answer "
            f"you have to justify — not a default.")
    else:
        closing = ("Activation found nothing to propose from for these topics, "
                   "so an empty list is the correct answer.")

    answer = await _run(agent, record, (
        "Decide what this room changes about how these topics are delivered: "
        + ", ".join(f"T{i}" for i in indexes) + ". "
        "Check each topic's contract and its active context before proposing "
        "anything. " + closing
        + (("\n\n" + policy_block.strip()) if (policy_block or "").strip() else "")))

    return {
        "adaptations": [a.model_dump() for a in answer.adaptations],
        "preserve": list(answer.preserve),
        "lowers_standard": bool(answer.lowers_standard),
        "note": answer.note,
    }


# ── Repair evidence (Agent 4) ────────────────────────────────────────────────

def _evidence_block(answer) -> str:
    """The agent's findings as the repair prompt wants to read them.

    Rendered here rather than in `compose.py` so the prompt-facing shape lives
    next to the schema it is rendered from: a field renamed in `schemas.py`
    breaks this function, which is visible, instead of silently producing an
    empty block in a prompt nobody reads.
    """
    lines: list[str] = []
    for item in (answer.evidence or []):
        where = f" (Page {item.page})" if item.page is not None else ""
        section = f"[{item.section}] " if item.section else ""
        lines.append(f"{section}{item.about}{where}")
        if item.verbatim:
            lines.append(f'    the page actually prints: "{item.verbatim}"')
        else:
            lines.append("    NOT FOUND ANYWHERE IN THIS CHAPTER — drop the claim, "
                         "do not restate it in other words")
        if item.correction:
            lines.append(f"    so the sheet should say: {item.correction}")
    for note in (answer.unverifiable or []):
        lines.append(f"(not checkable from the book: {note})")
    return "\n".join(lines)


async def gather_repair_evidence(state: dict, *, index: int, topic: str,
                                 pages: str, findings: str, sheet: str) -> str:
    """What the pages actually say, for one sheet that failed review.

    Returns a prompt-ready block, or "" when the agent could not run or found
    nothing worth reporting. BEST-EFFORT BY CONTRACT: repair already works
    without this — it just works blind — so every failure here degrades to the
    old behaviour rather than costing the topic its rewrite.
    """
    ctx = _context_from_state(state, topics=state.get("topics"))
    config = state.get("config") or {}
    agent, record = build_repair_agent(
        ctx, topic=topic, pages=pages, findings=findings, sheet=sheet,
        run_dir=config.get("deep_agents_workspace"),
        max_model_calls=int(config.get("deep_agents_max_steps_repair", 8)),
    )
    answer = await _run(agent, record, (
        f"Check the findings against the actual pages for '{topic}' "
        f"(pages {pages}). Read before you answer."))
    return _evidence_block(answer) if answer is not None else ""
