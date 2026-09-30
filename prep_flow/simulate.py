"""A simulated teacher, for closing the feedback loop before real teachers can.

Real feedback takes a term to accumulate. Until then the loop cannot be tested
end to end — and the alternative I tried first was worse than nothing: I invented
the ratings by hand. That tests the plumbing and nothing else, and it produced a
directive telling generation to fix a materials problem the blind judge scored
4.50/5 on. The optimizer was correctly solving a problem that did not exist.

The difference here is that the simulated teacher reads the ACTUAL sheet and
rates what would actually have gone wrong in the room. So a bad Explore gets
rated badly because it is bad, not because I decided it would be. That makes the
loop falsifiable: if it works, the sections rated badly in round 1 should improve
in round 2.

What this still is NOT:
-
  * **Not a teacher.** A model rating prose written by a model shares its blind
    spots. It cannot tell you that forty eight-year-olds got restless, that the
    chalk ran out, or that the Challenge died because nobody understood the
    instruction in Telugu.
  * **Not evidence about learning.** It rates whether a period would run, not
    whether anyone learned. The only signal for that is `marks`/`tests`.

Use it to prove the loop RESPONDS to quality. Do not use it to conclude the loop
improves teaching.
"""
import random
from typing import Optional

from .llm import call_json
from .sections import SECTION_LABELS, SECTION_ORDER

RATINGS = ("yes", "somewhat", "no")

_TEACHER_PROMPT = """You have just finished teaching this period from the prep sheet below. You teach
grade {grade} {subject} in a government primary school in India: {class_size}
children, no printed materials, one blackboard, {resource_note}.

You are filling in the after-class feedback form. One rating per section:

  YES      — it worked as written; you would use it again unchanged
  SOMEWHAT — it worked but you had to patch something mid-lesson
  NO       — it did not work; you abandoned it, ran out of time, or it confused them

TOPIC: {topic}
{previous}
--- BEGIN PREP SHEET ---
{sheet}
--- END PREP SHEET ---

Every child has the textbook open in front of them. A sheet that says "turn to
page 22" or cites "(Page 14)" is doing exactly the right thing — that is how you
point the class at the book, and it is never a problem.

Rate what would ACTUALLY have happened in your room, not whether the writing is
good:
- Could you run it with what you have? A section needing anything you were not
  given is a NO, however well written — but the textbook is always to hand.
- Was the timing survivable? An eight-minute activity in a three-minute slot is
  a SOMEWHAT at best.
- Did it tell you what to SAY, or did you have to invent it on the spot?
- For the Refresher: did the children actually remember the thing it asks them to
  remember — the scene and the question from last period's Explore? If it asks
  them to recall something the previous lesson never gave them, that is a NO.
- For the Concept: were the facts and numbers the ones in their textbook? If you
  had to correct it against the book in front of the class, that is a NO.

Most sections in a competent sheet are YES. Reserve NO for something that really
broke. Do not spread ratings around to seem balanced — if five sections worked,
rate five YES.

Write a `note` ONLY for a SOMEWHAT or a NO, in your own words, one sentence,
saying what went wrong in the room. Leave it out otherwise. Teachers do not
annotate what worked.

Return ONLY valid JSON, no markdown fences:
{{
  "ratings": {{{rating_keys}}},
  "notes": {{"<section>": "what went wrong, one sentence"}},
  "wouldTeachAgain": true
}}
"""


async def simulate_teacher(sheet_text: str, *, topic: str, grade: str, subject: str,
                           class_size: int = 40, resource_level: int = 0,
                           previous_explore: Optional[str] = None,
                           label: str = "sheet", temperature: float = 0.7) -> dict:
    """One teacher's form, after teaching one period from one sheet.

    Temperature is deliberately HIGH, unlike the rubric judge's 0.1. A judge that
    varies cannot detect a regression; a teacher population that does not vary is
    not a population. Five teachers of the same sheet should disagree a little,
    which is what gives the pattern matcher something to average over.
    """
    # The textbook is ALWAYS present. Stating "no printed materials" without this
    # exception cost a whole experiment: the simulated teachers read page
    # citations as demands for a book they did not have, complained about them,
    # and the optimizer wrote a directive telling Concept to stop citing pages —
    # which is the one thing that section exists to do. Round-2 material scored
    # 0.158 lower across every section as a result.
    resource_note = {
        0: "the children have their textbooks and nothing else — no worksheets, "
           "no printed handouts, no manipulatives",
        1: "the children have their textbooks, notebooks and pencils",
        2: "the children have their textbooks, and you can collect free local "
           "materials — stones, seeds, sticks, bottle caps",
    }.get(resource_level, "the children have their textbooks and nothing else")

    previous = (
        f"LAST PERIOD'S EXPLORE — what the class actually did before this one:\n"
        f"{previous_explore[:1000]}\n"
        if previous_explore else
        "This was the first period of the chapter; there was no previous lesson.\n")

    data = await call_json(
        _TEACHER_PROMPT.format(
            grade=grade, subject=subject, class_size=class_size,
            resource_note=resource_note, topic=topic, previous=previous,
            sheet=sheet_text[:9000],
            rating_keys=", ".join(f'"{s}": "yes"' for s in SECTION_ORDER)),
        label=f"teacher[{label}]", required=("ratings",),
        temperature=temperature, max_tokens=1200)

    raw_ratings = data.get("ratings") or {}
    raw_notes = data.get("notes") or {}
    ratings, notes = {}, {}
    for section in SECTION_ORDER:
        value = str(raw_ratings.get(section, "yes")).strip().lower()
        ratings[section] = value if value in RATINGS else "yes"
        note = raw_notes.get(section) or raw_notes.get(SECTION_LABELS[section])
        if isinstance(note, str) and note.strip() and ratings[section] != "yes":
            notes[section] = " ".join(note.split())[:400]

    return {"ratings": ratings, "notes": notes,
            "wouldTeachAgain": bool(data.get("wouldTeachAgain", True)),
            "score": round(sum({"yes": 1.0, "somewhat": 0.5, "no": 0.0}[r]
                               for r in ratings.values()) / len(ratings), 3)}


def to_feedback_rows(simulated: dict, *, scope_key: str, material_id: str = None,
                     run_id: str = None, taught_on=None, teacher_id: str = None) -> list[dict]:
    """A simulated form as `prep_flow_feedback` rows — the same shape the real
    HTTP endpoint writes, so nothing downstream can tell the difference."""
    import uuid as _uuid

    return [{
        "id": str(_uuid.uuid4()),
        "material_id": material_id,
        "run_id": run_id,
        "teacher_id": teacher_id,
        "scope_key": scope_key,
        "section": section,
        "rating": rating,
        "note": simulated["notes"].get(section),
        "taught_on": taught_on,
    } for section, rating in simulated["ratings"].items()]


def section_rates(forms: list[dict]) -> dict:
    """Per-section satisfaction across many simulated forms — the number the
    experiment actually turns on."""
    out = {}
    for section in SECTION_ORDER:
        values = [f["ratings"][section] for f in forms if section in f.get("ratings", {})]
        if not values:
            out[section] = {"n": 0, "score": None}
            continue
        score = sum({"yes": 1.0, "somewhat": 0.5, "no": 0.0}[v] for v in values) / len(values)
        out[section] = {
            "n": len(values), "score": round(score, 3),
            "yes": values.count("yes"), "somewhat": values.count("somewhat"),
            "no": values.count("no"),
        }
    return out


def teacher_personas(count: int, seed: int = 7) -> list[dict]:
    """A small population, so the pattern matcher averages over disagreement
    rather than over one voice repeated.

    Varied on the two axes that genuinely change whether a period runs — how
    much is in the room, and how big the class is — not on personality, which
    would just add noise the matcher cannot act on.
    """
    rng = random.Random(seed)
    return [{"class_size": rng.choice([28, 35, 40, 48, 55]),
             "resource_level": rng.choice([0, 0, 0, 1]),
             "temperature": rng.choice([0.6, 0.7, 0.8])}
            for _ in range(count)]
