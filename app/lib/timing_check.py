"""Standalone, additive check: does each section's ACTUAL content take as long
as it was ALLOCATED, or does the teacher run out of material with real minutes
still on the clock? Not part of the live pipeline -- reads an already-generated
lesson, estimates real classroom time per section, and where there is a
genuine shortfall, writes an "extra" block to fill it. Same precedent as
critique_textbook_content.py / quality_judge.py: built and tested standalone,
only wired into the live bridge later if it earns its keep.

WHY THIS IS THE OPPOSITE DIRECTION FROM THE SIZE-CAPPING WORK EARLIER THIS
SESSION. That work made sections SHORTER on purpose -- cut wordiness, not
substance, because the mean bullet detail was running 45 words against a
30-word target. This catches the opposite failure: a section now genuinely
too short for its own allocated minutes. The two must not fight each other --
"extra" is never folded into the capped bullets themselves; it is a clearly
separate, optional block, so the size discipline from before still holds for
the required content and this only adds to what is explicitly marked extra.

WHY "EXTRA" IS A STRETCH, NOT PADDING. The pipeline already has a name for
"more demand on the same content when time allows" -- deep_agents/skills/
stretch-design: same materials, no new object, open-ended so it cannot be
"finished", never required. That is exactly the shape "extra" has to take, on
purpose -- not a repeated example, not a slower version of the same task, a
genuine harder or wider question a fast-moving class can reach for.
"""
import json
from typing import Optional

from prep_flow.llm import call_json
from prep_flow.sections import SECTION_LABELS, SECTION_ORDER

# Below this, a gap is rounding error in either the model's own timing choice
# or the estimate itself -- not worth writing a block over. Chosen to match
# the granularity "planning" already allocates in (whole minutes).
MIN_GAP_MINUTES = 1.5


def _section_bullets_text(material: dict, section: str) -> str:
    """The section's bullets as plain numbered text, for the estimator to read
    -- reuses nothing from generation's own renderer on purpose, so this stays
    a fresh read of what a teacher would actually have to say and do, not a
    read of how the section was built."""
    value = material.get(section)
    if section == "refresher":
        value = material.get("previousTopicRefresher") or {}
        bullets = value.get("recap") if isinstance(value, dict) else value
    elif isinstance(value, dict):
        bullets = value.get("points")
    else:
        bullets = value
    lines = []
    for i, b in enumerate(bullets or [], start=1):
        if not isinstance(b, dict):
            continue
        text = (b.get("text") or "").strip()
        detail = (b.get("detail") or "").strip()
        lines.append(f"{i}. {text} — {detail}" if detail else f"{i}. {text}")
    return "\n".join(lines) if lines else "(empty)"


_TIMING_PROMPT = """You are a classroom-experienced teacher trainer estimating REAL time, not
reading time. For a room of 30-60 children with one teacher, some things take
far longer than the words describing them: distributing objects to every
pair, waiting for a show of hands, walking a row to check work, children
physically moving. Other things are fast: a teacher pointing and stating a
fact takes seconds, however long the sentence describing it is.

GRADE {grade} {subject}, TOPIC: {topic}

FIRST, JUDGE THE TOPIC'S OWN COMPLEXITY -- this changes what a real gap even
means. A simple, single-fact topic (naming a shape, counting to ten) is
SUPPOSED to be quick; a few unfilled minutes there is not a defect, it is
what a simple topic looks like, and padding it teaches nothing. A genuinely
multi-step or abstract topic (fractions, the water cycle, place value across
three digits) earns real stretch time when it runs short, because there is
real depth left to reach for. Judge complexity from what this topic actually
requires the child to do -- one step, or several building on each other --
not from how long the writer happened to make the sentences.

THE SIX SECTIONS AS WRITTEN, WITH WHAT EACH WAS ALLOCATED:
{sections_block}

MATERIALS ALREADY IN USE THIS LESSON (nothing outside this list may be
introduced in "extra" -- an unvetted new object skips every realism check
the lesson's actual anchor already passed): {materials_list}

For EACH section, estimate the REAL minutes this would actually take a
teacher to run with a full room -- distribution time, wait time, the teacher
actually talking, not just words-per-minute reading speed.

Then compare to ALLOCATED. A gap only counts as real if it is at least
{min_gap} minutes AND the topic's complexity justifies filling it:
  - SIMPLE topic, small gap: "extra" is null. A quick topic ending a little
    early is correct, not broken -- do not invent stretch content just
    because the clock has room. Leave the teacher's own judgment to fill it.
  - SIMPLE topic, large gap (this section is genuinely underwritten): a
    short, single-line "extra" is fine -- one sentence, nothing elaborate.
  - COMPLEX topic, real gap: propose a genuine STRETCH -- either more demand
    on the SAME object, or a DIFFERENT angle using an object ALREADY LISTED
    above (never a new one), reachable by a class that finished early, never
    required, never collected.
  wrong (padding): "Ask a few more students to share their guess."
  wrong (unvetted new object): "Now use a water bottle to show a cylinder."
    (water bottle is not in materials list -- not checked against this room)
  right (stretch, same object): "find one object in the room where the top
  view and the side view would look the same, and say why."
  right (stretch, different angle, already-vetted object): if a matchbox is
  already this lesson's material, "using the SAME matchbox, predict which
  2D shape you have not traced yet before checking."

If the gap does not clear both bars above, "extra" is null -- most sections
need no addition, and inventing one for a section that already fits its
complexity is the same mistake as writing a supplement no gap actually needs.

Return ONLY valid JSON:
{{"topicComplexity": "simple" or "complex",
  "complexityReason": "one sentence, specific to what this topic actually asks a child to do",
  "sections": [
  {{"section": "<key>", "estimatedMinutes": <number>, "allocatedMinutes": <number>,
    "extra": null or {{"text": "6-12 word headline", "detail": "1-2 sentences, under 30 words"}}}}
  , ...]}}
One entry per section, in this order: {section_order}. Every key present."""


async def check_timing(material: dict, *, topic: str, grade: str, subject: str) -> dict:
    """One call, all six sections at once -- cheaper than per-section, and the
    estimator needs to see the whole period to judge pacing sensibly anyway
    (a light Concept followed by a heavy Challenge is a real, intentional
    shape, not six independent guesses)."""
    timings = material.get("timings") or {}
    sections_block = "\n\n".join(
        f"— {SECTION_LABELS[s]} (allocated {timings.get(s, '?')} min):\n"
        f"{_section_bullets_text(material, s)}"
        for s in SECTION_ORDER
    )
    materials_list = ", ".join(material.get("materialsUsed") or []) or "(none listed)"
    prompt = _TIMING_PROMPT.format(
        grade=grade, subject=subject, topic=topic, sections_block=sections_block,
        materials_list=materials_list,
        min_gap=MIN_GAP_MINUTES, section_order=", ".join(SECTION_ORDER),
    )
    data = await call_json(prompt, label="timing-check", required=("sections",),
                            temperature=0.2, max_tokens=2000)
    return {
        "topic": topic,
        "topicComplexity": data.get("topicComplexity"),
        "complexityReason": data.get("complexityReason"),
        "sections": data.get("sections") or [],
    }


def apply_extras(material: dict, timing_result: dict) -> dict:
    """Writes each section's "extra" (if any) into a NEW top-level
    `extras: {section: {text, detail}}` field on the lesson -- never merged
    into `points`, so BULLETS_PER_SECTION and the word cap still apply to the
    required content exactly as before, and a renderer can show "extra" as
    its own clearly optional block or omit it entirely."""
    out = json.loads(json.dumps(material))
    canonical = {s.lower(): s for s in SECTION_ORDER}
    extras: dict[str, dict] = {}
    for row in timing_result.get("sections") or []:
        section = row.get("section")
        extra = row.get("extra")
        if not section or not isinstance(extra, dict) or not (extra.get("text") or "").strip():
            continue
        # the model doesn't reliably echo the section key in the exact case
        # given in the prompt -- normalise so a case-sensitive lookup like
        # extras.get("challenge") can't silently miss "Challenge".
        key = canonical.get(str(section).strip().lower(), section)
        extras[key] = {"text": extra.get("text"), "detail": extra.get("detail")}
    if extras:
        out["extras"] = extras
    return out
