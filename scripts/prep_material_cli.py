"""CLI for iterating on the Stage 6 prep-material pipeline end to end, against
the REAL canonical library and Pedagogy Library — no PDF, no admin UI. Give it
a topic/subtopic/content, and it walks through every stage, printing what each
one produced, so you can see exactly where a prompt needs adjusting or why a
lookup came back empty.

    Curriculum extraction (Stage 1, text)
        -> Canonical resolution (Phase B's resolve_canonical, real DB)
        -> Pedagogy lookup (Phase C's get_recommended_activities, real DB)
        -> Activity + context selection (Stage 4/5)
        -> Prompt assembly (Stage 6, printed before it's sent)
        -> LLM generation (Stage 6, unless --skip-generation)

Content can come from --content, --content-file, or stdin (paste, then
Ctrl-D on Unix / Ctrl-Z Enter on Windows to end).

Examples:

    python -m scripts.prep_material_cli \\
        --topic "Counting to Ten" --subtopic "Counting using real objects" \\
        --grade 1 --subject Maths --content-file lesson.txt

    python -m scripts.prep_material_cli \\
        --topic "Counting to Ten" --grade 1 --subject Maths \\
        --content-file lesson.txt --skip-generation   # stop after the lookup

    python -m scripts.prep_material_cli \\
        --topic "Shapes Around Us" --grade 1 --subject Maths \\
        --content-file lesson.txt --context Market --duration 40 --resource-level 1
"""

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Windows' default console codepage (cp1252) can't encode the box-drawing
# characters used in this script's output — force UTF-8 so `python -m
# scripts.prep_material_cli` doesn't crash on Windows terminals.
if sys.platform == "win32":
    sys.stdout.reconfigure(encoding="utf-8")

from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
load_dotenv(Path(__file__).resolve().parent.parent / ".env")

from app.lib.supabase_clients import create_admin_client  # noqa: E402
from app.lib.canonical_mapping import resolve_canonical  # noqa: E402
from app.lib.pedagogy_library import get_recommended_activities  # noqa: E402
from app.lib.prep_material_generator import (  # noqa: E402
    extract_knowledge_from_text, select_activity_and_context, grade_band_for,
    build_prep_material_prompt, generate_prep_material, render_prep_material_markdown,
)

_RULE = "─" * 78


def _header(n: int, title: str) -> None:
    print(f"\n{_RULE}\nSTAGE {n} — {title}\n{_RULE}")


def _read_content(args) -> str:
    if args.content:
        return args.content
    if args.content_file:
        return Path(args.content_file).read_text(encoding="utf-8")
    print("Paste the topic's content, then press Ctrl-D (Unix) or Ctrl-Z + Enter (Windows):")
    return sys.stdin.read()


async def run(args) -> None:
    content = _read_content(args)
    if not content.strip():
        print("No content given — nothing to extract from.")
        return

    ac = create_admin_client()
    grade_band = grade_band_for(args.grade)

    _header(1, "Curriculum extraction (text)")
    knowledge = await extract_knowledge_from_text(
        args.topic, args.subtopic, content, args.grade, args.subject,
    )
    print(json.dumps(knowledge, indent=2, ensure_ascii=False))

    _header(2, "Canonical resolution (real library — grows if these are new)")
    concept_map = resolve_canonical(ac, "concepts", knowledge["concepts"])
    competency_map = resolve_canonical(ac, "competencies", knowledge["competencies"])
    vocabulary_map = resolve_canonical(ac, "vocabulary", knowledge["vocabulary"])
    context_map = resolve_canonical(ac, "contexts", knowledge["contexts"])
    print("concepts     ->", concept_map)
    print("competencies ->", competency_map)
    print("vocabulary   ->", vocabulary_map)
    print("contexts     ->", context_map)

    _header(3, "Pedagogy lookup (Phase C)")
    competency_ids = list(competency_map.values())
    if not competency_ids:
        print("No competencies resolved — nothing to look up. "
              "(Did Stage 1 extract any? Check the JSON above.)")
        activities = []
    else:
        print(f"grade_band={grade_band or '(none — no filter, grade ' + str(args.grade) + ' has no pilot band)'} "
              f"resource_level={args.resource_level}")
        activities = get_recommended_activities(
            ac, competency_ids, resource_level=args.resource_level, grade_band=grade_band,
        )
        if not activities:
            print("No activity templates matched these competencies. Either the Pedagogy "
                  "Library doesn't cover this competency yet, or resource_level/grade_band "
                  "filtered everything out — try --resource-level 2 or check "
                  "GET /api/admin/pedagogy/activity-templates for what exists.")
        for a in activities:
            print(f"  - [{a['category']}] {a['name']} "
                  f"(matches {len(a['matchedCompetencyIds'])} competenc{'y' if len(a['matchedCompetencyIds'])==1 else 'ies'}, "
                  f"{len(a['contexts'])} context option(s))")

    _header(4, "Activity + context selection")
    activity, context = select_activity_and_context(activities, preferred_context=args.context)
    if activity:
        print(f"Selected activity: {activity['name']} ({activity['category']})")
        print(f"Selected context:  {context['name'] if context else '(none available)'}")
    else:
        print("Nothing selected — no activity matched (Stage 6 will generate a generic one).")

    teacher_settings = {
        "duration": args.duration,
        "classSize": args.class_size,
        "resourceLevel": args.resource_level,
        "language": args.language,
        "learningObjective": args.objective,
        "teachingStyle": args.teaching_style,
    }

    _header(5, "Prompt assembly (Stage 6 — this is exactly what gets sent to the LLM)")
    prompt = build_prep_material_prompt(
        args.topic, args.subtopic, args.grade, args.subject,
        knowledge, activity, context, teacher_settings, args.previous_topic,
    )
    print(prompt)

    if args.skip_generation:
        print("\n(--skip-generation given — stopping before the LLM call.)")
        return

    _header(6, "Generation")
    material = await generate_prep_material(
        args.topic, args.subtopic, args.grade, args.subject,
        knowledge, activity, context, teacher_settings, args.previous_topic,
    )
    if args.json:
        print(json.dumps(material, indent=2, ensure_ascii=False))
    else:
        print(render_prep_material_markdown(material, args.topic, args.subtopic))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--topic", required=True)
    parser.add_argument("--subtopic", default="")
    parser.add_argument("--grade", required=True)
    parser.add_argument("--subject", required=True)
    parser.add_argument("--content", default=None)
    parser.add_argument("--content-file", default=None)

    parser.add_argument("--duration", type=int, default=30, help="minutes (default 30)")
    parser.add_argument("--class-size", type=int, default=40)
    parser.add_argument("--resource-level", type=int, default=0, choices=[0, 1, 2])
    parser.add_argument("--language", default="English")
    parser.add_argument("--objective", default="new lesson", help='e.g. "new lesson", "revision", "remedial"')
    parser.add_argument("--teaching-style", default="interactive")
    parser.add_argument("--context", default=None, help="preferred context name/category, e.g. 'School'")
    parser.add_argument("--previous-topic", default=None, help="prior topic name, to test the refresher path")

    parser.add_argument("--skip-generation", action="store_true", help="stop after Stage 5, before spending an LLM call")
    parser.add_argument("--json", action="store_true", help="print Stage 6's raw JSON instead of the rendered Markdown prep sheet")

    args = parser.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
