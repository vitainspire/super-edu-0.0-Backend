"""A focused, standalone judge for ONE specific failure mode: a real-world
example that is individually true but teaches a false generalisation by
overreaching -- "squares work better than circles for a floor" is true for
flooring but teaches a child "square = good shape, circle = bad shape" when
the actual concept is that shapes whose edges can sit flush against a copy
of themselves tile without gaps, a property triangles and hexagons share.

WHY THIS IS ITS OWN MODULE, NOT A NEW DIMENSION IN quality_judge.DIMENSIONS.
That rubric was deliberately trimmed to 10 dimensions after real runs showed
more than that is noise (see quality_judge.py's own comment on the cut from
18). This failure mode was only DISCOVERED after that rubric was fixed, on a
Loop 3 revision, and confirmed (by full-repo search) to have no existing
check anywhere. Rather than reopen the tuned rubric, it lives here as an
independent pass, run separately, over already-generated material --
production code is untouched.
"""
import json

from prep_flow.llm import call_json
from prep_flow.sections import SECTION_LABELS, container_for, section_text

from .quality_judge import JUDGE_MODEL, _shape_skeleton, _validate_and_normalise

_FRAMING_PROMPT = """You are checking ONE specific failure mode in a Grade {grade} {subject}
prep sheet, topic "{topic}" -- nothing else. This pipeline has already shown
it can generate real-world examples that are individually true but teach the
WRONG lesson by overreaching: e.g. "squares work better than circles for a
floor" is true for THIS purpose but teaches a child "square = good shape,
circle = bad shape" -- a false generalisation -- when the actual concept is
whether the shape's EDGES let copies sit flush against each other, a
property triangles and hexagons also have.

THE CONCEPT SECTION (what the sheet is actually supposed to teach):
{concept_text}

THE REAL-WORLD / EXAMPLE SECTIONS TO CHECK (Real Life and Challenge):
{realworld_text}

For each sentence that makes an absolute or comparative claim about a whole
CATEGORY of thing (a shape, an object, a material) rather than the specific
PROPERTY the concept is actually about, flag it. A claim is fine when it
stays about the property ("shapes with straight edges can sit side-by-side
with no gaps"); it is a problem when it becomes a verdict on the category
itself ("squares are better", "circles don't work", "builders chose
rectangles instead of triangles" implying triangles don't work when they
do).

Return ONLY valid JSON:
{{"score": 1-5 (5 = no overgeneralisation anywhere, 1 = the sheet's central
   example teaches the wrong idea),
  "findings": [{{"text": "the exact sentence/bullet detail with the problem",
                "whyProblematic": "what false belief a child would form from it",
                "suggestedReframe": "a rewritten version that stays on the actual property"}}]}}
If there is nothing to flag, return {{"score": 5, "findings": []}}."""


async def judge_framing(lesson: dict, *, topic: str, grade: str, subject: str) -> dict:
    """One independent rating pass for over-generalisation only. Returns
    {"score": 1-5, "findings": [...]}."""
    concept_text = section_text(lesson, "concept") or "(none)"
    realworld_text = "\n\n".join(
        f"{SECTION_LABELS[s]}: {section_text(lesson, s)}"
        for s in ("realLife", "challenge") if section_text(lesson, s)
    ) or "(none)"

    prompt = _FRAMING_PROMPT.format(
        grade=grade, subject=subject, topic=topic,
        concept_text=concept_text, realworld_text=realworld_text)
    data = await call_json(
        prompt, label="framing-judge", required=("score", "findings"),
        model=JUDGE_MODEL, temperature=0.2, max_tokens=1200)

    findings = [f for f in (data.get("findings") or [])
               if isinstance(f, dict) and str(f.get("text") or "").strip()]
    try:
        score = max(1, min(5, int(data.get("score"))))
    except (TypeError, ValueError):
        score = 3 if findings else 5
    return {"score": score, "findings": findings}


_REFRAME_PROMPT = """Rewrite ONLY the "{section}" section of this Grade {grade} {subject} sheet,
topic "{topic}" -- every other section stays untouched. This section still
has to do its normal job:
{section_rule}

THE CURRENT SECTION:
{current}

WHAT'S WRONG WITH IT: one or more bullets make an absolute or comparative
claim about a whole CATEGORY (a shape, an object) rather than the specific
PROPERTY the lesson is actually about, which teaches a false
generalisation:
{findings}

Rewrite so every claim stays about the actual property, never a verdict on
a category. Keep the same bullets, the same headline text where it was not
the problem, the same real-world setting -- change only what makes the
claim overreach.

Return ONLY valid JSON: {{"section": {section_shape}}}"""


async def reframe_overgeneralization(lesson: dict, findings: list[dict], *,
                                     topic: str, grade: str, subject: str) -> tuple[dict, list[str]]:
    """Rewrites ONLY the realLife/challenge sections that contain a flagged
    over-generalization, keeping every other section and every unflagged
    bullet byte-identical. Reuses quality_judge's own shape-skeleton and
    validate-and-normalise helpers directly rather than reimplementing the
    bullet-shape-recovery logic they already solve."""
    try:
        from generation.compose import GENERATION_MODEL
    except ImportError:
        from .mastery_recovery import _GENERATION_MODEL_FALLBACK as GENERATION_MODEL

    affected = [s for s in ("realLife", "challenge")
               if any(f["text"] in (section_text(lesson, s) or "") for f in findings)]
    out = json.loads(json.dumps(lesson))  # deep copy; never mutate the caller's material
    rejected: list[str] = []

    for section in affected:
        section_findings = [f for f in findings if f["text"] in (section_text(lesson, section) or "")]
        findings_block = "\n".join(
            f'- "{f["text"]}" -- {f["whyProblematic"]}. Reframe toward: {f["suggestedReframe"]}'
            for f in section_findings)

        from prep_flow.sections import SECTION_POLICY
        prompt = _REFRAME_PROMPT.format(
            grade=grade, subject=subject, topic=topic, section=section,
            section_rule=SECTION_POLICY[section]["rule"],
            section_shape=_shape_skeleton(section),
            current=section_text(lesson, section), findings=findings_block)

        try:
            data = await call_json(
                prompt, label=f"framing-reframe[{section}]", required=("section",),
                model=GENERATION_MODEL, temperature=0.4, max_tokens=1200)
        except Exception as exc:                       # noqa: BLE001
            print(f"[framing-reframe] {section}: call failed ({exc}); keeping original")
            rejected.append(section)
            continue

        outer_key, _ = container_for(section)
        normalised, problem = _validate_and_normalise(section, data.get("section"), lesson.get(outer_key))
        if problem:
            print(f"[framing-reframe] {section}: rejected ({problem})")
            rejected.append(section)
            continue
        out[outer_key] = normalised

    return out, rejected
