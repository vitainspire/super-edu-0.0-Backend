"""The full proposed flow, end to end: textbook -> critique the BOOK'S OWN
content against named pedagogical principles -> where a real gap is found,
inject it through the pipeline's existing per-topic gap/gapCloser channel
(the one `_topic_block` already renders as "WHERE THE BOOK STOPS SHORT") ->
generation -> validation/repair -> the same offline quality_judge scoring
used for every v4-v8 comparison, so the result is directly comparable.

STILL ADDITIVE. No edits to prep_flow/, generation/, or validation_flow/.
Node 1 runs exactly as the live pipeline runs it. The only new thing is a
mutation of THIS SCRIPT's own copy of the contract, built by re-running the
same orchestration prep_pipeline_bridge.generate_chapter_lessons already
does -- duplicated here (not imported and monkey-patched) specifically so
nothing about the live path is at risk, per the mastery_recovery.py /
quality_judge.py precedent this whole experiment follows.

WHAT THIS DELIBERATELY OMITS, for a fair first comparison at reasonable
scope: figure attachment and citation-string cleanup (cosmetic passes in
prep_pipeline_bridge.py, no effect on the section text quality_judge scores).
If this proves out, folding those back in is a small addition, not a redesign.

ONE GAP, NOT AN INVENTORY. Per experience-design's own principle: if a
topic's `experience.gap`/`gapCloser` is already filled (Node 1's own deep
agent chose one), this never overwrites it. It only fills topics the deep
agent left empty -- adding, never replacing.

Usage:
    venv/Scripts/python.exe scripts/generate_with_textbook_critique.py \
        --book ts_scert_class3_maths_en --chapter 1 --topics 3 \
        --out ../shapes-quality-loop-v9-critiqued
"""
import argparse
import asyncio
import json
import os
import sys
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import prep_flow.graph as prep_flow_graph
import generation.graph as generation_graph
import validation_flow.graph as validation_flow_graph
from app.lib import prep_pipeline_bridge as bridge
from app.lib import quality_judge as qj
from prep_flow.llm import call_json

from critique_textbook_content import DIMENSIONS, critique_topic
from run_shapes_quality_loop import render_prep_material_markdown, _rating_md, _write_summary
from app.lib import timing_check as tck
from app.lib import dedup_check as ddk

MAX_REPAIR_ROUNDS = bridge.MAX_REPAIR_ROUNDS

# Which section a dimension's supplement most naturally strengthens -- used
# only to label the injected gapKind for readability in the saved output.
_DIMENSION_LABEL = {d["key"]: d["key"] for d in DIMENSIONS}


def _inject_gaps(contract: dict, critiques: dict[int, dict]) -> list[str]:
    """Fills experience.gap/gapCloser ONLY where empty. Returns a log of
    what was injected, per topic, for the saved report."""
    log = []
    by_index = {row.get("index"): row for row in (contract.get("topics") or [])}
    for index, result in critiques.items():
        row = by_index.get(index)
        if not row:
            continue
        experience = row.setdefault("experience", {})
        if experience.get("gap") and experience.get("gapCloser"):
            log.append(f"T{index}: left untouched -- Node 1 already chose a gap")
            continue
        needing = [r for r in (result.get("ratings") or []) if r.get("verdict") == "supplement_needed"]
        if not needing:
            log.append(f"T{index}: no supplement needed -- page stands as-is")
            continue
        pick = needing[0]  # one gap, not an inventory -- first flagged, fixed dimension order
        gap = pick.get("reason") or _DIMENSION_LABEL[pick["dimension"]]
        closer = pick.get("supplement") or ""
        kind = f"textbook-critique:{pick['dimension']}"
        experience["gap"] = gap
        experience["gapCloser"] = closer
        experience["gapKind"] = kind
        # ALSO written here, not just into `experience`: contract.py builds a
        # SEPARATE `experiencePlan.closesGap` field, once, at contract-build
        # time -- before this injection ever runs. validation_flow's new
        # diagnostic gate reads THAT field, not this one. Writing only
        # `experience` (what generation reads) left validation checking a
        # stale, pre-injection value -- the exact shape of bug this whole
        # session kept finding elsewhere, caught here before it shipped.
        row.setdefault("experiencePlan", {})["closesGap"] = {
            "kind": kind, "gap": gap, "closer": closer,
        }
        log.append(f"T{index}: injected [{pick['dimension']}] -> {pick.get('supplement')}")
    return log


async def _polish_before_repair(lesson: dict, *, topic: str, grade: str, subject: str
                                ) -> tuple[dict, Optional[dict], list[str]]:
    """Runs BEFORE Node 3's validate/repair cycle. Two different mechanisms,
    NOT treated the same way, because a real bug showed they cannot be:

    - duplication: produces findings, applied through
      quality_judge.revise_lesson() right here, before Node 3 -- because a
      bad trim CAN break realism or learnability, and Node 3 needs to see
      and catch that, which only happens if this runs before it.

    - timing: computed here, but NOT applied yet -- only returned. A live
      run found `extras` being silently dropped every time a topic went
      through repair: generation's own repair merge (`_merge_sections` in
      compose.py) only preserves untouched top-level fields on a narrowly
      diagnosed repair, and returns the bare rewrite otherwise -- `extras`
      isn't a section repair knows to carry forward either way. Since
      `extras` can't itself break anything Node 3 checks, the fix is to
      apply it AFTER repair instead of before, so nothing downstream can
      wipe it out. See `_apply_extras_after_repair`.
    """
    log = []

    timing_result = await tck.check_timing(lesson, topic=topic, grade=grade, subject=subject)
    complexity = timing_result.get("topicComplexity")
    extras = [r["section"] for r in timing_result.get("sections") or [] if r.get("extra")]
    log.append(f"timing ({complexity}): extra WILL be added to {extras or '(none)'} after repair")

    dedup_result = await ddk.check_duplication(lesson, topic=topic, grade=grade, subject=subject)
    findings = ddk.to_revise_findings(dedup_result)
    if findings:
        lesson, rejected = await qj.revise_lesson(lesson, findings, topic=topic, grade=grade, subject=subject)
        log.append(f"duplication ({dedup_result.get('topicComplexity')}): trimmed "
                   f"{[f['section'] for f in findings]}, rejected {rejected}")
    else:
        log.append(f"duplication ({dedup_result.get('topicComplexity')}): none found")

    return lesson, (timing_result if extras else None), log


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--book", default="ts_scert_class3_maths_en")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--topics", type=int, default=3)
    p.add_argument("--out", default="../shapes-quality-loop-v9-critiqued")
    args = p.parse_args()

    out_root = os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    os.makedirs(out_root, exist_ok=True)

    print(f"Fetching {args.book} chapter {args.chapter}...")
    catalog_entry = bridge._fetch_catalog_entry(args.book)
    chapter = bridge._fetch_published_chapter(args.book, args.chapter)
    figures = bridge.locate_figures(chapter.get("content") or "", chapter.get("images"),
                                     book_id=args.book, chapter_number=args.chapter)

    print("Running Node 1 (unmodified)...")
    state = await prep_flow_graph.run_chapter(
        grade=catalog_entry["grade"], subject=catalog_entry["subject"],
        chapter_title=chapter["chapter_title"], chapter_markdown=chapter["content"],
        chapter_number=chapter.get("chapter_number", args.chapter),
        page_start=chapter.get("page_start"), page_end=chapter.get("page_end"),
        chapter_figures=figures,
        config={"deep_agents": True, "min_topics": args.topics,
                "target_topics": args.topics, "max_topics": args.topics},
    )
    contract = state.get("contract") or {}
    topics = state.get("topics") or []
    if state.get("status") == "failed" or not (contract.get("topics")):
        raise SystemExit(f"Node 1 produced no usable topics (status={state.get('status')!r})")
    print(f"Node 1 produced {len(topics)} topic(s). Running textbook critique...")

    critiques: dict[int, dict] = {}
    for spec in topics:
        result = await critique_topic(spec, grade=catalog_entry["grade"], subject=catalog_entry["subject"])
        critiques[spec["index"]] = result
        needing = [r["dimension"] for r in result.get("ratings") or [] if r.get("verdict") == "supplement_needed"]
        print(f"  [{spec.get('topic')}] critique: {needing or '(keep as-is)'}")

    injection_log = _inject_gaps(contract, critiques)
    print("Gap injection:")
    for line in injection_log:
        print(f"  {line}")

    print("Running generation...")
    generated = await generation_graph.run_generation(
        contract=contract, context_plan=None, chapter_text=chapter["content"],
    )
    materials = generated.get("materials") or {}
    if not materials:
        raise SystemExit(f"generation produced no lessons: {generated.get('errors')}")

    # DUPLICATION RUNS HERE, RIGHT AFTER GENERATION -- deliberately BEFORE
    # Node 3. A bad trim can break realism or learnability, and Node 3 needs
    # to see and catch that -- which only happens if this runs before it, so
    # Node 3 gets the truly final content and the last word.
    #
    # TIMING'S "extra" IS COMPUTED HERE BUT NOT APPLIED YET -- saved in
    # `pending_extras` and written on AFTER repair instead. A live run found
    # it silently dropped every time a topic went through repair (repair's
    # own merge only preserves untouched fields on a narrowly diagnosed
    # round, not a structural one) -- `extras` can't itself break anything
    # Node 3 checks, so applying it after repair is both safe and the only
    # placement that survives.
    by_index = {row.get("index"): row for row in (contract.get("topics") or [])}
    print(f"Generated {len(materials)} topic(s). Running duplication check "
          f"before Node 3, timing check deferred until after repair...")

    polish_log: list[str] = []
    pending_extras: dict[str, dict] = {}
    for index_str in list(materials.keys()):
        index = int(index_str) if isinstance(index_str, str) else index_str
        topic_row = by_index.get(index) or {}
        topic = topic_row.get("topic") or f"topic {index}"
        polished, timing_result, log = await _polish_before_repair(
            materials[index_str], topic=topic,
            grade=catalog_entry["grade"], subject=catalog_entry["subject"],
        )
        materials[index_str] = polished
        if timing_result is not None:
            pending_extras[index_str] = timing_result
        polish_log.append(f"T{index} ({topic}):")
        polish_log.extend(f"  {line}" for line in log)
        print(f"  [{topic}] " + " | ".join(log))

    print("Validating + repairing (same loop as the live bridge) -- last word on the sheet...")
    validated = await validation_flow_graph.run_validation(
        contract=contract, materials=materials, plan=None, chapter_text=chapter["content"],
    )
    for round_number in range(MAX_REPAIR_ROUNDS):
        targets = sorted(set(validated.get("repair_targets") or []))
        if not targets:
            break
        repaired = await generation_graph.run_repair(
            contract=contract, materials=materials,
            issues=validated.get("issues") or {}, repair_targets=targets,
            repair_sections=validated.get("repair_sections") or {},
            context_plan=None, chapter_text=chapter["content"], repair_round=round_number,
        )
        fixed = repaired.get("materials") or {}
        if not fixed:
            break
        materials = {**materials, **fixed}
        validated = await validation_flow_graph.run_validation(
            contract=contract, materials=materials, plan=None, chapter_text=chapter["content"],
        )

    # APPLIED NOW, AFTER REPAIR IS FULLY DONE -- see _polish_before_repair's
    # docstring for why this can't happen earlier. Nothing between here and
    # the quality-judge loop touches `materials` again, so this is the one
    # placement where it actually survives to the saved output.
    for index_str, timing_result in pending_extras.items():
        if index_str in materials:
            materials[index_str] = tck.apply_extras(materials[index_str], timing_result)
    if pending_extras:
        polish_log.append(f"extras applied post-repair to: {sorted(pending_extras)}")
        print(f"Applied extras post-repair to: {sorted(pending_extras)}")

    print("Running quality loop...")

    rows = []
    for index_str, lesson in materials.items():
        index = int(index_str) if isinstance(index_str, str) else index_str
        topic_row = by_index.get(index) or {}
        topic = topic_row.get("topic") or f"topic {index}"
        folder = os.path.join(out_root, "".join(
            c if c.isalnum() else "-" for c in topic).strip("-")[:60])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "critique.json"), "w", encoding="utf-8") as f:
            json.dump(critiques.get(index, {}), f, indent=2, ensure_ascii=False)

        def on_round(loop_n, current_lesson, report, summary, _folder=folder, _topic=topic):
            with open(os.path.join(_folder, f"material-loop{loop_n}.json"), "w", encoding="utf-8") as f:
                json.dump(current_lesson, f, indent=2, ensure_ascii=False)
            with open(os.path.join(_folder, f"material-loop{loop_n}.md"), "w", encoding="utf-8") as f:
                f.write(render_prep_material_markdown(current_lesson, _topic, "") + "\n")
            with open(os.path.join(_folder, f"rating-loop{loop_n}.md"), "w", encoding="utf-8") as f:
                f.write(_rating_md(_topic, _topic, loop_n, report, summary))
            print(f"  [{_topic}] loop {loop_n}: targetable {summary['targetable']:.2f}/5 "
                  f"({summary['atOrAbove4']}/{summary['targetableCount']} at 4+)")

        result = await qj.run_quality_loop(
            lesson, topic=topic, book_heading=topic,
            grade=catalog_entry["grade"], subject=catalog_entry["subject"],
            chapter_title=chapter["chapter_title"], on_round=on_round,
        )
        rows.append({"topic": topic, "history": result["history"]})

    _write_summary(out_root, chapter["chapter_title"], rows)
    with open(os.path.join(out_root, "gap_injection_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(injection_log))
    with open(os.path.join(out_root, "polish_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(polish_log))
    print(f"\nDone. Output in {out_root}")


if __name__ == "__main__":
    asyncio.run(main())
