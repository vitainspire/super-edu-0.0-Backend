"""Same fetch/Node1/critique/gap-injection front half every pipeline in this
repo reuses (imported, not copied). Per topic: Writer agent (writes all six
sections, replacing `generation/compose.py`) -> the SAME Checker/Fixer
critique/loop `agent_prep_pipeline` uses, reused unchanged. Timing and
duplication are NOT separate agents -- the Checker already has
`check_timing`/`check_duplication` as tools it may call, and the Fixer
already knows how to act on either (a section rewrite for duplication, the
`extra` field for timing).

NOTE: unlike `agent_prep_pipeline/run.py`, this pipeline does NOT call
`_polish_before_repair` -- there is no separate fixed pre-step here at all;
timing/duplication live entirely inside the Checker/Fixer loop now.

Topics are processed strictly in order (Refresher needs the previous
topic's own Explore output).

Usage:
    venv/Scripts/python.exe agent_prep_pipeline_task_agents/run.py \
        --book ts_scert_class3_maths_en --chapter 1 --topics 3 \
        --out ../shapes-task-agents-v1
"""
import argparse
import asyncio
import json
import os
import sys

_BACKEND_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _BACKEND_ROOT)
sys.path.insert(0, os.path.join(_BACKEND_ROOT, "scripts"))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(_BACKEND_ROOT, ".env"))

import prep_flow.graph as prep_flow_graph
from app.lib import prep_pipeline_bridge as bridge

from critique_textbook_content import critique_topic
from generate_with_textbook_critique import _inject_gaps
from run_shapes_quality_loop import render_prep_material_markdown

from agent_prep_pipeline_task_agents.orchestrator import write_and_polish
from agent_prep_pipeline_task_agents.topic_spec import TopicSpec


def _build_topic_spec(node1_row: dict, contract_row: dict, *, is_first: bool,
                      previous_topic: str, previous_explore_points: list,
                      activity_name: str) -> TopicSpec:
    academic = contract_row.get("academic") or {}
    experience = contract_row.get("experience") or {}
    grounding = contract_row.get("grounding") or {}
    gap = (contract_row.get("experiencePlan") or {}).get("closesGap") or {}
    return TopicSpec(
        index=node1_row.get("index"), topic=node1_row.get("topic") or "",
        subtopic=node1_row.get("subtopic") or "", excerpt=node1_row.get("excerpt") or "",
        is_first=is_first,
        mastery_target=academic.get("masteryTarget") or "",
        concepts=academic.get("concepts") or [], competencies=academic.get("competencies") or [],
        vocabulary=academic.get("vocabulary") or [],
        anchor=experience.get("anchor") or "", trajectory=experience.get("trajectory") or "",
        floor=experience.get("floor") or "",
        gap=gap.get("gap") or experience.get("gap") or "",
        gap_closer=gap.get("closer") or experience.get("gapCloser") or "",
        page_start=grounding.get("pageStart"), page_end=grounding.get("pageEnd"),
        activity_name=activity_name,
        previous_topic=previous_topic, previous_explore_points=previous_explore_points,
    )


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--book", default="ts_scert_class3_maths_en")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--topics", type=int, default=3)
    p.add_argument("--out", default="../shapes-task-agents-v1")
    args = p.parse_args()

    out_root = os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    os.makedirs(out_root, exist_ok=True)

    print(f"Fetching {args.book} chapter {args.chapter}...")
    catalog_entry = bridge._fetch_catalog_entry(args.book)
    chapter = bridge._fetch_published_chapter(args.book, args.chapter)
    figures = bridge.locate_figures(chapter.get("content") or "", chapter.get("images"),
                                     book_id=args.book, chapter_number=args.chapter)
    grade, subject = catalog_entry["grade"], catalog_entry["subject"]

    print("Running Node 1 (unmodified -- same call every other script makes)...")
    state = await prep_flow_graph.run_chapter(
        grade=grade, subject=subject,
        chapter_title=chapter["chapter_title"], chapter_markdown=chapter["content"],
        chapter_number=chapter.get("chapter_number", args.chapter),
        page_start=chapter.get("page_start"), page_end=chapter.get("page_end"),
        chapter_figures=figures,
        config={"deep_agents": True, "min_topics": args.topics,
                "target_topics": args.topics, "max_topics": args.topics},
    )
    contract = state.get("contract") or {}
    node1_topics = state.get("topics") or []
    if state.get("status") == "failed" or not (contract.get("topics")):
        raise SystemExit(f"Node 1 produced no usable topics (status={state.get('status')!r})")
    print(f"Node 1 produced {len(node1_topics)} topic(s). Running textbook critique...")

    critiques: dict[int, dict] = {}
    for spec in node1_topics:
        result = await critique_topic(spec, grade=grade, subject=subject)
        critiques[spec["index"]] = result
        needing = [r["dimension"] for r in result.get("ratings") or [] if r.get("verdict") == "supplement_needed"]
        print(f"  [{spec.get('topic')}] critique: {needing or '(keep as-is)'}")

    injection_log = _inject_gaps(contract, critiques)
    print("Gap injection:")
    for line in injection_log:
        print(f"  {line}")

    contract_by_index = {row.get("index"): row for row in (contract.get("topics") or [])}
    selections = state.get("selections") or {}
    node1_ordered = sorted(node1_topics, key=lambda t: t.get("index"))

    print("\nWriting each topic with the WRITER agent, then the reused CHECKER/FIXER "
          "critique/loop (timing + duplication are tools the Checker may call, not "
          "separate agents), in order...")
    materials: dict[str, dict] = {}
    task_traces: dict[str, list[dict]] = {}
    previous_topic_title, previous_explore_points = "", []
    for i, node1_row in enumerate(node1_ordered):
        index = node1_row.get("index")
        is_last = i == len(node1_ordered) - 1
        activity_name = ((selections.get(index) or {}).get("activity") or {}).get("name") or ""
        topic_spec = _build_topic_spec(
            node1_row, contract_by_index.get(index, {}), is_first=(i == 0),
            previous_topic=previous_topic_title, previous_explore_points=previous_explore_points,
            activity_name=activity_name)
        material, trace = await write_and_polish(
            topic_spec, grade=grade, subject=subject, chapter_title=chapter["chapter_title"],
            chapter_markdown=chapter["content"], chapter_figures=figures,
            node1_row=node1_row, contract_row=contract_by_index.get(index, {}), is_last=is_last)
        materials[str(index)] = material
        task_traces[str(index)] = trace
        previous_topic_title = node1_row.get("topic") or ""
        previous_explore_points = material.get("explore", {}).get("points") or []
        checker_rounds = next((t["rounds"] for t in trace if t["agent"] == "checker_fixer_loop"), [])
        checker_passed = any(r.get("role") == "checker" and r.get("passed") for r in checker_rounds)
        checks_run = {c for r in checker_rounds if r.get("role") == "checker" for c in r.get("checksRun") or []}
        print(f"  [{node1_row.get('topic')}] {len(checker_rounds)} round(s), "
              f"{'PASSED' if checker_passed else 'hit safety cap'}; "
              f"checks the Checker chose to run: {sorted(checks_run) or '(none)'}")

    node1_by_index = {t["index"]: t for t in node1_topics}

    for index_str, lesson in materials.items():
        index = int(index_str)
        topic = node1_by_index.get(index, {}).get("topic") or f"topic {index}"
        folder = os.path.join(out_root, "".join(
            c if c.isalnum() else "-" for c in topic).strip("-")[:60])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "critique.json"), "w", encoding="utf-8") as f:
            json.dump(critiques.get(index, {}), f, indent=2, ensure_ascii=False)
        full_trace = task_traces.get(index_str, [])
        with open(os.path.join(folder, "task_agent_trace.json"), "w", encoding="utf-8") as f:
            json.dump(full_trace, f, indent=2, ensure_ascii=False)
        checker_rounds = next((t["rounds"] for t in full_trace if t["agent"] == "checker_fixer_loop"), [])
        with open(os.path.join(folder, "agent_trace.json"), "w", encoding="utf-8") as f:
            json.dump(checker_rounds, f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "material-final.json"), "w", encoding="utf-8") as f:
            json.dump(lesson, f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "material-final.md"), "w", encoding="utf-8") as f:
            f.write(render_prep_material_markdown(lesson, topic, "") + "\n")

    with open(os.path.join(out_root, "gap_injection_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(injection_log))
    print(f"\nDone. Output in {out_root}")


if __name__ == "__main__":
    asyncio.run(main())
