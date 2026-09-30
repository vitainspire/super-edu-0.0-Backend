"""Cognitive bands — what a child of this age can be asked to DO, not how the
sheet is worded.

Three things about a grade already exist elsewhere, and this module deliberately
adds a fourth rather than a competing copy of any of them:

  * `grade_band_for(grade)`  -> "1-3" / "4-5". Filters the Pedagogy Library, so
    the Challenge activity is already age-appropriate. **This module keys off it**,
    so a cognitive band can never disagree with the activity band.
  * `engagement_level(grade)` -> 1..4, with LEVEL_GUIDANCE. Governs sentence
    length, vocabulary and tone, and reaches the generation prompt.
  * `check_pedagogy_rules`   -> caps headline length for the youngest band.

All three are about **language**. None of them is about cognitive demand, and the
two come apart in a way that is easy to miss: "can generalise the rule that shapes
tessellate" is nine short words of grade-2 vocabulary describing a grade-6 act.
Every existing check passes it.

WHAT IS BOUNDED HERE, AND WHAT IS NOT. Reasoning demand is bounded; content
magnitude is not. It is tempting to write "numbers under 20" into the grade 1-3
band, and it would be wrong — Class 3 Maths chapter 2 teaches numbers to 999, and
the textbook is authoritative for the Concept section. A band that argues with the
book would either be ignored or corrupt the one section that must not be invented.
So these rules constrain the VERB, never the number.

WHERE IT IS CHECKED. On the objectives — the reasoning chain's clauses and each
sheet's `objective` — and not on the prose. Those are short, declarative, and
about the child, so a verb found there means what it says. The same discipline as
the knowledge chain: check the plan, not the paragraphs.
"""
import re
from typing import Optional

from .deps import grade_band_for

COGNITIVE_BANDS = {
    "1-3": {
        "label": "Grades 1-3",
        "summary": "one idea, one change at a time, always through something the "
                   "child can hold, see on the page, or do with their body",
        # What a `gained` clause may legitimately ask of the child.
        "verbs": ["point to", "find", "count", "match", "sort", "trace", "name",
                  "show", "build", "copy", "put in order", "notice", "join in"],
        # Verbs that describe an act this band cannot yet perform independently.
        # Matched as whole words against the objectives only.
        # Formal reasoning acts only. `infer` and `deduce` were here and were
        # wrong: a seven-year-old working out "the cup stayed still, I moved"
        # is inferring, and it is precisely the act this band exists to enable.
        # On the first chapter that rule flagged five topics out of six, which
        # is the signature of a bad rule rather than a bad chapter.
        "beyond": ["generalise", "generalize", "justify", "prove", "derive",
                   "hypothesise", "hypothesize", "evaluate", "abstract",
                   "analyse", "analyze", "critique", "formulate",
                   "reason about", "explain why"],
        "rules": [
            "One idea per topic, and only ONE thing changes at a time. Two "
            "variables at once is a different lesson, not a harder one.",
            "The child must be able to reach the understanding by doing, "
            "pointing, sorting or making — never by being told and agreeing.",
            "A rule can be NOTICED here. It cannot be stated in general form: "
            "\"all the round ones roll\" is the ceiling, not \"round objects roll\".",
            "Nothing rests on remembering something from more than one lesson ago "
            "unless the object is back in their hands.",
        ],
    },
    "4-5": {
        "label": "Grades 4-5",
        "summary": "two steps of reasoning, a rule that can be stated once it has "
                   "been found, and a light step off the concrete",
        "verbs": ["compare", "explain", "predict", "check", "estimate", "group by",
                  "sort by rule", "find the rule", "work out", "decide", "test",
                  "put in order", "measure"],
        "beyond": ["prove", "derive", "hypothesise", "hypothesize", "formulate",
                   "critique", "abstract", "generalise beyond"],
        "rules": [
            "Two steps of reasoning are fine. Three is a project, not a period.",
            "The child may be asked to explain, predict or check — but always "
            "about something they have just done, not in the abstract.",
            "A rule may be stated in general form ONCE the class has found it. "
            "Never handed over first and applied afterwards.",
            "Notation is allowed only after the concrete version has worked.",
        ],
    },
}


def band_for(grade) -> Optional[str]:
    """The cognitive band key for a grade, or None outside the pilot's range.

    Delegates rather than re-deciding. A second grade->band rule that agreed
    today and drifted later is exactly the bug this indirection exists to make
    impossible.
    """
    key = grade_band_for(grade)
    return key if key in COGNITIVE_BANDS else None


def band(grade) -> dict:
    """The band's data, or {} when the grade falls outside 1-5.

    Empty means "no opinion", and every consumer treats it that way: the prompts
    render nothing and the check returns nothing. A grade 7 chapter generates
    exactly as it did before this module existed.
    """
    key = band_for(grade)
    return COGNITIVE_BANDS.get(key) or {}


def band_block(grade) -> str:
    """The band, rendered for a prompt.

    Rendered from the table rather than written out beside it, for the reason
    SECTION_POLICY is: a rule that lives in one prompt string is enforced in one
    prompt string, and the check below reads this same table.
    """
    data = band(grade)
    if not data:
        return ""
    return (
        f"\nWHAT A CHILD IN {data['label'].upper()} CAN BE ASKED TO DO. This is about "
        "the THINKING, not the wording — short words describing an act they cannot yet "
        "perform is the failure to avoid.\n"
        f"  In one line: {data['summary']}.\n"
        "  Reach for: " + ", ".join(data["verbs"][:10]) + ".\n"
        + "".join(f"  - {rule}\n" for rule in data["rules"]) +
        "  This bounds the DEMAND, never the content. If the textbook teaches "
        "three-digit numbers, you teach three-digit numbers — you just do not ask "
        "this class to justify a generalisation about them.\n"
    )


def over_pitched(text: str, grade) -> list[str]:
    """Verbs in `text` that describe an act above this band.

    Whole-word matching, and only ever run against objectives — the chain's
    clauses and a sheet's `objective` line. Run against prose it would fire on
    "the teacher explains", which is not a claim about the child at all.
    """
    data = band(grade)
    if not data or not (text or "").strip():
        return []
    lowered = text.lower()
    return [verb for verb in data["beyond"]
            if re.search(rf"\b{re.escape(verb)}\b", lowered)]
