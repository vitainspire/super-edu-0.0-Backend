"""Reproduces the shapes-quality-loop experiment: fetch a chapter from the
published textbook API, generate a handful of topics through the real
pipeline (prep_flow -> generation -> validation_flow), then run each topic
through the founder-facing quality_judge loop (judge -> fix -> re-judge,
MAX_FIX_ROUNDS rounds), saving every round's material and rating to disk.

Usage:
    venv/Scripts/python.exe scripts/run_shapes_quality_loop.py \
        --book ts_scert_class3_maths_en --chapter 1 --topics 3 \
        --out ../shapes-quality-loop-v4

No driver for this existed in the repo (shapes-quality-loop-v3's output was
found with no script behind it) -- this is that script, written so the next
run doesn't repeat the same archaeology.
"""
import argparse
import asyncio
import json
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Must run before importing app.lib.ai (and anything that imports it) -- its
# API_KEY/MODEL constants are read from os.environ at import time, not lazily,
# so a script invoked outside the FastAPI app (which calls this in main.py)
# needs this itself or every call fails with an empty bearer token.
from dotenv import load_dotenv
load_dotenv()

from app.lib import prep_pipeline_bridge as bridge
from app.lib import quality_judge as qj
from app.lib.prep_material_generator import _as_section_dict, _render_bullets


def render_prep_material_markdown(material: dict, topic: str = "", subtopic: str = "") -> str:
    """Local copy of prep_material_generator.render_prep_material_markdown with
    two changes: (1) Real Life is actually rendered -- the shared function never
    prints it at all, despite generation always producing it (verified: present
    in every material-loop*.json this pipeline writes); this copy inherited that
    gap silently until it was caught by comparing against the six-section order.
    (2) Explore prints last (after Level Set) instead of before Challenge, per
    the earlier ask. All six sections now print in the canonical order: Refresher
    -> Concept -> Real Life -> Challenge -> Level Set -> Explore. Kept here rather
    than edited in the shared module because that function is also what the live
    pilot route and generation's own repair path render against -- this is a
    read-order/completeness fix for these review files only, not a schema change."""
    out = []

    title = material.get("objective") or topic or "Prep Material"
    out.append(f"# {title}")
    if topic or subtopic:
        out.append(f"_{topic}{' — ' + subtopic if subtopic else ''}_")
    if material.get("planningNote"):
        out.append("")
        out.append(f"> {material['planningNote']}")

    refresher_raw = material.get("previousTopicRefresher")
    if refresher_raw:
        refresher = {"recap": refresher_raw} if isinstance(refresher_raw, list) else refresher_raw
        out.append("")
        out.append(f"## Refresher — {refresher.get('previousTopic', '')}")
        out.append(_render_bullets(refresher.get("recap")))

    out.append("")
    out.append("## Concept")
    out.append(_render_bullets(material.get("concept")))

    real_life = _as_section_dict(material.get("realLife"))
    out.append("")
    out.append("## Real Life")
    out.append(_render_bullets(real_life.get("points")))

    challenge = _as_section_dict(material.get("challenge"))
    out.append("")
    out.append(f"## Challenge — {challenge.get('activity', '(unspecified)')}")
    out.append(_render_bullets(challenge.get("points")))

    level_set = _as_section_dict(material.get("levelSet"))
    out.append("")
    out.append("## Level Set")
    out.append(_render_bullets(level_set.get("points")))

    explore = _as_section_dict(material.get("explore"))
    out.append("")
    out.append("## Explore")
    if explore.get("imageFocus"):
        out.append(f"*Board sketch: {explore['imageFocus']}*")
        out.append("")
    out.append(_render_bullets(explore.get("points")))

    watch = material.get("sectionWatch") or {}
    watch_lines = []
    for section in ("refresher", "concept", "explore", "challenge", "levelSet"):
        w = watch.get(section)
        if w:
            label = "Level Set" if section == "levelSet" else section.capitalize()
            text = (w.get("text") or "").strip()
            detail = (w.get("detail") or "").strip()
            watch_lines.append(f"- **{label}:** {text}" + (f" — {detail}" if detail else ""))
    if watch_lines:
        out.append("")
        out.append("## Watch For")
        out.extend(watch_lines)

    materials = material.get("materialsUsed") or []
    out.append("")
    out.append("## Materials")
    out.append("\n".join(f"- {m}" for m in materials) if materials else "_(none)_")

    timings = material.get("timings") or {}
    if timings:
        section_labels = {"levelSet": "Level Set"}
        parts = [
            f"{section_labels.get(k, k.capitalize())}: {v} min"
            for k, v in timings.items() if v is not None
        ]
        total = sum(v for v in timings.values() if isinstance(v, (int, float)))
        out.append("")
        out.append("## Timings")
        out.append(", ".join(parts) + f" (Total: {total} min)")

    return "\n".join(out)


def _slug(topic: str) -> str:
    s = topic.strip().rstrip(".")
    s = re.sub(r"_{2,}", "_", s)
    s = re.sub(r"[^A-Za-z0-9]+", "-", s)
    return re.sub(r"-{2,}", "-", s).strip("-")[:60]


def _rating_md(topic: str, book_heading: str, loop_n: int, report: dict, summary: dict) -> str:
    labels = {d["key"]: d["label"] for d in qj.DIMENSIONS}
    blocked = summary.get("blocked") or {}
    lines = [
        f"# Rating — {topic} — Loop {loop_n}",
        "",
        f"*Book heading: {book_heading}*  |  Judge model: `{qj.JUDGE_MODEL}`",
        "",
        f"**Targetable average: {summary['targetable']:.2f}/5** "
        f"({summary['atOrAbove4']}/{summary['targetableCount']} at 4+) — what the fix loop can move",
        "",
        f"*Overall average including blocked dimensions: {summary['overall']:.2f}/5*",
        "",
    ]
    if blocked:
        lines.append("Excluded from the targetable score (cannot be moved by rewriting):")
        lines.append("")
        for key, why in blocked.items():
            lines.append(f"- **{key}** — {why}")
        lines.append("")
    lines.append("| Dimension | Score | Revisable? | Reason |")
    lines.append("|---|---|---|---|")
    for r in report.get("ratings") or []:
        if not isinstance(r, dict):
            continue
        label = labels.get(r.get("dimension"), r.get("dimension"))
        revisable = "yes" if r.get("revisable") else "no (format ceiling)"
        reason = str(r.get("reason") or "").replace("\n", " ").strip()
        lines.append(f"| {label} | {r.get('score')}/5 | {revisable} | {reason} |")
    return "\n".join(lines) + "\n"


async def _run_topic(entry: dict, out_root: str, *, grade: str, subject: str, chapter_title: str) -> dict:
    topic = entry["topic"]
    lesson = entry["lesson"]
    book_heading = entry.get("bookHeading") or topic
    folder = os.path.join(out_root, _slug(topic))
    os.makedirs(folder, exist_ok=True)

    def on_round(loop_n: int, current_lesson: dict, report: dict, summary: dict) -> None:
        with open(os.path.join(folder, f"material-loop{loop_n}.json"), "w", encoding="utf-8") as f:
            json.dump(current_lesson, f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, f"material-loop{loop_n}.md"), "w", encoding="utf-8") as f:
            f.write(render_prep_material_markdown(current_lesson, topic, "") + "\n")
        with open(os.path.join(folder, f"rating-loop{loop_n}.md"), "w", encoding="utf-8") as f:
            f.write(_rating_md(topic, book_heading, loop_n, report, summary))
        print(f"  [{topic}] loop {loop_n}: targetable {summary['targetable']:.2f}/5 "
              f"({summary['atOrAbove4']}/{summary['targetableCount']} at 4+)")

    result = await qj.run_quality_loop(
        lesson, topic=topic, book_heading=book_heading,
        grade=grade, subject=subject, chapter_title=chapter_title,
        on_round=on_round,
    )
    return {"topic": topic, "history": result["history"]}


def _write_summary(out_root: str, chapter_title: str, rows: list[dict]) -> None:
    lines = [
        f"# Shapes chapter (re-run) — quality loop summary",
        "",
        f"Source: {chapter_title}, fetched live from {bridge.PUBLISHED_TEXTBOOK_API}",
        "",
        f"Judge model: `{qj.JUDGE_MODEL}` (independent of the generation model) | "
        f"Max fix rounds: {qj.MAX_FIX_ROUNDS} | "
        "**\"final\" = best-scoring round kept, not necessarily the last one**",
        "",
        "| Topic | Targetable: loop 1 → best (loop) | At 4+: loop 1 → best | Overall: loop 1 → best |",
        "|---|---|---|---|",
    ]
    for row in rows:
        h = row["history"]
        first, best = h[0], max(h, key=lambda x: x["targetable"])
        lines.append(
            f"| {row['topic']} | {first['targetable']:.2f} → **{best['targetable']:.2f} (loop {best['loop']})** "
            f"| {first['atOrAbove4']}/{first['targetableCount']} → **{best['atOrAbove4']}/{best['targetableCount']}** "
            f"| {first['overall']:.2f} → {best['overall']:.2f} |"
        )
    with open(os.path.join(out_root, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    with open(os.path.join(out_root, "summary_data.json"), "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, ensure_ascii=False)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--book", default="ts_scert_class3_maths_en")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--topics", type=int, default=3)
    p.add_argument("--out", default="../shapes-quality-loop-v4")
    args = p.parse_args()

    out_root = os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    os.makedirs(out_root, exist_ok=True)

    print(f"Fetching {args.book} chapter {args.chapter}, pinned to {args.topics} topic(s)...")
    result = await bridge.generate_lessons_from_published_book(
        args.book, args.chapter, topic_count=args.topics, generate_ai_images=True,
    )
    lessons = result.get("lessons") or []
    print(f"Generated {len(lessons)} topic(s) for '{result.get('chapterTitle')}'. Running quality loop...")

    rows = []
    for entry in lessons:
        row = await _run_topic(
            entry, out_root,
            grade=result["grade"], subject=result["subject"],
            chapter_title=result["chapterTitle"],
        )
        rows.append(row)

    _write_summary(out_root, result["chapterTitle"], rows)
    print(f"\nDone. Output in {out_root}")


if __name__ == "__main__":
    asyncio.run(main())
