"""Same chapter, same Creator, same textbook-critique/gap-injection front
half as `scripts/generate_with_textbook_critique.py` -- reused by importing
that script directly, not copy-pasted, so this can never silently drift from
what the fixed pipeline actually does upstream of Node 3. The ONLY thing
this replaces is the fixed Node 3 validate/repair block: here, each topic
goes through the autonomous Checker/Fixer loop in `orchestrator.py` instead.

Deliberately not wired into `main.py` or any live route -- run by hand, for
a side-by-side comparison against a `generate_with_textbook_critique.py` run
on the same chapter. Nothing in `prep_flow/`, `generation/`,
`validation_flow/`, or `app/lib/` is imported in a way that changes its
behaviour; see `__init__.py` for the fuller version of this note and the
stated v1 scope gaps.

DOES NOT RUN THE OFFLINE quality_judge SCORING LOOP. That loop is what
produces the comparable 1-5 scores `generate_with_textbook_critique.py`'s
`SUMMARY.md` shows -- deliberately left out here so this only costs tokens
on the actual new thing under test (the checker/fixer loop), not on a second
scoring pass. Output is `material-final.json`/`.md` per topic plus
`agent_trace.json` -- read those directly for now.

Usage:
    venv/Scripts/python.exe agent_prep_pipeline/run.py \
        --book ts_scert_class3_maths_en --chapter 1 --topics 3 \
        --out ../shapes-agent-pilot-v1
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
import generation.graph as generation_graph
from app.lib import prep_pipeline_bridge as bridge
from app.lib import timing_check as tck

from critique_textbook_content import critique_topic
from generate_with_textbook_critique import _inject_gaps, _polish_before_repair
from run_shapes_quality_loop import render_prep_material_markdown

from agent_prep_pipeline.orchestrator import run_checker_fixer_loop
from agent_prep_pipeline.tools_checks import TopicContext


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--book", default="ts_scert_class3_maths_en")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--topics", type=int, default=3)
    p.add_argument("--out", default="../shapes-agent-pilot-v1")
    args = p.parse_args()

    out_root = os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    os.makedirs(out_root, exist_ok=True)

    print(f"Fetching {args.book} chapter {args.chapter}...")
    catalog_entry = bridge._fetch_catalog_entry(args.book)
    chapter = bridge._fetch_published_chapter(args.book, args.chapter)
    figures = bridge.locate_figures(chapter.get("content") or "", chapter.get("images"),
                                     book_id=args.book, chapter_number=args.chapter)
    grade, subject = catalog_entry["grade"], catalog_entry["subject"]

    print("Running Node 1 (unmodified -- same call the fixed pipeline makes)...")
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

    print("Running generation (the Creator -- unchanged, still deterministic)...")
    generated = await generation_graph.run_generation(
        contract=contract, context_plan=None, chapter_text=chapter["content"],
    )
    materials = generated.get("materials") or {}
    if not materials:
        raise SystemExit(f"generation produced no lessons: {generated.get('errors')}")

    node1_by_index = {t["index"]: t for t in node1_topics}
    contract_by_index = {row.get("index"): row for row in (contract.get("topics") or [])}
    ordered = sorted(int(i) for i in materials.keys())

    print(f"Generated {len(materials)} topic(s). Running duplication check "
          f"before the agent loop, timing check deferred until after (same "
          f"reasoning as the fixed pipeline -- see _polish_before_repair)...")
    polish_log: list[str] = []
    pending_extras: dict[str, dict] = {}
    for index_str in list(materials.keys()):
        index = int(index_str)
        topic = node1_by_index.get(index, {}).get("topic") or f"topic {index}"
        polished, timing_result, log = await _polish_before_repair(
            materials[index_str], topic=topic, grade=grade, subject=subject)
        materials[index_str] = polished
        if timing_result is not None:
            pending_extras[index_str] = timing_result
        polish_log.append(f"T{index} ({topic}):")
        polish_log.extend(f"  {line}" for line in log)
        print(f"  [{topic}] " + " | ".join(log))

    print("\nRunning the AUTONOMOUS checker/fixer loop, per topic "
          "(this is what replaces Node 3 here)...")
    all_traces: dict[str, list[dict]] = {}
    for index_str in [str(i) for i in ordered]:
        index = int(index_str)
        spec = node1_by_index.get(index, {})
        contract_row = contract_by_index.get(index, {})
        topic = spec.get("topic") or f"topic {index}"
        is_first = index == ordered[0]
        is_last = index == ordered[-1]

        topic_ctx = TopicContext(
            material=materials[index_str], spec=spec, contract_row=contract_row,
            chapter_text=chapter["content"], grade=grade, subject=subject, topic=topic,
            is_first=is_first, is_last=is_last, plan=None, activity_name=None,
        )
        final_material, trace = await run_checker_fixer_loop(
            materials[index_str], topic_ctx=topic_ctx, grade=grade, subject=subject,
            chapter_title=chapter["chapter_title"], chapter_markdown=chapter["content"],
            chapter_figures=figures,
        )
        materials[index_str] = final_material
        all_traces[index_str] = trace
        rounds_used = trace[-1]["round"] if trace else 0
        final_passed = any(t.get("role") == "checker" and t.get("passed") for t in trace)
        print(f"  [{topic}] {rounds_used} round(s), "
              f"{'PASSED' if final_passed else 'hit the safety cap'}")

    for index_str, timing_result in pending_extras.items():
        if index_str in materials:
            materials[index_str] = tck.apply_extras(materials[index_str], timing_result)
    if pending_extras:
        polish_log.append(f"extras applied post-loop to: {sorted(pending_extras)}")

    for index_str, lesson in materials.items():
        index = int(index_str)
        topic = node1_by_index.get(index, {}).get("topic") or f"topic {index}"
        folder = os.path.join(out_root, "".join(
            c if c.isalnum() else "-" for c in topic).strip("-")[:60])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "critique.json"), "w", encoding="utf-8") as f:
            json.dump(critiques.get(index, {}), f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "agent_trace.json"), "w", encoding="utf-8") as f:
            json.dump(all_traces.get(index_str, []), f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "material-final.json"), "w", encoding="utf-8") as f:
            json.dump(lesson, f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "material-final.md"), "w", encoding="utf-8") as f:
            f.write(render_prep_material_markdown(lesson, topic, "") + "\n")

    with open(os.path.join(out_root, "gap_injection_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(injection_log))
    with open(os.path.join(out_root, "polish_log.txt"), "w", encoding="utf-8") as f:
        f.write("\n".join(polish_log))
    print(f"\nDone. Output in {out_root}")


if __name__ == "__main__":
    asyncio.run(main())
