"""Standalone, additive check: reads a fully-generated six-section sheet and
finds genuine IDEA-level duplication across sections -- the same explanation
or fact taught more than once, wasting minutes without adding anything. Not
the same problem Concept's own rule already guards against (the same OBJECT
appearing in both Concept and Challenge, which is fine when they do
different jobs with it) -- this catches the same TEACHING happening twice.

Same precedent as critique_textbook_content.py / quality_judge.py: standalone,
reports findings, does not touch the live pipeline. Findings from this module
are shaped to feed directly into quality_judge.revise_lesson() -- same
(dimension, section, reason) shape every other finding in this pipeline
already uses, so the existing trim/rewrite machinery handles the fix; this
module's only job is finding real duplication, not rewriting anything itself.
"""
from prep_flow.llm import call_json
from prep_flow.sections import SECTION_LABELS, SECTION_ORDER

_DEDUP_PROMPT = """You are checking ONE finished lesson sheet for wasted repetition -- the same
idea genuinely taught, explained, or demonstrated in more than one section,
not just the same object mentioned twice.

GRADE {grade} {subject}, TOPIC: {topic}

FIRST, JUDGE THE TOPIC'S OWN COMPLEXITY -- this changes what "repetition"
even means here. For a SIMPLE topic (naming a shape, one fact, one step),
the same explanation showing up twice is pure waste -- there is nothing to
reinforce, it is just said again. For a genuinely COMPLEX or multi-step
topic (fractions, the water cycle, anything a child needs several angles on
to actually hold onto), the SAME idea meeting the child again in a different
context is deliberate reinforcement, not a defect -- that is how complex
ideas are supposed to be taught. Judge complexity from what the topic
actually asks a child to build understanding of, not from section length.

WHAT EACH SECTION IS FOR, so you know which one SHOULD keep a given idea:
  Concept — the core idea, teacher-led, first explanation
  Real Life — where the idea already exists outside class, teacher-led
  Challenge — the first hands-on practice, student-led
  Level Set — checks it landed, sends judgment out the door
  Explore — a take-home activity, unrelated to what happens in class

THE FULL SHEET:
{sheet}

Find duplication that is actually wasteful, given the complexity you just
judged: the SAME explanation, demonstration, or fact repeated with NO new
angle, context, or depth -- not two sections both mentioning a chair for
different reasons, not Level Set's required recap of Concept (that
repetition is the section's job, not a defect), and NOT a complex topic
meeting the same idea from a genuinely different angle across sections.
  real duplication (simple topic): Concept explains why a matchbox tiles and
  a bangle doesn't; Challenge's PLAY step re-explains the identical reason
  before the activity starts, instead of just running the activity.
  real duplication (complex topic): the exact same worked example, word for
  word in substance, appears in both Concept and Real Life with no new
  angle -- reinforcement means a different context, not a copy.
  not duplication (complex topic): Concept introduces why fractions need
  equal parts; Real Life revisits the same idea through sharing food
  unequally -- same idea, genuinely different angle, which is the point.
  not duplication (any topic): Concept and Real Life both mention a chair,
  but Concept demonstrates views and Real Life connects it to
  furniture-making -- two different jobs with the same object.

For each real instance found, decide which ONE section should keep it --
whichever section's job (above) the idea actually belongs to -- and name
which other section(s) should be trimmed of the repeat.

Return ONLY valid JSON:
{{"topicComplexity": "simple" or "complex",
  "complexityReason": "one sentence, specific to what this topic actually asks a child to do",
  "findings": [
  {{"idea": "one phrase naming what's repeated", "keepIn": "<section key>",
    "trimFrom": ["<section key>", ...], "reason": "one sentence, specific to what's repeated and where"}}
  , ...]}}
Empty list if the sheet is clean -- most sheets need nothing here, and
inventing a finding on a sheet that has no real duplication is the same
mistake as any other over-eager critique. On a complex topic, expect fewer
findings, not more -- deliberate reinforcement is the correct shape there."""


async def check_duplication(material: dict, *, topic: str, grade: str, subject: str) -> dict:
    from app.lib.quality_judge import _sheet_text  # reuse, not duplicate

    prompt = _DEDUP_PROMPT.format(
        grade=grade, subject=subject, topic=topic, sheet=_sheet_text(material),
    )
    data = await call_json(prompt, label="dedup-check", required=("findings",),
                            temperature=0.2, max_tokens=1500)
    return {
        "topic": topic,
        "topicComplexity": data.get("topicComplexity"),
        "complexityReason": data.get("complexityReason"),
        "findings": data.get("findings") or [],
    }


def to_revise_findings(dedup_result: dict) -> list[dict]:
    """Reshapes this module's findings into the (dimension, section, reason)
    triples quality_judge.revise_lesson() already knows how to consume --
    one entry per (idea, trimFrom-section) pair, since revise_lesson groups
    findings by section and rewrites one section at a time."""
    out: list[dict] = []
    for f in dedup_result.get("findings") or []:
        keep = f.get("keepIn")
        reason = f.get("reason") or ""
        idea = f.get("idea") or "this idea"
        for section in f.get("trimFrom") or []:
            if section not in SECTION_ORDER or section == keep:
                continue
            out.append({
                "dimension": "duplication",
                "section": section,
                "reason": (
                    f"'{idea}' is already taught in {SECTION_LABELS.get(keep, keep)} "
                    f"and does not need re-explaining here. {reason} Trim this "
                    f"section back to its own job -- keep any parts that are NOT "
                    f"this repeated explanation, remove only the repeat."
                ),
            })
    return out
