"""An ADDITIVE, standalone "diagnose -> recover -> reassess" layer for an
already-generated, already-reviewed prep sheet.

WHY THIS IS SEPARATE FROM THE SIX-SECTION CONTRACT (prep_flow/sections.py),
NOT A NEW SECTION IN IT. app/lib/quality_judge.py's own judge prompt already
names this exact gap: a dimension is marked NOT revisable when "the real fix
is a feature the current six-section format has no place for at all... a
full diagnose-reteach-reassess sequence." Rather than guess at reshaping the
production contract, this module writes the missing piece as its own
top-level `masteryLoop` block, called AFTER generation/validation have
already finished -- nothing here edits prep_flow/, generation/,
validation_flow/, or quality_judge.py. It is meant to be run over saved
output, compared by hand, and only folded into the real pipeline later if it
proves out.

WHY IT ASKS FOR A PRE-WRITTEN BRANCH, NOT REAL BRANCHING. No per-student
result ever reaches this pipeline (there is a `student_topic_mastery` table
elsewhere in the product, fed by an unrelated quiz flow, with no connection
to a Level Set check or a named misconception). So "if wrong, do X" is
authored once, at generation time, for the teacher to apply by judgment --
not something the app decides at runtime. That is an honest scope limit, not
an oversight.
"""
import json
from typing import Optional

from prep_flow.llm import call_json
from prep_flow.sections import section_text

# Same generation model the rest of the pipeline writes prose with -- this is
# authoring lesson content, not judging it, so there is no "different model
# family" requirement the way there is for JUDGE_MODEL.
_GENERATION_MODEL_FALLBACK = "google/gemini-2.5-flash"


_MASTERY_RECOVERY_PROMPT = """This is an ADDITIVE layer on top of an already-generated, already-reviewed
Grade {grade} {subject} prep sheet, topic "{topic}". The sheet itself is
finished and is NOT being rewritten here -- you are authoring one new,
separate block: a short mastery check with a pre-written response for a
child who gets it wrong. Nothing you write replaces or edits the sheet.

THE ALREADY-WRITTEN SHEET (context only -- ground your check in what it
actually taught; invent no new content, no new vocabulary, no new numbers):
CONCEPT: {concept_text}
CHALLENGE: {challenge_text}

{misconception_block}

Write:
1. "masteryChecks" -- 2 or 3 short checks a teacher can run in under a
   minute each, testing DIRECTLY whether a child can do what CONCEPT/
   CHALLENGE just taught -- not a new topic, not a harder extension. Each
   one: a real question or task the teacher poses ("prompt"), what a correct
   response looks like ("correctAnswerCue"), and the SPECIFIC wrong answer a
   child who has not understood would actually give ("commonWrongAnswer") --
   not "gets it wrong", the real wrong thing a real child says or does.
2. "diagnose" -- {diagnose_instruction}
3. "recovery" -- what the teacher does RIGHT THEN, in the same period, for a
   child who gave the wrong answer: "simplerReteach" is one concrete,
   smaller-scope re-explanation or easier example (never "explain it again"
   or "give more examples" -- name the actual simpler example or step down),
   and "reassessCue" is the one question that tells the teacher the child
   has now got it, short enough to ask on the spot.

Return ONLY valid JSON, no markdown fences:
{{"masteryChecks": [{{"prompt": "...", "correctAnswerCue": "...", "commonWrongAnswer": "..."}}, ...],
  "diagnose": {{"misconception": "...", "whyThisHappens": "..."}},
  "recovery": {{"simplerReteach": "...", "reassessCue": "..."}}}}"""


def build_mastery_recovery_prompt(lesson: dict, *, topic: str, grade: str, subject: str,
                                  misconception: Optional[str] = None) -> str:
    concept_text = section_text(lesson, "concept") or "(no concept text found)"
    challenge_text = section_text(lesson, "challenge") or "(no challenge text found)"

    if misconception:
        misconception_block = (
            "THE MISCONCEPTION THIS CLASS IS KNOWN TO FORM HERE (identified from the "
            "chapter's own curriculum reasoning, in the child's own words):\n"
            f'    "{misconception}"'
        )
        diagnose_instruction = (
            'name this exact misconception as "misconception" and explain, in one '
            'sentence, why a child taught this lesson would form it ("whyThisHappens")'
        )
    else:
        misconception_block = (
            "THE MISCONCEPTION THIS CLASS IS KNOWN TO FORM HERE: none was identified "
            "for this topic specifically -- infer the single most likely wrong belief "
            "a child would form from THIS sheet's own concept and challenge, in the "
            'child\'s own voice, first person (e.g. "the cup changed when I walked '
            'round it", not "students may confuse views").'
        )
        diagnose_instruction = (
            'name the misconception you inferred as "misconception" and explain why a '
            'child taught this lesson would form it ("whyThisHappens")'
        )

    return _MASTERY_RECOVERY_PROMPT.format(
        grade=grade, subject=subject, topic=topic,
        concept_text=concept_text, challenge_text=challenge_text,
        misconception_block=misconception_block, diagnose_instruction=diagnose_instruction,
    )


def _clean_checks(value) -> list[dict]:
    if not isinstance(value, list):
        return []
    out = []
    for item in value:
        if not isinstance(item, dict):
            continue
        prompt = str(item.get("prompt") or "").strip()
        correct = str(item.get("correctAnswerCue") or "").strip()
        wrong = str(item.get("commonWrongAnswer") or "").strip()
        if prompt and correct:
            out.append({"prompt": prompt, "correctAnswerCue": correct, "commonWrongAnswer": wrong})
    return out


def _clean_diagnose(value) -> dict:
    if not isinstance(value, dict):
        return {}
    belief = str(value.get("misconception") or "").strip()
    why = str(value.get("whyThisHappens") or "").strip()
    return {"misconception": belief, "whyThisHappens": why} if belief and why else {}


def _clean_recovery(value) -> dict:
    if not isinstance(value, dict):
        return {}
    reteach = str(value.get("simplerReteach") or "").strip()
    reassess = str(value.get("reassessCue") or "").strip()
    return {"simplerReteach": reteach, "reassessCue": reassess} if reteach and reassess else {}


async def generate_mastery_recovery(lesson: dict, *, topic: str, grade: str, subject: str,
                                    misconception: Optional[str] = None) -> dict:
    """One LLM call that authors the additive masteryLoop block. Raises if the
    response has no usable content -- a silently empty block is worse than a
    run that stopped and said why (same reasoning as judge_lesson's raise on
    a malformed rubric)."""
    prompt = build_mastery_recovery_prompt(
        lesson, topic=topic, grade=grade, subject=subject, misconception=misconception)

    try:
        from generation.compose import GENERATION_MODEL
    except ImportError:
        GENERATION_MODEL = _GENERATION_MODEL_FALLBACK

    data = await call_json(
        prompt, label="mastery-recovery", required=("masteryChecks", "diagnose", "recovery"),
        model=GENERATION_MODEL, temperature=0.4, max_tokens=1500)

    checks = _clean_checks(data.get("masteryChecks"))
    diagnose = _clean_diagnose(data.get("diagnose"))
    recovery = _clean_recovery(data.get("recovery"))
    if not checks or not diagnose or not recovery:
        raise ValueError(
            f"mastery-recovery response missing usable content for topic {topic!r}: "
            f"checks={len(checks)}, diagnose={bool(diagnose)}, recovery={bool(recovery)}")
    return {"masteryChecks": checks, "diagnose": diagnose, "recovery": recovery}


def attach_mastery_recovery(lesson: dict, mastery_loop: dict) -> dict:
    """A NEW lesson dict with the mastery loop spliced in under its own
    top-level key. Additive only -- never mutates `lesson`, never touches
    levelSet or any existing container in prep_flow.sections._CONTAINERS."""
    return {**json.loads(json.dumps(lesson)), "masteryLoop": mastery_loop}


def render_mastery_loop_md(topic: str, mastery_loop: dict) -> str:
    lines = [f"# Mastery loop — {topic}", "", "## Mastery checks"]
    for i, c in enumerate(mastery_loop.get("masteryChecks") or [], 1):
        lines.append(f"{i}. **Ask:** {c.get('prompt', '')}")
        lines.append(f"   - Correct response: {c.get('correctAnswerCue', '')}")
        if c.get("commonWrongAnswer"):
            lines.append(f"   - Common wrong answer: {c['commonWrongAnswer']}")

    diagnose = mastery_loop.get("diagnose") or {}
    if diagnose:
        lines += ["", "## Diagnose",
                 f"- **Misconception:** {diagnose.get('misconception', '')}",
                 f"- **Why it happens:** {diagnose.get('whyThisHappens', '')}"]

    recovery = mastery_loop.get("recovery") or {}
    if recovery:
        lines += ["", "## Recovery",
                 f"- **Simpler reteach:** {recovery.get('simplerReteach', '')}",
                 f"- **Reassess with:** {recovery.get('reassessCue', '')}"]

    return "\n".join(lines)
