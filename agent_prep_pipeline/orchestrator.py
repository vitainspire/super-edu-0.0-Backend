"""Creator's output in, Node-3-equivalent quality gate out -- but the
checking and the fixing are genuinely autonomous agents instead of a fixed
LangGraph node sequence. This is the one function the rest of this package
exists to support.

SAFETY_CAP_ROUNDS IS A HARD CEILING REGARDLESS OF WHAT EITHER AGENT DECIDES.
The whole point of this experiment is letting the agents choose WHAT to
check and HOW to fix it -- not letting the round count itself become
unbounded. `validation_flow/graph.py`'s fixed pipeline caps repair at
`MAX_REPAIR_ROUNDS = 2`; this is deliberately a little looser (3) because an
autonomous checker may reasonably want a clean re-check after a narrow fix
that the fixed pipeline would not have offered a separate round for, but it
is still a hard number, not "however many the agents feel like" -- the
budget-consciousness this whole session has been built around does not stop
applying just because a step became autonomous.
"""
from __future__ import annotations

import json
from typing import Optional

from prep_flow.sections import SECTION_ORDER, _CONTAINERS

from . import registry
from .tools_checks import TopicContext

SAFETY_CAP_ROUNDS = 3

_CANONICAL_SECTION = {s.lower(): s for s in SECTION_ORDER}


def _apply_extra(material: dict, extra) -> dict:
    """Writes the Fixer's optional timing `extra` onto the material's
    `extras` field -- the same shape `app/lib/timing_check.py`'s
    `apply_extras` produces, so a renderer built against either pipeline
    reads it the same way. Normalises the section key defensively, same
    reasoning as `timing_check.apply_extras`'s own fix earlier this
    session: a model does not reliably echo the exact case given in the
    schema."""
    if extra is None:
        return material
    out = json.loads(json.dumps(material))
    key = _CANONICAL_SECTION.get(str(extra.section).strip().lower(), extra.section)
    out.setdefault("extras", {})[key] = {"text": extra.text, "detail": extra.detail}
    return out


def _apply_revisions(material: dict, revisions: list) -> dict:
    """Writes the Fixer's revised sections onto the material using the SAME
    container map generation and validation already read
    (`prep_flow.sections._CONTAINERS`) -- so Concept's bare list, Explore's
    `points`, and Refresher's nested `recap` each land exactly where the
    rest of the pipeline expects them, with no parallel reimplementation of
    that mapping to drift out of sync with it."""
    out = json.loads(json.dumps(material))
    for rev in revisions:
        key, sub = _CONTAINERS[rev.section]
        points = [{"text": b.text, "detail": b.detail} for b in rev.points]
        if sub is None:
            out[key] = points
        else:
            out.setdefault(key, {})
            out[key][sub] = points
        if rev.section == "challenge" and rev.activity:
            out.setdefault("challenge", {})["activity"] = rev.activity
        if rev.section == "explore" and rev.image_focus:
            out.setdefault("explore", {})["imageFocus"] = rev.image_focus
    return out


def _findings_text(findings: list) -> str:
    if not findings:
        return "(none)"
    return "\n".join(
        f"[{f.severity}] ({f.check or '?'}) {f.section or '(whole sheet)'}: {f.message}"
        for f in findings
    )


async def run_checker_fixer_loop(
    material: dict, *, topic_ctx: TopicContext, grade: str, subject: str,
    chapter_title: str, chapter_markdown: str, chapter_figures: Optional[list],
) -> tuple[dict, list[dict]]:
    """Returns (final_material, trace). `trace` is the whole point of running
    this alongside the fixed pipeline: it is exactly what each agent decided
    to check, what it found, and what it changed, in order -- visible and
    diffable, not an opaque run that only looks different in its output."""
    trace: list[dict] = []
    current = material

    for round_number in range(1, SAFETY_CAP_ROUNDS + 1):
        topic_ctx.material = current
        checker, checker_record = registry.build_checker_agent(
            topic_ctx, grade=grade, subject=subject)
        verdict = await registry.invoke_structured(checker, (
            f"Review this sheet for '{topic_ctx.topic}'. Decide which checks it "
            "actually needs and whether it is ready to hand a teacher."))
        blocking = [f for f in verdict.findings if f.severity == "blocking"]
        trace.append({
            "round": round_number, "role": "checker",
            "passed": bool(verdict.passed and not blocking),
            "checksRun": verdict.checks_run,
            "findings": [f.model_dump() for f in verdict.findings],
            "reasoning": verdict.reasoning,
            "agentShape": checker_record.as_dict(),
        })
        if verdict.passed and not blocking:
            if verdict.findings:
                # Nothing blocking, but advisory findings exist (chiefly a
                # check_timing "extra" suggestion) -- these never reach the
                # Fixer through the normal branch below, since that branch
                # only runs when there IS a blocking finding. Give the Fixer
                # exactly one look at them here, then stop regardless of
                # what it does: an advisory-only pass is optional by
                # definition and does not need re-verification the way a
                # blocking fix does.
                topic_ctx.material = current
                fixer, fixer_record = registry.build_fixer_agent(
                    topic_ctx, grade=grade, subject=subject, chapter_title=chapter_title,
                    chapter_markdown=chapter_markdown, chapter_figures=chapter_figures,
                    findings_text=_findings_text(verdict.findings))
                fix = await registry.invoke_structured(fixer, (
                    f"'{topic_ctx.topic}' passed review. These advisory findings remain -- "
                    "act on any that are worth it (e.g. a timing extra), or make no "
                    "changes if none are."))
                trace.append({
                    "round": round_number, "role": "fixer-polish",
                    "revisedSections": [r.section for r in fix.revisions],
                    "extra": fix.extra.model_dump() if fix.extra else None,
                    "investigated": fix.investigated, "notes": fix.notes,
                    "agentShape": fixer_record.as_dict(),
                })
                current = _apply_revisions(current, fix.revisions)
                current = _apply_extra(current, fix.extra)
            break
        if round_number == SAFETY_CAP_ROUNDS:
            trace.append({"round": round_number, "role": "orchestrator",
                          "note": "safety cap reached -- kept the last revision as-is"})
            break

        fixer, fixer_record = registry.build_fixer_agent(
            topic_ctx, grade=grade, subject=subject, chapter_title=chapter_title,
            chapter_markdown=chapter_markdown, chapter_figures=chapter_figures,
            findings_text=_findings_text(blocking or verdict.findings))
        fix = await registry.invoke_structured(fixer, (
            f"Fix '{topic_ctx.topic}' using the checker's findings in your prompt."))
        trace.append({
            "round": round_number, "role": "fixer",
            "revisedSections": [r.section for r in fix.revisions],
            "extra": fix.extra.model_dump() if fix.extra else None,
            "investigated": fix.investigated, "notes": fix.notes,
            "agentShape": fixer_record.as_dict(),
        })
        if not fix.revisions and not fix.extra:
            trace.append({"round": round_number, "role": "orchestrator",
                          "note": "fixer made no changes -- stopping to avoid an empty-edit loop"})
            break
        current = _apply_revisions(current, fix.revisions)
        current = _apply_extra(current, fix.extra)

    return current, trace
