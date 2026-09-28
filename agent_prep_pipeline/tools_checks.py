"""The live pipeline's own checks, wrapped as tools a Checker agent can choose
to call -- instead of the fixed sequence `validation_flow/graph.py` always
runs in the same order. Nothing here reimplements a check; every tool is a
thin closure around the exact function `validation_flow/checks.py` (or
`realism.py` / `diagnostic.py` / `app/lib/timing_check.py` /
`app/lib/dedup_check.py`) already uses in production, so a finding from this
package means the same thing a live Node 3 finding means.

CHEAP_CHECK_NAMES / COSTLY_CHECK_NAMES are named as constants rather than
inferred from the tool objects (e.g. "is it async") because that reflection
is a langchain-version-dependent detail this package should not depend on --
the split is a property of what each check actually costs, not of its
Python shape, and stating it directly is both simpler and more honest about
where the money goes: the *_CHECK_NAMES lists are what the Checker agent's
prompt reads to know which tools are free to call and which cost a real
model call each.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from langchain_core.tools import BaseTool, tool

from validation_flow import checks as vchecks
from validation_flow import realism as vrealism
from validation_flow import diagnostic as vdiagnostic
from prep_flow.moves import book_activity
from app.lib import timing_check as tck
from app.lib import dedup_check as ddk

CHEAP_CHECK_NAMES = [
    "check_structure", "check_handoff", "check_explore_voice",
    "check_concept_grounding", "check_competency_alignment",
    "check_book_fidelity", "check_pedagogy_rules",
]
COSTLY_CHECK_NAMES = [
    "check_realism", "check_diagnostic_thinking", "check_timing", "check_duplication",
]


@dataclass
class TopicContext:
    """One topic's material plus everything the wrapped checks need to judge
    it -- the same inputs `validation_flow.checks.validation_node` gathers
    per topic, just held here instead of read off LangGraph state.

    `spec` is the topic's row from Node 1's OWN `state["topics"]` (carries
    `excerpt`, `moves`, etc.) -- this is deliberately NOT the contract's topic
    row; `validation_node` reads `spec.get("excerpt")` off exactly this
    collection, and using the contract row here would silently check against
    the wrong object. `contract_row` is kept separately, only for the one
    thing that lives there and nowhere else: `experiencePlan.closesGap`
    (see `generate_with_textbook_critique.py`'s `_inject_gaps` docstring for
    why the two are not interchangeable).
    """

    material: dict
    spec: dict
    contract_row: dict = field(default_factory=dict)
    chapter_text: str = ""
    grade: str = ""
    subject: str = ""
    topic: str = ""
    is_first: bool = True
    is_last: bool = True
    plan: Optional[dict] = None
    activity_name: Optional[str] = None


def _fmt(findings: list[dict]) -> str:
    if not findings:
        return "No findings."
    lines = []
    for f in findings:
        lines.append(f"[{f.get('severity', '?')}] ({f.get('check', '?')}) "
                     f"{f.get('section') or '(whole sheet)'}: {f.get('message', '')}")
    return "\n".join(lines)


def _fmt_dedup(result: dict) -> str:
    findings = result.get("findings") or []
    if not findings:
        return f"Topic complexity: {result.get('topicComplexity')}\nNo duplication found."
    lines = [f"Topic complexity: {result.get('topicComplexity')}"]
    for f in findings:
        lines.append(f"  '{f.get('idea')}' -- keep in {f.get('keepIn')}, "
                     f"trim from {f.get('trimFrom')}: {f.get('reason')}")
    return "\n".join(lines)


def build(ctx: TopicContext) -> list[BaseTool]:
    """The checker's toolset, bound to one topic's current material."""

    @tool
    def check_structure() -> str:
        """FREE. Are all six sections present, with the right bullet count and
        word caps? Run this first -- it catches the most common failures and
        costs nothing beyond reading the material."""
        return _fmt(vchecks.check_structure(ctx.material, ctx.is_first))

    @tool
    def check_handoff() -> str:
        """FREE. Does Explore's last point leave the next Refresher something
        specific to recall by name tomorrow?"""
        return _fmt(vchecks.check_handoff(ctx.material, ctx.plan or {}, ctx.is_last))

    @tool
    def check_explore_voice() -> str:
        """FREE. Does every Explore point report the teacher telling the class
        ("Tell students that...") rather than commanding the child directly?
        Also checks the board-sketch phrase names something Explore's own
        points actually mention."""
        return _fmt(vchecks.check_explore_voice(ctx.material))

    @tool
    def check_concept_grounding() -> str:
        """FREE. Is every number/fact Concept states actually on this topic's
        textbook pages, not invented or drifted from a nearby page?"""
        return _fmt(vchecks.check_concept_grounding(
            ctx.material, ctx.spec.get("excerpt") or "", ctx.chapter_text))

    @tool
    def check_competency_alignment() -> str:
        """FREE. Does the sheet actually build the competencies this topic is
        supposed to build, not just mention them?"""
        return _fmt(vchecks.check_competency_alignment(ctx.material, ctx.spec))

    @tool
    def check_book_fidelity() -> str:
        """FREE. Did the sheet drop or swap out something the textbook page
        itself covers for this topic?"""
        return _fmt(vchecks.check_book_fidelity(ctx.material, ctx.spec))

    @tool
    def check_pedagogy_rules() -> str:
        """FREE. Student-action-first bullets, no assessment vocabulary, one
        consistent currency, the activity named as actually selected."""
        return _fmt(vchecks.check_pedagogy_rules(
            ctx.material, ctx.activity_name, ctx.grade,
            book_activity(ctx.spec.get("moves") or []), is_first=ctx.is_first))

    @tool
    async def check_realism() -> str:
        """COSTS A REAL MODEL CALL. Would this genuinely work in a room of
        30-60 children with one teacher and a blackboard -- distribution
        time, wait time, mechanical accuracy of any hands-on demo? Reach for
        this when the free structural checks are clean but you still have a
        specific doubt about a hands-on section."""
        # _judge_one returns (index, {"findings": [raw dicts]}) -- NOT a bare
        # findings list. Caught for real on a live run: unpacking straight
        # into `findings` made `_fmt` iterate a dict's KEYS ('findings'
        # itself, as a string) instead of its rows, crashing on `.get`. Fixed
        # here rather than papered over, and formatted with realism's own
        # raw keys (dimension/problem/fix), which `_fmt`'s generic shape
        # (severity/check/section/message) does not match either.
        _, data = await vrealism._judge_one(0, ctx.spec, ctx.material, ctx.grade)
        raw = [r for r in (data.get("findings") or []) if isinstance(r, dict) and r.get("problem")]
        if not raw:
            return "No findings."
        lines = []
        for r in raw:
            severity = "blocking" if str(r.get("severity")).lower() == "blocking" else "advisory"
            fix = str(r.get("fix") or "").strip()
            lines.append(
                f"[{severity}] ({r.get('dimension') or 'focus'}) "
                f"{r.get('section') or '(whole sheet)'}: {r.get('problem')}"
                + (f" Fix: {fix}" if fix else ""))
        return "\n".join(lines)

    @tool
    async def check_diagnostic_thinking() -> str:
        """COSTS A REAL MODEL CALL. Only meaningful if this topic has a named
        misconception gap to close -- check the topic profile you were given
        first. If it does, does the sheet contain an explicit if-then
        response a teacher could actually say to a confused child?"""
        gap_row = (ctx.contract_row.get("experiencePlan") or {}).get("closesGap") or {}
        if not (gap_row.get("gap") and gap_row.get("closer")):
            return "This topic has no named gap to close -- this check does not apply, skip it."
        # _judge_one's own prompt-builder reads `gapCloser`, not `closer` --
        # the contract's own closesGap shape uses `closer` (see
        # generate_with_textbook_critique.py's `_inject_gaps`).
        # `validation_flow/adapter.py`'s `_experience()` does this exact
        # rename for the live pipeline's own call; skipping it here meant
        # this check was silently asking about an empty gap-closer on every
        # real call, well before today's crash.
        translated = {"gap": gap_row.get("gap") or "", "gapCloser": gap_row.get("closer") or ""}
        # _judge_one returns (index, data) where `data` is the single verdict
        # object {"delivered", "section", "reason"} -- not a findings list.
        # Same class of bug as check_realism above: unpacking it as
        # `findings` and handing it to `_fmt` iterated its KEYS as strings.
        _, data = await vdiagnostic._judge_one(0, translated, ctx.material, ctx.topic)
        if data.get("delivered"):
            return "Delivered -- the sheet gives an explicit if-then response for the named gap."
        reason = str(data.get("reason") or "").strip()
        section = str(data.get("section") or "").strip() or "(unclear which section)"
        return f"[blocking] (diagnostic_thinking) {section}: gap not delivered. {reason}"

    @tool
    async def check_timing() -> str:
        """COSTS A REAL MODEL CALL. Does any section finish with real minutes
        left on the clock and nothing for the teacher to fill it with?"""
        result = await tck.check_timing(
            ctx.material, topic=ctx.topic, grade=ctx.grade, subject=ctx.subject)
        lines = [f"Topic complexity: {result.get('topicComplexity')} "
                 f"({result.get('complexityReason')})"]
        for row in result.get("sections") or []:
            extra = row.get("extra")
            lines.append(
                f"  {row.get('section')}: allocated {row.get('allocatedMinutes')}, "
                f"estimated {row.get('estimatedMinutes')}"
                + (f" -- suggested extra: {extra.get('text')}" if extra else ""))
        return "\n".join(lines)

    @tool
    async def check_duplication() -> str:
        """COSTS A REAL MODEL CALL. Is the same idea wastefully repeated
        across sections, or is it genuine complexity-justified reinforcement
        from a different angle?"""
        result = await ddk.check_duplication(
            ctx.material, topic=ctx.topic, grade=ctx.grade, subject=ctx.subject)
        return _fmt_dedup(result)

    return [check_structure, check_handoff, check_explore_voice, check_concept_grounding,
            check_competency_alignment, check_book_fidelity, check_pedagogy_rules,
            check_realism, check_diagnostic_thinking, check_timing, check_duplication]
