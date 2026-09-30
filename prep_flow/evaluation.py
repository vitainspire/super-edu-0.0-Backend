"""Blind rubric scoring — the core, shared by the CLI and the activation gate.

Factored out of prep_flow/eval_cli.py so the regression gate scores a candidate
adaptive state with exactly the same rubric a human runs by hand. Two scorers
that drift apart would make the gate's verdict unreproducible, which is the one
property it needs.

Scoring is BLIND in a specific sense: one judge call per sheet, so nothing is
scored by comparison with a sibling. A judge shown both arms would reason about
which is "the improved one"; a judge shown one sheet can only read it.
"""
from typing import Optional

from .llm import call_json

# Each criterion names the section it judges and what a 5 requires. Derived from
# SECTION_POLICY's source rules, so the rubric and the generation contract cannot
# drift apart on what "good" means.
RUBRIC: dict[str, tuple[str, str]] = {
    "seam": (
        "Refresher",
        "Does the Refresher bring back the PREVIOUS lesson's Explore — its actual "
        "scene, object and unanswered question — rather than summarising the "
        "previous topic? 5 = the class re-enters the exact moment they left. "
        "1 = a generic recap, or a scene the previous lesson never had. "
        "Score 3 if this is the chapter's first sheet and it opens from everyday "
        "life without inventing a previous lesson."),
    "concept_fidelity": (
        "Concept",
        "Are the FACTS the textbook's — same numbers, same definitions, same "
        "technical terms — and cited as (Page N)? The EXAMPLE that carries them "
        "need NOT be the book's: one chosen because it works better for these "
        "children is correct here, not a defect. 5 = every fact traceable to a "
        "cited page and nothing asserted the book does not say. 1 = invents or "
        "contradicts a fact, or sends the class to look at a picture those pages "
        "do not contain."),
    "concept_discovery": (
        "Concept",
        "Do the children ACT on the idea rather than being told it and asked to "
        "repeat it? Where the book shows before it names, 5 = they act, notice it "
        "themselves, and only then is the term given. Where the book NAMES FIRST "
        "(see how the book explains this topic, above), following that is correct "
        "and not a defect: 5 = the term is given plainly and the class immediately "
        "goes out to find and test instances of it. Either way, 1 = they are told "
        "the fact and asked to repeat it, or pointed at the page and asked to "
        "discuss it."),
    "book_order": (
        "all six",
        "Does the sheet explain this topic in the ORDER ITS OWN TEXTBOOK PAGES "
        "explain it — see how the book explains this topic, above? 5 = the moves "
        "land in the book's sequence and the book's own printed task is the one "
        "the class runs. 3 = mostly, or the book's order was not given. 1 = the "
        "sheet reorders the explanation, or substitutes its own activity for one "
        "the book prints on these pages."),
    "challenge_runnable": (
        "Challenge",
        "Could a first-time teacher run this Challenge without improvising or "
        "computing in front of the class? 5 = the activity is unambiguous AND the "
        "worked answer is stated substantively in 'detail'. 1 = vague, or the "
        "'answer' is a restatement of the question ('the answer is a triangle') "
        "rather than actual working."),
    "explore_hook": (
        "Explore",
        "Does the Explore end on a genuinely open question a child would want "
        "answered, which the next lesson could pick up? 5 = a real cliffhanger. "
        "1 = closed, already answered, or no question at all."),
    "levelset_invitational": (
        "Level Set",
        "Is the Level Set a self-check and an invitation rather than assessment or "
        "homework? 5 = partners check themselves, the take-home is a genuine "
        "choice. 1 = reads as a test or an assignment."),
    "student_action_first": (
        "all six",
        "Does every section open with something students DO rather than the "
        "teacher explaining? 5 = all six. 1 = most sections open with teacher talk."),
    "grade_fit": (
        "all six",
        "Is the language pitched right for this grade — short sentences, concrete, "
        "playful, nothing abstract or babyish? 5 = exactly right. 1 = badly "
        "mispitched."),
    "materials_realistic": (
        "Materials",
        "Could a resource-level-0 Indian classroom — nothing but the room, the "
        "children and a blackboard, plus free local materials — actually run this "
        "sheet? 5 = yes, everything is to hand. 1 = needs bought or printed items."),
}

JUDGE_PROMPT = """You are an experienced primary-school teacher trainer reviewing ONE lesson prep
sheet. Score it against the rubric below. You are not comparing it with anything;
score only what is in front of you.

GRADE: {grade} | SUBJECT: {subject}
TOPIC: {topic}
{previous}{book_order}{excerpt}
--- BEGIN PREP SHEET ---
{sheet}
--- END PREP SHEET ---

RUBRIC — score each 1-5, where 3 is "acceptable, a competent teacher could use
this" and 5 is "genuinely excellent":

{rubric}

Be discriminating. A sheet that is merely fine scores 3, not 5 — if everything
scores 5 the scores carry no information. Where a criterion does not apply, say so
in the reason and score 3.

Return ONLY valid JSON, no markdown fences:
{{
  "scores": {{{score_keys}}},
  "reasons": {{"<criterion>": "one short sentence naming the specific evidence"}},
  "weakest": "the criterion most in need of work",
  "wouldUseAsIs": true
}}
"""


def methodology_fingerprint() -> str:
    """Everything that decides what a score MEANS, in twelve characters.

    Over the rubric AND the prompt, because either changes the number without
    changing the material. That distinction is not academic: adding the textbook
    excerpt to this prompt moved `concept_fidelity` from "does it sound
    confident and cite pages" to "is the fact actually on the page", and touched
    no criterion text at all. A fingerprint over RUBRIC alone would have called
    scores from before and after that change comparable, and the regression
    harness would have reported the shift as a change in material quality.

    The prompt TEMPLATE, not the filled values — the excerpt a topic happens to
    carry is input, not methodology.
    """
    import hashlib
    import json
    canonical = json.dumps(
        {"rubric": {key: list(spec) for key, spec in RUBRIC.items()},
         "prompt": JUDGE_PROMPT},
        sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]


def rubric_text() -> str:
    return "\n".join(f"- {key} ({section}): {text}"
                     for key, (section, text) in RUBRIC.items())


async def judge_sheet(sheet_text: str, *, topic: str, grade: str, subject: str,
                      previous_explore: Optional[str] = None,
                      book_moves: Optional[list] = None,
                      excerpt: Optional[str] = None,
                      label: str = "sheet") -> dict:
    """Score one rendered prep sheet.

    `previous_explore` is what makes the seam criterion answerable at all —
    without it a judge can only ask "does this Refresher look like a recap",
    which is the exact confusion the seam rule exists to prevent.

    `excerpt` does the same for `concept_fidelity`. That criterion asks whether
    the FACTS are the textbook's — same numbers, same definitions, same technical
    terms — and it was being scored by a judge that had never seen the textbook.
    What it could actually check was whether the sheet looked confident and
    carried (Page N) markers, which a fluent invention also does. Without the
    pages the criterion is unanswerable and the judge is told to score 3, the
    same abstention `book_order` already uses, rather than guessing.
    """
    previous = (
        f"THE PREVIOUS LESSON'S EXPLORE — this sheet's Refresher must build on it:\n"
        f"{previous_explore[:1200]}\n"
        if previous_explore else
        "This is the FIRST sheet of the chapter — there is no previous lesson, so "
        "its Refresher should open from everyday life.\n")

    # Without this the `book_order` criterion is unanswerable and the judge
    # guesses, which is worse than abstaining — hence the explicit "score 3"
    # instruction when the moves are absent rather than a silent omission.
    if book_moves:
        book_order = "\n".join([
            "",
            "HOW THE BOOK ITSELF EXPLAINS THIS TOPIC, in printed order — the sheet is",
            "supposed to follow this:",
            *(f"  {i}. [{m.get('type')}] {m.get('gist')}"
              for i, m in enumerate(book_moves[:12], start=1)),
            "",
        ])
    else:
        book_order = ("\nThe book's own explanatory order was not recorded for this "
                      "topic, so score `book_order` 3.\n")

    if excerpt and str(excerpt).strip():
        # Truncated generously but not unboundedly: a topic's excerpt is two or
        # three textbook pages, and the whole point is that a number in the sheet
        # can be checked against the page it came from — a cut landing mid-table
        # takes the number with it.
        source = "\n".join([
            "",
            "THE TEXTBOOK PAGES THIS TOPIC WAS WRITTEN FROM. `concept_fidelity` is",
            "judged against THIS and nothing else — a fact the sheet asserts that is",
            "not here, or that contradicts what is here, is the defect that criterion",
            "exists to catch. The EXAMPLE carrying a fact need not be the book's; the",
            "fact must be.",
            "--- BEGIN TEXTBOOK ---",
            str(excerpt).strip()[:6000],
            "--- END TEXTBOOK ---",
            "",
        ])
    else:
        source = ("\nThe textbook pages behind this topic were not supplied, so "
                  "`concept_fidelity` cannot be checked against them — score it 3 "
                  "and say so in the reason rather than guessing from how confident "
                  "the sheet sounds.\n")

    data = await call_json(
        JUDGE_PROMPT.format(
            grade=grade, subject=subject, topic=topic, previous=previous,
            book_order=book_order, excerpt=source,
            sheet=sheet_text[:9000], rubric=rubric_text(),
            score_keys=", ".join(f'"{k}": 3' for k in RUBRIC)),
        label=f"judge[{label}]",
        required=("scores",),
        # Low temperature: a judge that varies run to run cannot detect a small
        # regression, which is the only thing it is for.
        temperature=0.1, max_tokens=1600)

    scores = {}
    for key in RUBRIC:
        try:
            scores[key] = max(1, min(5, int(round(float((data.get("scores") or {}).get(key, 3))))))
        except (TypeError, ValueError):
            scores[key] = 3
    return {
        "scores": scores,
        "reasons": data.get("reasons") or {},
        "weakest": data.get("weakest"),
        "wouldUseAsIs": bool(data.get("wouldUseAsIs")),
        "mean": round(sum(scores.values()) / len(scores), 3),
    }


def mean_ci(values: list[float]) -> tuple[float, float, float]:
    """(mean, standard deviation, 95% half-width).

    An interval rather than a p-value, because the question this serves is "do
    these overlap" — and an interval makes the sample size visible in a way a
    bare mean does not.
    """
    n = len(values)
    if n == 0:
        return 0.0, 0.0, 0.0
    mean = sum(values) / n
    if n < 2:
        return mean, 0.0, 0.0
    sd = (sum((v - mean) ** 2 for v in values) / (n - 1)) ** 0.5
    return mean, sd, 1.96 * sd / (n ** 0.5)


def paired_delta(baseline: list[dict], candidate: list[dict],
                 key: str = "index") -> tuple[float, float, int]:
    """(mean delta, 95% half-width, pairs) for candidate minus baseline.

    Paired on topic, because every arm generates the SAME topics. Without
    pairing, an arm that happened to draw an easier topic looks better than it
    is — and with only a handful of topics that difference swamps the effect
    being measured.
    """
    def by_topic(rows: list[dict]) -> dict:
        grouped: dict = {}
        for row in rows:
            grouped.setdefault(row[key], []).append(row["mean"])
        return {k: sum(v) / len(v) for k, v in grouped.items()}

    a, b = by_topic(baseline), by_topic(candidate)
    deltas = [b[t] - a[t] for t in sorted(set(a) & set(b))]
    if not deltas:
        return 0.0, 0.0, 0
    mean, _sd, ci = mean_ci(deltas)
    return mean, ci, len(deltas)
