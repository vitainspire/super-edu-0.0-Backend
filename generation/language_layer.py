"""The Telugu-English bilingual layer -- a separate, bounded pass applied

AFTER a lesson's English content is fully finalized (figures attached,
citations cleaned), never merged into the main generation call. Two reasons:

  * The generation prompt already carries a lot of hard-won, specific rules
    (the if-then gap-closer, a genuine either/or in Challenge, the floor
    field, object continuity). Asking the same call to also produce a second
    language risks trading compliance with those rules for compliance with
    this one -- one call doing two different jobs worse than two calls doing
    one job each.
  * This is ONE bounded `call_json` call per lesson, the same shape as the
    existing consistency/learner/realism/diagnostic passes -- never an agent
    loop. That distinction cost real money to learn elsewhere in this
    pipeline (see prep_pipeline_bridge.py's PREP_FLOW_DEEP_AGENTS comment) and
    is deliberately not repeated here.

Implements telugu-english-language-layer (Edu_VitaBook skill), adapted onto
THIS pipeline's own six-section schema rather than that skill's own field
names (say_or_do_en/te, ask_en/te, ...) -- there is no 1:1 field mapping, so
the section-to-treatment table below is the translation of that skill's
rules onto `points: [{"text", "detail"}]` bullets.

EVERY OUTPUT OF THIS PASS IS AN UNREVIEWED DRAFT. No reviewed Telugu glossary
exists for any book this pipeline has generated from yet. `_meta.teluguReview`
is stamped "unreviewed-draft" on every lesson this runs on, and any caller
displaying `detail_te` must show that alongside it.
"""
import re

from prep_flow.llm import call_json

_TELUGU_SCRIPT = re.compile(r"[ఀ-౿]")
_LATIN_LETTER = re.compile(r"[A-Za-z]")

# The skill's own "Checks before sharing" section, section 6: "Telugu fields
# are at least 60% Telugu script" -- the minimum PROPORTION, not just
# presence, so a `detail_te` that is mostly English with one Telugu word
# tacked on (which reads to a teacher as decoration, not support) fails this
# the same way an empty one would.
_MIN_TELUGU_SCRIPT_RATIO = 0.60


def _is_telugu_proportioned(text: str) -> bool:
    """Does this Telugu line actually carry the language, not just gesture at it?

    Ratio of Telugu-script letters to ALL letters (Telugu + Latin) in the
    string. Latin letters legitimately appear inside a correct Telugu
    cue -- key terms, exercise labels, a quoted English sentence, per the
    skill's own rule 4 -- so this is not "must be pure Telugu script"; it is
    "Telugu must still be the majority of the line", which is what the
    skill's 60% figure actually measures.
    """
    telugu_count = len(_TELUGU_SCRIPT.findall(text))
    latin_count = len(_LATIN_LETTER.findall(text))
    total = telugu_count + latin_count
    if total == 0:
        return False
    return (telugu_count / total) >= _MIN_TELUGU_SCRIPT_RATIO
from prep_flow.sections import SECTION_ORDER, bullets_of

# Cue: a short Telugu gist beneath a teacher-said line. Full: an independent,
# complete Telugu rendering of something the teacher or a parent DOES by
# following it -- same materials, same steps, same responses as the English.
# See the skill's own table, section 1.
_TREATMENT = {
    "refresher": "cue",
    "concept": "cue",
    "realLife": "cue",
    "challenge": "full",
    "levelSet": "cue",
    "explore": "full",
}

_LANGUAGE_LAYER_PROMPT = """You are applying ONE skill -- telugu-english-language-layer -- to a
lesson that has already been fully written in English. You add a Telugu layer to each
bullet's "detail" field. You do not change the English, you do not add or remove bullets,
and you do not touch the mathematics, the facts, or the steps.

ENGLISH LEADS. Telugu supports. Every Telugu line means EXACTLY the same as its English
line -- a cue may be shorter, but it must never add, drop or change a step, a material, a
number, or a fact.

WRITE IN TELUGU, NOT ABOUT TELUGU WORDS DROPPED INTO AN ENGLISH SENTENCE. Every
"detail_te" you write is checked afterward and DISCARDED if it is not AT LEAST 60% Telugu
script by letter count (English key terms and quoted English sentences are allowed inside
it and do not count against this, but they cannot be the majority of the line). Write the
actual sentence in Telugu -- subject, verb, the connecting words -- and drop in the
English terms only where rule 4 below requires them, not the reverse. A line like "Ask
students to observe rectangle side lines carefully" with one Telugu word added is NOT
Telugu support and will be thrown away; a line built in Telugu from the start, with
"rectangle" kept in English because it is a term being taught, is what this skill asks
for.

TWO TREATMENTS, PER SECTION (this topic's sections and their treatment are listed below):
- "cue": a SHORT Telugu gist of the English detail -- one or two sentences, not a full
  translation. This is for a line the TEACHER SAYS.
- "full": a COMPLETE, independent Telugu rendering of the English detail -- same
  materials, same steps, same responses, nothing shortened. This is for a line the
  teacher or a parent DOES by following it (an activity, a home task).

HOW THE TELUGU ITSELF MUST BE WRITTEN:
1. Everyday SPOKEN Telugu, the words a village child hears at home. Never formal,
   literary, or Sanskritised Telugu, and never a coined textbook word.
2. Keep IN ENGLISH, IN LATIN SCRIPT, even inside the Telugu sentence: any key term the
   child is learning to read/write/say in English (page-specific vocabulary, named
   quantities, exercise labels), and any English sentence the child is meant to say aloud
   or write -- never transliterate these into Telugu script.
3. Numbers and symbols stay exactly as the English prints them (627, <, >, 2 x 10 = 20).
4. If you are not confident a specific Telugu word is correct and natural for this region,
   do not guess -- write the concept in simpler Telugu you ARE confident of, or leave that
   one term in English, rather than invent or mistranslate a word.
5. Never invent a second Telugu name for a term that already has one elsewhere in this
   lesson -- reuse the same Telugu word every time the same English term recurs.

THIS TOPIC'S SECTIONS AND THEIR TREATMENT:
{section_treatments}

Return ONLY valid JSON, no markdown fences, this exact shape -- one entry per bullet, in
the same order as given, naming the section and bullet index (0-based) it belongs to:
{{
  "bullets": [
    {{"section": "concept", "index": 0, "detail_te": "Telugu text for this bullet's detail, per its section's treatment above"}},
    "... one entry for EVERY bullet in EVERY section below, same order, none skipped"
  ]
}}

THE LESSON'S BULLETS, ENGLISH, TO ADD TELUGU TO:
{bullets_block}
"""


def _collect_bullets(lesson: dict) -> list[dict]:
    """Every bullet across all six sections, in a flat, ordered list."""
    out = []
    for section in SECTION_ORDER:
        for i, bullet in enumerate(bullets_of(lesson, section) or []):
            if not isinstance(bullet, dict):
                continue
            out.append({"section": section, "index": i, "text": bullet.get("text") or "",
                       "detail": bullet.get("detail") or ""})
    return out


def _bullets_block(bullets: list[dict]) -> str:
    lines = []
    for b in bullets:
        treatment = _TREATMENT.get(b["section"], "cue")
        lines.append(f'[{b["section"]} #{b["index"]}, treatment={treatment}]')
        lines.append(f'  text: {b["text"]}')
        lines.append(f'  detail: {b["detail"]}')
    return "\n".join(lines)


def _section_treatments_block() -> str:
    return "\n".join(f"  {section}: {treatment}" for section, treatment in _TREATMENT.items())


async def add_telugu_layer(lesson: dict) -> dict:
    """Return a COPY of `lesson` with `detail_te` added to every bullet.

    Never mutates the English content. A lesson this fails on (a bad model
    response, a parse error) is returned unchanged, with `_meta.teluguReview`
    left unset -- best-effort, same as the pedagogy-lookup and board-sketch
    passes elsewhere in this pipeline: losing the Telugu layer costs this one
    lesson its bilingual version, not the run.
    """
    bullets = _collect_bullets(lesson)
    if not bullets:
        return lesson

    prompt = _LANGUAGE_LAYER_PROMPT.format(
        section_treatments=_section_treatments_block(),
        bullets_block=_bullets_block(bullets),
    )

    try:
        data = await call_json(
            prompt, label="language-layer", required=("bullets",),
            temperature=0.3, max_tokens=1200 + 220 * len(bullets),
        )
    except Exception as exc:
        print(f"[generation:language_layer] Telugu layer failed for this lesson, "
              f"leaving it English-only: {exc}")
        return lesson

    by_key = {}
    dropped_thin = 0
    for row in (data.get("bullets") or []):
        if not isinstance(row, dict):
            continue
        section, index = row.get("section"), row.get("index")
        detail_te = (row.get("detail_te") or "").strip()
        if section is None or index is None or not detail_te:
            continue
        # THE SKILL'S OWN 60% CHECK, APPLIED HERE RATHER THAN TRUSTED TO THE
        # MODEL. A `detail_te` that is mostly English with one Telugu word
        # gestures at the skill without doing the actual job -- dropped
        # rather than shipped, the same "worse than none" reasoning
        # SECTION_POLICY already uses for a guessed regional word.
        if not _is_telugu_proportioned(detail_te):
            dropped_thin += 1
            continue
        by_key[(section, int(index))] = detail_te
    if dropped_thin:
        print(f"[generation:language_layer] dropped {dropped_thin} detail_te "
              f"below the skill's 60% Telugu-script threshold")

    out = dict(lesson)
    applied = 0
    for section in SECTION_ORDER:
        section_bullets = bullets_of(out, section)
        if not section_bullets:
            continue
        for i, bullet in enumerate(section_bullets):
            if not isinstance(bullet, dict):
                continue
            detail_te = by_key.get((section, i))
            if detail_te:
                bullet["detail_te"] = detail_te
                bullet["treatment_te"] = _TREATMENT.get(section, "cue")
                applied += 1

    meta = dict(out.get("_meta") or {})
    meta["teluguReview"] = "unreviewed-draft"
    meta["teluguBulletsApplied"] = applied
    meta["teluguBulletsTotal"] = len(bullets)
    out["_meta"] = meta
    return out
