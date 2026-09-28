"""Standalone, additive experiment: judges the TEXTBOOK'S OWN content against
named pedagogical principles, before any lesson sheet gets written -- not the
generated material (quality_judge.py already does that), the book itself.

WHY THIS EXISTS. Node 1 (prep_flow) is deliberately a faithful, non-judgmental
mirror of the book -- see its own docstrings. That is correct for what Node 1
does. But nothing in the live pipeline ever asks "is the book's OWN approach
to teaching this idea actually sound", and the founder asked for exactly that:
preserve what is good, unchanged; where there is a real pedagogical gap, name
it and propose a concrete supplement -- never rewrite the book's own facts,
definitions or numbers.

ADDITIVE ONLY, PER THE MASTERY_RECOVERY.PY / QUALITY_JUDGE.PY PRECEDENT. Does
not edit prep_flow/, generation/ or validation_flow/. Reuses Node 1
(prep_flow_graph.run_chapter) exactly as the live pipeline does, read-only,
to get real topic boundaries and excerpts -- then adds one new judged step
nothing else depends on. Meant to be run over a real chapter, read by hand,
and only wired into the live bridge later if it actually earns its keep.

DIMENSIONS, AND WHY THESE FOUR. Chosen because each ties to a specific,
already-written skill file (deep_agents/skills/*.md) rather than invented
vibes -- the same lesson this whole session kept relearning: a vague "is this
good" produces nothing checkable. Four, not more, on purpose -- quality_judge.py
was trimmed from 18 dimensions to 10 "after two real runs on real material";
starting smaller here and expanding only if a real run shows a gap.

Usage:
    venv/Scripts/python.exe scripts/critique_textbook_content.py \
        --book ts_scert_class3_maths_en --chapter 1 --topics 3 \
        --out ../textbook-critique-v1
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"))

import prep_flow.graph as prep_flow_graph
from app.lib import prep_pipeline_bridge as bridge
from prep_flow.llm import call_json

DIMENSIONS = [
    {"key": "misconceptionNamed", "skill": "misconception-analysis",
     "what": "Does the page give a teacher a way to anticipate the WRONG idea a "
             "child will likely hold, or does it only state the correct fact and "
             "leave the teacher to discover the misconception live in the room? "
             "keep: the page itself says 'children often think the taller glass "
             "holds more -- pour side by side to show it doesn't.' "
             "supplement_needed: the page states 'a square has 4 equal sides' and "
             "nothing else -- a child who thinks a rectangle also qualifies is "
             "never addressed anywhere on the page."},
    {"key": "belowLevelEntry", "skill": "differentiation",
     "what": "Is there a smaller, one-variable version of the idea a child behind "
             "grade level could enter through, reachable from the page's own "
             "content -- or does the page open directly at the full, main case? "
             "keep: the page shows 2 objects compared before asking about 5. "
             "supplement_needed: the page opens directly on 'sort these 12 shapes "
             "by number of sides' with no smaller case first."},
    {"key": "concreteAnchor", "skill": "core-pedagogy / experience-design",
     "what": "Is the idea grounded in something a rural Indian child could "
             "actually hold or point to, or is it introduced as an abstract "
             "statement with no concrete object behind it? "
             "keep: 'a square has 4 equal sides, like a chapati cut straight on "
             "all sides.' supplement_needed: 'a square is a quadrilateral with "
             "four congruent sides and four right angles' with no named object."},
    {"key": "sequencingSoundness", "skill": "curriculum-reasoning / experience-design",
     "what": "Does the page build the idea in an order where each step is "
             "reachable from the one before it, or does it jump to something "
             "that assumes groundwork the page never laid? "
             "keep: names shapes, then compares two, then asks for a group sort. "
             "supplement_needed: jumps from naming a shape straight to composite "
             "figures with no step teaching how to isolate one shape inside another."},
]

_CRITIQUE_PROMPT = """You are an instructional-design reviewer with real pedagogical training --
not grading a lesson, judging whether the TEXTBOOK PAGE ITSELF, as written,
teaches this idea well. This is real, government-approved curriculum content,
written by curriculum scholars. Your default is that it is good. You are
looking for a small number of genuine, specific gaps, not reasons to rewrite it.

TOPIC: {topic}
GRADE {grade} {subject}

THE PAGE'S OWN CONTENT (verbatim, this and nothing else is what you judge):
{excerpt}

Rate it against EXACTLY these {n} dimensions:
{dimension_list}

RULES:
- Default verdict is "keep" -- a real curriculum scholar wrote this page, and a
  page that does its job needs no supplement. Only mark "supplement_needed"
  for a genuine, specific gap you can name concretely.
- WHEN IN DOUBT, KEEP. A dimension being thin or minimal is not the same as it
  being missing -- if the page does the job in one line, that is a design
  choice, not a gap. Only mark "supplement_needed" if you could point to the
  exact sentence or paragraph where the thing is absent, the way the examples
  above do -- "the page never..." not "the page could do more to...".
- NEVER propose changing, contradicting, or "correcting" a fact, definition,
  number or term the page states. You may ONLY propose an ADDITION -- a
  misconception to name, an easier entry case, a concrete anchor, a
  resequencing note -- that sits alongside the page's own content, never
  replacing it.
- "supplement" MUST BE A STAGED MOVE, NOT ADVICE. The downstream writer copies
  this almost directly into a lesson bullet -- it should not have to invent
  the classroom action itself. Describe WHAT HAPPENS: the object, the
  comparison, the question -- the same voice every section in this pipeline
  already writes in (a described move, never dialogue in quotes -- "ask which
  holds more, then pour one into the other to show the guess was wrong", not
  "say: 'which holds more?'"). "Point out that a taller glass doesn't always "
  "hold more" is advice -- it tells the writer WHAT to achieve, not what to "
  "DO. "Show a tall thin glass next to a short wide bowl, ask which holds "
  "more, then pour one into the other to reveal the guess was wrong" is a "
  "staged move -- a real classroom action ready to be staged, in the same "
  "guidance-not-script voice as the rest of the sheet.
- "reason": one sentence, specific to what the page actually does or omits,
  quoting or closely paraphrasing the page where useful.
- DO NOT FLAG THE SAME DIMENSION ON EVERY TOPIC OUT OF HABIT. If two topics in
  a row get "supplement_needed" on the same dimension for a similar-sounding
  reason, stop and check the page again -- a critic that finds the same gap
  everywhere is pattern-matching, not reading.

Return ONLY valid JSON:
{{"ratings": [{{"dimension": "<key>", "verdict": "keep" or "supplement_needed",
  "reason": "...", "supplement": "..." or null}}, ...]}}
One entry per dimension, in the order given, every key present."""


async def critique_topic(topic_spec: dict, *, grade: str, subject: str) -> dict:
    dimension_list = "\n".join(f"- {d['key']} ({d['skill']}): {d['what']}" for d in DIMENSIONS)
    excerpt = (topic_spec.get("excerpt") or "").strip()
    if not excerpt:
        return {"topic": topic_spec.get("topic"), "ratings": [], "skipped": "no excerpt"}

    prompt = _CRITIQUE_PROMPT.format(
        topic=topic_spec.get("topic") or "", grade=grade, subject=subject,
        excerpt=excerpt[:6000], n=len(DIMENSIONS), dimension_list=dimension_list,
    )
    data = await call_json(prompt, label="textbook-critique", required=("ratings",),
                            temperature=0.2, max_tokens=1500)
    return {"topic": topic_spec.get("topic"), "index": topic_spec.get("index"),
            "ratings": data.get("ratings") or []}


def _render_md(result: dict) -> str:
    lines = [f"# Textbook critique — {result['topic']}", ""]
    for r in result.get("ratings") or []:
        verdict = r.get("verdict") or "?"
        mark = "KEEP" if verdict == "keep" else "SUPPLEMENT NEEDED"
        lines.append(f"## {r.get('dimension')} — {mark}")
        lines.append(f"- reason: {r.get('reason')}")
        if r.get("supplement"):
            lines.append(f"- supplement: {r['supplement']}")
        lines.append("")
    return "\n".join(lines)


async def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--book", default="ts_scert_class3_maths_en")
    p.add_argument("--chapter", type=int, default=1)
    p.add_argument("--topics", type=int, default=3)
    p.add_argument("--out", default="../textbook-critique-v1")
    args = p.parse_args()

    out_root = os.path.abspath(os.path.join(os.path.dirname(__file__), args.out))
    os.makedirs(out_root, exist_ok=True)

    print(f"Fetching {args.book} chapter {args.chapter} and running Node 1 "
          f"(read-only, unmodified) to get real topic boundaries...")
    catalog_entry = bridge._fetch_catalog_entry(args.book)
    chapter = bridge._fetch_published_chapter(args.book, args.chapter)
    figures = bridge.locate_figures(chapter.get("content") or "", chapter.get("images"),
                                     book_id=args.book, chapter_number=args.chapter)

    state = await prep_flow_graph.run_chapter(
        grade=catalog_entry["grade"], subject=catalog_entry["subject"],
        chapter_title=chapter["chapter_title"], chapter_markdown=chapter["content"],
        chapter_number=chapter.get("chapter_number", args.chapter),
        page_start=chapter.get("page_start"), page_end=chapter.get("page_end"),
        chapter_figures=figures,
        config={"deep_agents": True, "min_topics": args.topics,
                "target_topics": args.topics, "max_topics": args.topics},
    )
    topics = state.get("topics") or []
    print(f"Node 1 produced {len(topics)} topic(s). Running textbook critique...")

    for spec in topics:
        result = await critique_topic(spec, grade=catalog_entry["grade"], subject=catalog_entry["subject"])
        folder = os.path.join(out_root, "".join(
            c if c.isalnum() else "-" for c in (spec.get("topic") or "topic")).strip("-")[:60])
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, "critique.json"), "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        with open(os.path.join(folder, "critique.md"), "w", encoding="utf-8") as f:
            f.write(_render_md(result))
        needing = [r["dimension"] for r in result.get("ratings") or [] if r.get("verdict") == "supplement_needed"]
        print(f"  [{spec.get('topic')}] needs supplement on: {needing or '(none — page stands as-is)'}")

    print(f"\nDone. Output in {out_root}")


if __name__ == "__main__":
    asyncio.run(main())
