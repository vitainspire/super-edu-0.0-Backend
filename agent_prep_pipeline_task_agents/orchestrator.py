"""Writer -> Checker/Fixer. Two stages, not four -- see `__init__.py` and
`registry.py` for why timing/duplication were folded back into the reused
Checker/Fixer pair rather than kept as separate agents.

`run_checker_fixer_loop` is `agent_prep_pipeline.orchestrator`'s, reused
unchanged -- it already IS the critique/loop stage, including the
timing/duplication tools and the Fixer's `extra` field for a timing
suggestion. Duplicating any of it here would be the one thing this
package's whole design is built to avoid.
"""
from __future__ import annotations

from prep_flow.sections import SECTION_ORDER

from agent_prep_pipeline.orchestrator import run_checker_fixer_loop
from agent_prep_pipeline.tools_checks import TopicContext as CheckerTopicContext

from . import registry
from .schemas import WriterOutput
from .topic_spec import TopicSpec


def _assemble(out: WriterOutput, *, spec: TopicSpec) -> dict:
    material: dict = {
        "objective": out.objective,
        "planningNote": out.planning_note,
        "teachingOrder": list(SECTION_ORDER),
    }
    if not spec.is_first and out.refresher_points:
        material["previousTopicRefresher"] = {
            "previousTopic": spec.previous_topic,
            "recap": [b.model_dump() for b in out.refresher_points],
        }
    material["concept"] = [b.model_dump() for b in out.concept_points]
    material["realLife"] = {"points": [b.model_dump() for b in out.real_life_points]}
    material["challenge"] = {
        "activity": out.activity_name,
        "points": [b.model_dump() for b in out.challenge_points],
    }
    material["levelSet"] = {"points": [b.model_dump() for b in out.level_set_points]}
    material["explore"] = {
        "points": [b.model_dump() for b in out.explore_points],
        "imageFocus": out.image_focus,
    }
    material["materialsUsed"] = list(dict.fromkeys(out.materials_used))
    material["pagesCited"] = out.pages_cited
    material["figureRefs"] = []  # out of scope for this pilot -- see __init__.py
    material["sectionWatch"] = {w.section: {"text": w.text, "detail": w.detail} for w in out.watch}
    material["timings"] = dict(out.minutes)
    return material


async def write_and_polish(
    spec: TopicSpec, *, grade: str, subject: str, chapter_title: str,
    chapter_markdown: str, chapter_figures: list, node1_row: dict,
    contract_row: dict, is_last: bool,
) -> tuple[dict, list[dict]]:
    """Returns (material, trace). `node1_row`/`contract_row`/`is_last` exist
    only to build the reused Checker/Fixer stage's `TopicContext`."""
    trace: list[dict] = []

    writer_agent, writer_record = registry.build_writer_agent(
        spec, grade=grade, subject=subject, chapter_title=chapter_title,
        chapter_markdown=chapter_markdown, chapter_figures=chapter_figures)
    writer_out = await registry.invoke_structured(writer_agent, (
        f"Write the full six-section sheet for '{spec.topic}'."))
    trace.append({"agent": "writer", "output": writer_out.model_dump(),
                  "agentShape": writer_record.as_dict()})
    material = _assemble(writer_out, spec=spec)

    checker_ctx = CheckerTopicContext(
        material=material, spec=node1_row, contract_row=contract_row,
        chapter_text=chapter_markdown, grade=grade, subject=subject, topic=spec.topic,
        is_first=spec.is_first, is_last=is_last, plan=None, activity_name=None,
    )
    material, check_trace = await run_checker_fixer_loop(
        material, topic_ctx=checker_ctx, grade=grade, subject=subject,
        chapter_title=chapter_title, chapter_markdown=chapter_markdown,
        chapter_figures=chapter_figures,
    )
    trace.append({"agent": "checker_fixer_loop", "rounds": check_trace})

    return material, trace
