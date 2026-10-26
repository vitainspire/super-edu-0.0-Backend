"""An independent judge rates a freshly generated live prep sheet
(smart_lesson_routes.py's six-section schema) against a fixed rubric, and
low-scoring, genuinely fixable findings drive one bounded rewrite pass.

DELIBERATELY STANDALONE. app/lib/quality_judge.py already does almost this
exact thing, but for the SEPARATE, offline agentic pipeline's lesson shape
(prep_flow -> generation -> validation_flow) — it imports prep_flow.llm and
prep_flow.sections at module level, and its own docstring says plainly it is
"NOT the production validation gate". Importing it here would make the live,
per-teacher generation path depend on that experimental pipeline's package
even existing/importing cleanly, for a system this one has no real need of.
This module copies the rubric (plain data, not pipeline-specific code) and
writes its own sheet-renderer/reviser against THIS schema's actual keys
(concept is a bare list; explore/challenge/levelSet are {points, ...};
previousTopicRefresher is {previousTopic, recap} or null) instead.

WHY A SEPARATE MODEL FROM THE GENERATOR: a model checking its own work tends
to rate its own choices kindly. JUDGE_MODEL is a different family from the
generator on purpose.

NEVER BLOCKS A LESSON. Every entry point here is wrapped by its caller in a
try/except — a judge or revise call failing (bad JSON, model error, timeout)
must fall back to the Tier-2-validated lesson a teacher already has, not
turn into "couldn't generate your prep sheet".
"""
import json
import os
import re
from typing import Optional

from .ai import call_ai
from .lesson_images import has_image as _bullet_has_image

JUDGE_MODEL = os.environ.get("PREP_QUALITY_JUDGE_MODEL", "anthropic/claude-haiku-4.5")
# One fix round by default — this runs inside a live request a teacher is
# waiting on, not an offline batch job. Raise via env if latency allows more.
MAX_FIX_ROUNDS = int(os.environ.get("PREP_QUALITY_MAX_FIX_ROUNDS", "1"))

SECTION_KEYS = ("refresher", "concept", "explore", "challenge", "levelSet")
SECTION_LABELS = {
    "refresher": "Refresher", "concept": "Concept", "explore": "Explore",
    "challenge": "Challenge", "levelSet": "Level Set",
}

# Same 10 dimensions app/lib/quality_judge.py rates — plain pedagogical
# criteria, not tied to any one pipeline's schema. See that module's own
# docstring for why these 10 survived a cut from an original 18.
DIMENSIONS: list[dict] = [
    {"key": "diagnosticThinking", "label": "Diagnostic thinking",
     "what": "Names a misconception AND maps it to a specific move -- not just 'watch for X', but 'if you see X, do Y'."},
    {"key": "belowLevelSupport", "label": "Below-level support",
     "what": "A child behind grade level has a concrete, simpler path in -- not just concrete objects, an explicit easier example or step before the main one."},
    {"key": "realWorldLearning", "label": "Real-world learning",
     "what": "Local examples ask the child to solve or decide something, not just observe or imagine."},
    {"key": "exploration", "label": "Exploration",
     "what": "Explore gives a real reason to look outside class, self-checkable, costing nothing."},
    {"key": "creativity", "label": "Creativity",
     "what": "Room for more than one right answer or more than one way to solve it."},
    {"key": "teacherUsability", "label": "Teacher usability",
     "what": "Timing, materials, what to say, what to expect back, and what to do if it goes sideways are all actually on the page."},
    {"key": "continuity", "label": "Continuity across lessons",
     "what": "Each topic visibly follows from the one before it -- concretely, the Refresher actually picks up the previous topic's Explore, not just related by subject."},
    {"key": "contentPrecision", "label": "Content precision",
     "what": "Every claim, definition and vocabulary word is correct and pitched at this grade -- nothing borrowed from a harder register."},
    {"key": "textbookGrounding", "label": "Textbook grounding",
     "what": "Concepts, examples and tasks trace to specific things this chapter's pages actually say -- no fact introduced that the textbook excerpt doesn't contain."},
    {"key": "classroomRealism", "label": "Classroom realism",
     "what": "Every activity can actually run with one teacher, 30-60 children, fixed benches, a blackboard and the textbook -- nothing else guaranteed -- and a fallback exists for anything that assumes small groups or a lot of movement."},
]
_DIMENSION_KEYS = {d["key"] for d in DIMENSIONS}
_DIMENSION_LABELS_LOWER = {d["label"].lower() for d in DIMENSIONS} | {d["key"].lower() for d in DIMENSIONS}


def _section_bullets(lesson: dict, section: str) -> list:
    if section == "refresher":
        return (lesson.get("previousTopicRefresher") or {}).get("recap") or []
    if section == "concept":
        return lesson.get("concept") or []
    container = lesson.get(section) or {}
    return container.get("points") or []


def _blocked_dimensions(textbook: Optional[dict]) -> dict[str, str]:
    """textbookGrounding can't be honestly judged when nothing was grounded in
    the first place -- that's a property of the INPUT (no textbook match for
    this topic/grade/subject), not the generated lesson."""
    if textbook:
        return {}
    return {"textbookGrounding": "no textbook grounding was available for this generation"}


def _summarise(report: dict, textbook: Optional[dict]) -> dict:
    ratings = [r for r in (report.get("ratings") or []) if isinstance(r, dict)]
    blocked = _blocked_dimensions(textbook)
    targetable = [r for r in ratings if r.get("dimension") not in blocked]
    avg = lambda rs: round(sum(int(r.get("score") or 0) for r in rs) / len(rs), 2) if rs else 0.0
    return {
        "overall": avg(ratings), "targetable": avg(targetable),
        "targetableCount": len(targetable),
        "atOrAbove4": sum(1 for r in targetable if int(r.get("score") or 0) >= 4),
        "blocked": blocked,
    }


def _low_scoring_revisable(report: dict, textbook: Optional[dict], threshold: int = 4) -> list[dict]:
    blocked = _blocked_dimensions(textbook)
    return [r for r in (report.get("ratings") or [])
            if isinstance(r, dict) and r.get("revisable")
            and r.get("dimension") not in blocked
            and r.get("section") in SECTION_KEYS
            and int(r.get("score") or 5) < threshold]


def _sheet_text(lesson: dict, textbook: Optional[dict]) -> str:
    """The whole sheet as a teacher would receive it -- including timings,
    materials and the textbook chapter info, not just section prose, since
    teacherUsability and textbookGrounding are judged against exactly those."""
    parts: list[str] = []
    if lesson.get("objective"):
        parts.append(f"OBJECTIVE FOR THIS PERIOD: {lesson['objective']}")
    if lesson.get("planningNote"):
        parts.append(f"PLANNING NOTE: {lesson['planningNote']}")

    timings = lesson.get("timings") or {}
    if timings:
        total = sum(v for v in timings.values() if isinstance(v, (int, float)))
        per = ", ".join(f"{SECTION_LABELS.get(k, k)} {v} min" for k, v in timings.items())
        parts.append(f"TIMINGS ({total} min total): {per}")
    if lesson.get("materialsUsed"):
        parts.append("MATERIALS THE TEACHER NEEDS: " + ", ".join(str(m) for m in lesson["materialsUsed"]))

    if textbook:
        page_part = (
            f" (pages {textbook['pageStart']}-{textbook.get('pageEnd') or textbook['pageStart']})"
            if textbook.get("pageStart") else ""
        )
        where = f'"{textbook["matchedTopic"]}" in ' if textbook.get("matchedTopic") else ""
        parts.append(f'TEXTBOOK THIS PERIOD TEACHES FROM: {where}Chapter {textbook.get("chapterNumber")}: '
                     f'"{textbook.get("chapterTitle")}"{page_part}')

    for section in SECTION_KEYS:
        bullets = _section_bullets(lesson, section)
        if not bullets:
            continue
        head = SECTION_LABELS[section]
        if section == "challenge":
            activity = (lesson.get("challenge") or {}).get("activity")
            if activity:
                head += f" (activity: {activity})"
        mins = timings.get(section)
        if isinstance(mins, (int, float)) and mins:
            head += f" ({mins} min)"
        if any(isinstance(b, dict) and _bullet_has_image(b) for b in bullets):
            head += " [has a printed textbook picture attached]"
        lines = "\n".join(
            f"  - {b.get('text', '')}: {b.get('detail', '')}" for b in bullets if isinstance(b, dict)
        )
        parts.append(f"{head}:\n{lines}")

    watch = lesson.get("sectionWatch") or {}
    watch_lines = [f"    - {SECTION_LABELS.get(k, k)}: {(v or {}).get('text') if isinstance(v, dict) else v}"
                   for k, v in watch.items() if v]
    if watch_lines:
        parts.append("WHAT THE TEACHER IS TOLD TO WATCH FOR:\n" + "\n".join(watch_lines))

    return "\n\n".join(parts) if parts else "(the sheet is empty)"


_JUDGE_PROMPT = """You are an independent instructional-design reviewer. You did not write this
material -- another system did, and your job is to judge it honestly, neither
defending it nor point-scoring against it.

WHAT YOU ARE JUDGING IT AGAINST. This is a one-period PREP SHEET for a teacher
in an Indian government school: one teacher, 30-60 children, fixed benches, a
blackboard, and the children's own textbook. It is not a personalised adaptive
learning system and was never meant to be. Score it as "is this a good prep
sheet for that teacher, in that room" -- not against an idealised product.

CONTEXT: Grade {grade} {subject}, topic "{topic}"{subtopic_part}.

THE MATERIAL:
{sheet}

Rate it against EXACTLY these {n} dimensions. For each one:
  - "score": 1-5, anchored like this and not on vibes:
      5 = genuinely strong; a good teacher could run this as-is and it would work.
      4 = solid and usable; a small gap a teacher would close themselves without noticing.
      3 = usable but with a real gap they would have to work around.
      2 = present but weak enough to cause a problem mid-lesson.
      1 = absent, or actively wrong.
    Judge what IS there. An accurate 5 is as useful as an accurate 1.
  - "reason": one or two sentences, SPECIFIC to this material.
  - "section": which of refresher/concept/explore/challenge/levelSet this finding
    is mainly about, or "none" if it's about the whole sheet.
  - "revisable": true if a targeted REWRITE of that one section could plausibly
    raise this score, false if the real fix is something this format has no
    place for at all. Be honest -- marking something revisable that a rewrite
    cannot fix wastes the next round.

DIMENSIONS:
{dimension_list}

Return ONLY valid JSON:
{{"ratings": [{{"dimension": "<key>", "score": 1-5, "reason": "...", "section": "...", "revisable": true}}, ...]}}
One entry per dimension listed above, every key present, in the order given."""


async def judge_lesson(
    lesson: dict, *, topic: str, subtopic: Optional[str], grade: str, subject: str, textbook: Optional[dict],
) -> dict:
    dimension_list = "\n".join(f"- {d['key']}: {d['label']} -- {d['what']}" for d in DIMENSIONS)
    prompt = _JUDGE_PROMPT.format(
        grade=grade, subject=subject, topic=topic,
        subtopic_part=f' (subtopic: "{subtopic}")' if subtopic else "",
        sheet=_sheet_text(lesson, textbook), n=len(DIMENSIONS), dimension_list=dimension_list,
    )
    raw = await call_ai(
        [{"role": "user", "content": prompt}],
        {"model": JUDGE_MODEL, "temperature": 0.2, "max_tokens": 3000, "json_mode": True},
    )
    data = json.loads(raw)
    ratings = data.get("ratings") or []
    got = {r.get("dimension") for r in ratings if isinstance(r, dict)}
    missing = _DIMENSION_KEYS - got
    if missing:
        raise ValueError(f"judge left out dimension(s): {sorted(missing)}")
    return {"ratings": ratings}


def _shape_skeleton(section: str) -> str:
    example = '[{"text": "a 6-12 word headline", "detail": "1-2 sentences"}, {"text": "...", "detail": "..."}, {"text": "...", "detail": "..."}]'
    if section == "refresher":
        return f'{{"previousTopic": "...", "recap": {example}}}'
    if section == "challenge":
        return f'{{"activity": "short name, character-for-character unchanged unless THIS finding is about the activity name", "points": {example}}}'
    if section == "explore":
        return f'{{"points": {example}, "imageFocus": "one short phrase to sketch on the board"}}'
    if section == "concept":
        return example
    return f'{{"points": {example}}}'


def _bullets_in(value) -> list:
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        for key in ("points", "recap"):
            if isinstance(value.get(key), list):
                return value[key]
        if "text" in value:
            recovered = [{"text": value.get("text"), "detail": value.get("detail")}]
            i = 2
            while f"text_{i}" in value:
                recovered.append({"text": value.get(f"text_{i}"), "detail": value.get(f"detail_{i}")})
                i += 1
            return recovered
    return []


def _validate_and_normalise(section: str, value, original) -> tuple[Optional[object], Optional[str]]:
    bullets = _bullets_in(value)
    if not bullets:
        return None, "no bullets came back for this section"
    if not (1 <= len(bullets) <= 3):
        return None, f"{len(bullets)} bullets came back, expected 1-3"
    for b in bullets:
        if not isinstance(b, dict):
            return None, "a bullet was not an object"
        text = str(b.get("text") or "").strip()
        if not text:
            return None, "a bullet had no text"
        if text.lower() in _DIMENSION_LABELS_LOWER:
            return None, f"bullet text is literally a rubric dimension name ('{text}')"
        if len(text.split()) > 16:
            return None, f"bullet text reads like a sentence, not a headline ({len(text.split())} words)"
        detail_words = len(str(b.get("detail") or "").split())
        if detail_words > 50:
            return None, f"bullet detail is {detail_words} words, too long for this format"

    bullets = [{"text": b.get("text"), "detail": b.get("detail")} for b in bullets]
    orig = original if isinstance(original, dict) else {}
    vdict = value if isinstance(value, dict) else {}
    if section == "refresher":
        return {"previousTopic": vdict.get("previousTopic") or orig.get("previousTopic"), "recap": bullets}, None
    if section == "challenge":
        return {"activity": vdict.get("activity") or orig.get("activity"), "points": bullets}, None
    if section == "explore":
        return {"points": bullets, "imageFocus": vdict.get("imageFocus") or orig.get("imageFocus") or ""}, None
    if section == "concept":
        return bullets, None
    return {"points": bullets}, None


_REVISE_PROMPT = """This Grade {grade} {subject} prep sheet, topic "{topic}", was reviewed and
found weak on specific points. Rewrite ONLY the sections named below -- leave
every other section exactly as it is, character for character.

THE ROOM THIS IS WRITTEN FOR: one teacher, 30-60 children, fixed benches, a
blackboard, and the children's own textbook -- nothing else guaranteed. No
projector, no printer, no internet.

EVERY "detail" YOU WRITE: 1-2 sentences, SHORT. Name what the teacher does,
plus at most one of (wait time / how students respond / what to watch for).

THE CURRENT SHEET:
{sheet}

THE EXACT JSON SHAPE EACH NAMED SECTION MUST COME BACK AS -- copy these keys
exactly, do not invent your own numbered keys or flatten anything:
{section_shapes}

WHAT TO FIX IN EACH:
{findings}

THE ONE MISTAKE THAT MAKES THIS WORSE THAN DOING NOTHING: writing the CRITIQUE
as the section's content ("Classroom realism: the activity does not scale to
60 children" is a sentence ABOUT the bullet, not a bullet). The output is a
real, better lesson section a teacher reads and teaches from.

Rules:
- Touch ONLY the sections named above.
- Keep the same JSON shape this section already uses -- 1 to 3 {{"text","detail"}}
  bullets, "text" a 6-12 word headline, never a rubric dimension name or a
  sentence describing a problem.
- Fix the SPECIFIC problem named, not a generic rewrite.
- Never introduce a fact this sheet's textbook excerpt doesn't support.

Return ONLY valid JSON: {{"sections": {{"<section>": <the section's full corrected value>, ...}}}}
One entry per section named above, nothing else."""


async def revise_lesson(lesson: dict, findings: list[dict], *, topic: str, grade: str, subject: str) -> tuple[dict, list[str]]:
    generation_model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")

    by_section: dict[str, list[str]] = {}
    for f in findings:
        section = f.get("section")
        if section not in SECTION_KEYS:
            continue
        label = next((d["label"] for d in DIMENSIONS if d["key"] == f["dimension"]), f["dimension"])
        by_section.setdefault(section, []).append(f"{label}: {f['reason']}")

    out = json.loads(json.dumps(lesson))  # deep copy; never mutate the caller's lesson
    if not by_section:
        return out, []

    rejected: list[str] = []
    for section, msgs in by_section.items():
        findings_block = f"- {section}:\n" + "\n".join(f"    * {m}" for m in msgs)
        section_shapes = f'- "{section}": {_shape_skeleton(section)}'
        current = _sheet_text(lesson, None)

        extra_instruction = ""
        applied = False
        for attempt in range(2):
            prompt = _REVISE_PROMPT.format(
                grade=grade, subject=subject, topic=topic, sheet=current,
                findings=findings_block, section_shapes=section_shapes,
            ) + extra_instruction
            try:
                raw = await call_ai(
                    [{"role": "user", "content": prompt}],
                    {"model": generation_model, "temperature": 0.5, "max_tokens": 2000, "json_mode": True},
                )
                data = json.loads(raw)
            except Exception as exc:
                print(f"[prep-quality] revise[{section}] call failed ({exc}); keeping original")
                break

            value = (data.get("sections") or {}).get(section)
            outer_key = section
            if value is None:
                problem = "the section was missing from the answer"
            else:
                original = lesson.get(outer_key) if section != "concept" else lesson.get("concept")
                normalised, problem = _validate_and_normalise(section, value, original)
                if not problem:
                    out[outer_key] = normalised
                    applied = True
                    break
                print(f"[prep-quality] revise[{section}] rejected ({problem})")

            if attempt == 1:
                print(f"[prep-quality] keeping original content for {section} ({problem})")
                break
            extra_instruction = (
                f"\n\nYour previous answer was rejected: {problem}. Write REAL lesson "
                f"content for `{section}` this time -- an activity, a hook, a question a "
                "child answers -- not a description of what was wrong, and make sure the "
                "section key is present in the JSON."
            )

        if not applied:
            rejected.append(section)

    return out, rejected


async def run_quality_loop(
    lesson: dict, *, topic: str, subtopic: Optional[str], grade: str, subject: str, textbook: Optional[dict],
) -> dict:
    """judge -> fix -> re-judge, bounded by MAX_FIX_ROUNDS, keeping the
    best-scoring round rather than just the last one (an independent fix can
    make one dimension worse while improving another)."""
    best_lesson, best_report, best_summary = lesson, None, None
    best_score = -1.0
    history: list[dict] = []
    current_lesson = lesson
    loop_n = 1

    while True:
        report = await judge_lesson(
            current_lesson, topic=topic, subtopic=subtopic, grade=grade, subject=subject, textbook=textbook,
        )
        summary = _summarise(report, textbook)
        low = _low_scoring_revisable(report, textbook)

        if summary["targetable"] > best_score:
            best_score = summary["targetable"]
            best_lesson, best_report, best_summary = current_lesson, report, summary

        history.append({
            "loop": loop_n, "targetable": summary["targetable"], "overall": summary["overall"],
            "fixableFindings": len(low),
        })

        if not low or loop_n > MAX_FIX_ROUNDS:
            break

        current_lesson, rejected = await revise_lesson(current_lesson, low, topic=topic, grade=grade, subject=subject)
        if rejected:
            history[-1]["rejectedSections"] = rejected
        loop_n += 1

    return {"finalLesson": best_lesson, "finalReport": best_report, "finalSummary": best_summary, "history": history}
