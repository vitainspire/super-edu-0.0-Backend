"""Consistency & Validation Agent — the four checks from the design, made real.

    topic continuity  |  competency alignment  |  consistent structure  |  grade fit

Most of it is deterministic, and that is the point. "Does the Refresher of T8
actually pick up the Explore of T7" is a measurable question — the two texts
either share the scene and the object or they do not — and measuring it is both
cheaper and more reliable than asking a model whether it thinks they connect.
The LLM pass at the end handles only what no rule can: whether the chapter reads
like one teacher's work, and whether any single period is pitched wrongly for the
grade.

Findings carry a severity, and severity is what drives cost:

  * `blocking`  — the sheet is wrong in a way a teacher would hit in class. Sends
                  the topic back for repair.
  * `advisory`  — worth recording, not worth an LLM call. Stored with the
                  material, so when the feedback loop later sees this section
                  rated "no", the note is already there.

The distinction is deliberately conservative. Regenerating a 40-topic chapter
because eleven sheets had a slightly short Real Life bullet spends real money to
re-roll the same dice.
"""
import re
from collections import Counter
from typing import Optional

from prep_flow.bands import band, over_pitched
from prep_flow.clauses import (ABSOLUTES, CLAUSE_STOPWORDS, same_activity,
                       mentions as clause_mentions,
                       polarity as clause_polarity,
                       root as clause_root, roots as clause_roots)
from prep_flow.deps import engagement_level, validate_visuals
from prep_flow.llm import call_json
from prep_flow.sections import (
    BULLETS_PER_SECTION,
    OPTIONAL_WHEN_FIRST,
    SECTION_LABELS,
    SECTION_ORDER,
    bullets_of,
    canonical_section,
    handoff_of,
    section_text,
)
from prep_flow.moves import book_activity, verbatim_moves
from prep_flow.agents.planning import vocabulary_ledger
from prep_flow.agents.reasoning import anchors_for, strands

# The pilot's prompt forbids these outright: they reframe a lesson as assessment,
# which is the opposite of what a prep sheet is for.
BANNED_WORDS = ("quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite")

# Openings that mean the teacher is talking first — the rule every section is
# supposed to invert.
TEACHER_FIRST = re.compile(
    r"^\s*(explain|tell\s+(?:them|students|the\s+class)|say\s+that|describe\s+to|"
    r"lecture|state\s+that|inform\s+(?:them|students)|teach\s+them\s+that)\b", re.I)

_WORD = re.compile(r"[a-z][a-z'-]{2,}")
_NUMBER = re.compile(r"(?<![\w.])(\d+(?:[.,]\d+)?)(?:\s*(?:st|nd|rd|th))?(?![\w])")

# Numbers that are stage directions, not claims about the content. A `detail`
# field is *supposed* to say "wait 10 seconds" and "give them 2 minutes", and
# counting those as facts the textbook must corroborate flags every well-written
# bullet in the batch — which is worse than not checking at all, because a
# validator that always complains is one people stop reading.
_STAGE_DIRECTION = re.compile(
    r"\d+\s*(?:-|\s)?\s*(?:second|sec|minute|min|hour|hr)s?\b"
    r"|\b(?:wait|pause|allow|give|take|for|within|about|around|count\s+to)\s+\d+\b"
    r"|\bbullet\s*\d+\b|\bstep\s*\d+\b|\bpart\s*\(?\d+\)?\b"
    r"|\(?\s*page\s*\d+\s*\)?"
    r"|\b(?:grade|class|std)\.?\s*\d+\b",
    re.I)

STOPWORDS = frozenset("""
a an the and or but if then than that this these those there here of in on to for with
without from by at as is are was were be been being do does did doing have has had
having will would shall should can could may might must not no nor so such very too
also its it he she they them their his her our your you we us i me my one two some any
each other more most much many few less least own same both all how what when where
which who whom why while about into over under again once out up down off above below
students student teacher class ask asks say says let lets tell tells make makes take
takes use uses show shows give gives put puts get gets go goes come comes see sees look
looks hold holds write writes read reads count counts pair pairs partner partners
minute minutes second seconds watch listen answer answers question questions
""".split())


def content_words(text: str) -> set[str]:
    """The words that carry meaning, for overlap scoring.

    Overlap on stopwords and classroom furniture ("students", "ask", "pairs")
    measures nothing — every bullet in every section contains them — so a
    continuity check built on raw word overlap passes trivially. Stripping them is
    what makes the score mean "these two texts are about the same thing".
    """
    return {w for w in _WORD.findall((text or "").lower())
            if w not in STOPWORDS and len(w) > 3}


def numbers_in(text: str) -> set[str]:
    return {n.replace(",", "") for n in _NUMBER.findall(text or "")}


def _finding(section: Optional[str], message: str, severity: str = "blocking",
             check: str = "") -> dict:
    return {"section": section, "message": message, "severity": severity, "check": check}


# ── Per-topic checks ─────────────────────────────────────────────────────────

def check_structure(material: dict, is_first: bool) -> list[dict]:
    """All six sections, present, with the right number of usable bullets."""
    findings: list[dict] = []
    for section in SECTION_ORDER:
        bullets = bullets_of(material, section)
        if not bullets:
            if section in OPTIONAL_WHEN_FIRST and is_first:
                continue
            findings.append(_finding(
                section, f"{SECTION_LABELS[section]} is missing or empty — all six "
                         f"sections must be present on every sheet", check="structure"))
            continue
        usable = [b for b in bullets
                  if isinstance(b, dict) and (b.get("text") or "").strip()]
        if len(usable) < len(bullets):
            findings.append(_finding(
                section, f"{SECTION_LABELS[section]} has "
                         f"{len(bullets) - len(usable)} bullet(s) with no text",
                check="structure"))
        if len(usable) != BULLETS_PER_SECTION:
            # Always blocking, including off-by-one. Advisory-only for one extra
            # bullet was tried and shipped a real 4-bullet Concept section that
            # never got repaired -- an off-by-one is still a spec violation the
            # teacher reads, not a rounding error the pipeline can shrug at.
            severity = "blocking"
            findings.append(_finding(
                section, f"{SECTION_LABELS[section]} has {len(usable)} bullets, "
                         f"expected exactly {BULLETS_PER_SECTION}",
                severity=severity, check="structure"))
        for i, bullet in enumerate(usable, start=1):
            detail = (bullet.get("detail") or "").strip()
            if not detail:
                findings.append(_finding(
                    section, f"{SECTION_LABELS[section]} bullet {i} has no 'detail' — "
                             f"the teacher has nothing to actually say",
                    severity="advisory", check="structure"))
                continue
            # The generation prompt asks for "2-4 sentences, under 60 words" (a
            # real 45-minute period, not the original 30-minute target -- see
            # compose.py's _BULLET). Blocking above 75 (headroom past the
            # 60-word target, not a hard cut at it) catches real bloat without
            # punishing a genuine three-clause instruction that lands at 62-68.
            word_count = len(detail.split())
            if word_count > 75:
                findings.append(_finding(
                    section, f"{SECTION_LABELS[section]} bullet {i} detail is "
                             f"{word_count} words, target is under 60 -- cut the "
                             f"reasoning, keep the instruction",
                    severity="blocking", check="structure"))

    if not (material.get("objective") or "").strip():
        findings.append(_finding(None, "objective is missing", check="structure"))

    if not (material.get("floor") or "").strip():
        findings.append(_finding(
            None, "floor is missing -- no stated path through this sheet's own "
                  "objects and tasks for the weakest child in the room",
            severity="blocking", check="structure"))

    challenge = material.get("challenge")
    activity = (challenge or {}).get("activity") if isinstance(challenge, dict) else None
    if not (activity or "").strip():
        findings.append(_finding("challenge", "Challenge has no activity name",
                                 check="structure"))

    watch = material.get("sectionWatch") or {}
    absent = [s for s in SECTION_ORDER
              if not (watch.get(s) or {}) and not (s in OPTIONAL_WHEN_FIRST and is_first)]
    if absent:
        findings.append(_finding(
            None, "sectionWatch missing for: " + ", ".join(SECTION_LABELS[s] for s in absent),
            severity="advisory", check="structure"))
    return findings


def check_continuity(material: dict, previous: Optional[dict],
                     previous_topic: str, is_first: bool,
                     spec: Optional[dict] = None) -> list[dict]:
    """The seam: does this Refresher actually come from the previous Explore?

    Scored on shared content words rather than judged, because the failure mode is
    specific and measurable — a Refresher that recaps the previous TOPIC instead
    of the previous EXPLORE reads perfectly well and shares almost no vocabulary
    with the scene the class was actually in.
    """
    findings: list[dict] = []
    refresher = section_text(material, "refresher")

    if is_first:
        if refresher:
            # `_normalise_material()` force-nulls previousTopicRefresher for
            # topic 1 — reaching here with content anyway means that
            # enforcement was bypassed (material built outside the normal
            # generation path, or a resequence that made a different topic
            # "first" without renormalising it). Topic 1 gets NO Refresher,
            # full stop — not just no fabricated "last lesson" reference. A
            # cold-open recap built from general prior knowledge reads to a
            # teacher exactly like a real one, which is the failure this
            # exists to catch.
            findings.append(_finding(
                "refresher", "Topic 1 has a Refresher, but this is the first topic of "
                             "the chapter — there is no previous lesson to recap, so "
                             "this section should be empty", check="continuity"))
        return findings

    if not refresher:
        findings.append(_finding(
            "refresher", f"Refresher is empty, but this topic follows "
                         f"'{previous_topic}' and must build on its Explore",
            check="continuity"))
        return findings

    if previous is None:
        findings.append(_finding(
            "refresher", "the previous topic's material is missing, so this Refresher "
                         "could not be grounded in its Explore",
            severity="advisory", check="continuity"))
        return findings

    previous_explore = section_text(previous, "explore")
    handoff = handoff_of(previous)
    handoff_text = " ".join(str(v) for v in handoff.values() if isinstance(v, str))
    source_words = content_words(previous_explore) | content_words(handoff_text)
    refresher_words = content_words(refresher)

    if not source_words:
        findings.append(_finding(
            "refresher", f"'{previous_topic}' produced no usable Explore text, so this "
                         f"Refresher has nothing to derive from",
            severity="advisory", check="continuity"))
        return findings

    shared = refresher_words & source_words
    # A third is a deliberate floor. A Refresher that genuinely reaches back into
    # the previous scene reuses its nouns; one that shares less than a third of
    # its content words with it is talking about something else, whatever it says.
    ratio = len(shared) / max(1, len(refresher_words))
    if len(shared) < 3 and ratio < 0.33:
        findings.append(_finding(
            "refresher",
            f"Refresher does not pick up '{previous_topic}'s Explore — only "
            f"{len(shared)} content word(s) in common "
            f"({', '.join(sorted(shared)) or 'none'}). It should bring back that "
            f"Explore's own scene, object and unanswered question.",
            check="continuity"))
    elif ratio < 0.2:
        findings.append(_finding(
            "refresher",
            f"Refresher only loosely echoes '{previous_topic}'s Explore "
            f"({len(shared)} shared content words) — it names the topic but not the "
            f"scene the class was in.",
            severity="advisory", check="continuity"))

    # WHERE DID THE REFRESHER'S WORDS COME FROM, if not the previous Explore?
    #
    # The overlap test above is necessary and not sufficient. A Refresher
    # fabricated from THIS topic's own textbook pages still shares chapter-level
    # vocabulary with the previous Explore — same chapter, same subject, often
    # the same objects — so it clears a ratio floor while describing a lesson the
    # class never had.
    #
    # That is not hypothetical. Measured over 8 repeats of a 4-topic fixture, the
    # one topic that opens a generation window scored seam 1.75 against 3.19 for
    # its in-window siblings, and the judge's reason was the same every time:
    # "invents a previous match-stick activity that was not in the provided
    # PREVIOUS LESSON'S EXPLORE", "from the current lesson's textbook content,
    # not the previous lesson's explore".
    #
    # Comparing the two sources by the words UNIQUE to each is what separates
    # them: shared vocabulary is uninformative, and a Refresher drawn from the
    # wrong source gives itself away on the words only that source has.
    own_excerpt = content_words(str(spec.get("excerpt") or "")) if spec else set()
    if own_excerpt and refresher_words:
        only_previous = refresher_words & (source_words - own_excerpt)
        only_own = refresher_words & (own_excerpt - source_words)
        # Two clear words of margin, so a Refresher that legitimately mentions
        # this topic's object in passing is not accused of inventing a lesson.
        if len(only_own) >= len(only_previous) + 2:
            findings.append(_finding(
                "refresher",
                f"Refresher appears to be built from THIS topic's own pages rather "
                f"than '{previous_topic}'s Explore — {len(only_own)} content word(s) "
                f"unique to this topic's textbook ({', '.join(sorted(only_own)[:6])}) "
                f"against {len(only_previous)} unique to the previous Explore. A "
                f"Refresher recaps a lesson the class actually had.",
                check="continuity"))

    declared = (material.get("previousTopicRefresher") or {})
    if isinstance(declared, dict):
        named = (declared.get("previousTopic") or "").strip()
        if named and previous_topic and not (
            content_words(named) & content_words(previous_topic)
        ):
            findings.append(_finding(
                "refresher",
                f"previousTopicRefresher.previousTopic says '{named}' but the previous "
                f"topic is '{previous_topic}'",
                severity="advisory", check="continuity"))
    return findings


def check_handoff(material: dict, plan: dict, is_last: bool) -> list[dict]:
    """Does this Explore leave the next Refresher something specific to hold?

    Checked even on the final topic. A chapter's last Explore hands off to the
    next chapter, and an Explore that ends vague rather than specific is a
    weaker Explore regardless of what follows it.

    Explore no longer carries a separate `handoff` object (see sections.py's
    "explore" rule) -- its own THIRD point is what Refresher recalls by name
    tomorrow. This checks that point exists and is specific, not that a
    removed field is present.
    """
    findings: list[dict] = []
    points = [b for b in (bullets_of(material, "explore") or []) if isinstance(b, dict)]
    last = points[-1] if points else None
    last_text = (str((last or {}).get("text") or "") + " " +
                str((last or {}).get("detail") or "")).strip()
    explore = section_text(material, "explore")

    if not last_text:
        findings.append(_finding(
            "explore",
            "Explore's last point is empty, so the next topic's Refresher has "
            "nothing specific to bring back",
            severity="blocking" if not is_last else "advisory", check="handoff"))
    elif len(last_text.split()) < 6:
        findings.append(_finding(
            "explore",
            "Explore's last point is too thin to recall by name tomorrow — name "
            "the specific real-life thing to notice, not a vague prompt",
            severity="advisory", check="handoff"))

    planned = (plan or {}).get("exploreHook") or {}
    planned_text = " ".join(str(v) for v in planned.values() if isinstance(v, str))
    if planned_text.strip() and explore:
        overlap = content_words(planned_text) & content_words(explore)
        if len(overlap) < 2:
            findings.append(_finding(
                "explore",
                "Explore ignores the planned handoff, so the next topic's Refresher — "
                "which was planned against it — will not match",
                severity="advisory", check="handoff"))
    return findings


# Reported-teacher-speech openers sections.py's "explore" rule now requires --
# checked literally because the rule alone has already been proven, more than
# once this session (the diagnostic gate, the earlier bullet/word-cap gap), to
# not reliably survive into generation on its own. A real v12 run wrote
# "At home, look at a chair from directly above." for a detail -- no literal
# "homework" anywhere, so no keyword-ban on that word would have caught it;
# what actually marks the failure is the MISSING report-of-the-telling.
_EXPLORE_REPORTED_SPEECH = re.compile(
    r"^\s*(tell|ask)\s+(the\s+)?(students?|class|children|kids)\b", re.IGNORECASE)


def check_explore_voice(material: dict) -> list[dict]:
    """Each Explore point's detail must report the teacher telling the class,
    not command the child directly -- see check_explore_voice's docstring
    reasoning above and sections.py's "explore" rule for the RIGHT/WRONG pair
    this mirrors."""
    findings: list[dict] = []
    points = [b for b in (bullets_of(material, "explore") or []) if isinstance(b, dict)]
    for i, point in enumerate(points, start=1):
        detail = str(point.get("detail") or "").strip()
        if not detail:
            continue
        if not _EXPLORE_REPORTED_SPEECH.match(detail):
            findings.append(_finding(
                "explore",
                f"Explore point {i}'s detail reads as a direct command to the "
                "child rather than the teacher's reported instruction -- start "
                "it with 'Tell students that...' / 'Tell students to...' / "
                "'Ask students to...'",
                severity="blocking", check="explore_voice"))

    explore = material.get("explore")
    image_focus = str((explore or {}).get("imageFocus") or "").strip()
    if image_focus and points:
        points_words = content_words(" ".join(
            f"{p.get('text') or ''} {p.get('detail') or ''}" for p in points))
        if not (content_words(image_focus) & points_words):
            findings.append(_finding(
                "explore",
                f"Explore's board sketch ('{image_focus}') names something none "
                "of Explore's own points mention -- it must sketch what THIS "
                "section is telling the class to notice, not another section's "
                "anchor object",
                severity="advisory", check="explore_voice"))
    return findings


def check_concept_grounding(material: dict, excerpt: str,
                            chapter_text: str = "") -> list[dict]:
    """Concept is textbook-only. This is where that is enforced.

    Numbers are checked hard and prose softly, on purpose. A number in a Concept
    bullet that appears nowhere on the page is the single most damaging thing this
    pipeline can produce — a teacher reads it out as fact — whereas paraphrasing
    the book's sentence in simpler words is exactly what the section is supposed
    to do, so low word overlap is a hint, not a verdict.
    """
    findings: list[dict] = []
    concept = section_text(material, "concept")
    if not concept:
        return findings
    if not (excerpt or "").strip():
        findings.append(_finding(
            "concept", "no textbook text was available for these pages, so the Concept "
                       "section could not be checked for grounding",
            severity="advisory", check="grounding"))
        return findings

    book_numbers = numbers_in(excerpt)
    cited_pages = {str(p) for p in (material.get("pagesCited") or []) if p is not None}
    stated = numbers_in(_STAGE_DIRECTION.sub(" ", concept))
    # 1/2/3 are excluded because they are almost never content: they are "in
    # pairs", "the first one", "three of them". A genuinely wrong small number
    # would slip through, and that is the right trade against flagging every
    # sheet that says "work in pairs".
    off_page = sorted(n for n in stated - book_numbers - cited_pages
                      if n not in {"1", "2", "3"} and len(n) <= 6)
    # Two tiers, and the difference is worth the extra parameter.
    #
    # Checked against the topic's own pages alone, this fired on 4, 5 and 0 for a
    # Class 3 chapter that prints all three across pages 16-30 — they simply were
    # not inside that topic's slice. Three false blocks out of five findings, each
    # of which spent a repair round re-rolling a sheet that was correct. A number
    # the book uses IS the book's own value; which page it sits on is a fact about
    # the printer, not about the teaching.
    #
    # What stays blocking is the number the book never uses anywhere, which on the
    # same chapter was 103 and 425 — invented place-value examples a teacher would
    # read out as the book's. Borrowed-from-elsewhere stays advisory, because a
    # value correct on page 20 can still be wrong in the context of page 29.
    chapter_numbers = numbers_in(chapter_text) if chapter_text else set()
    invented = [n for n in off_page if n not in chapter_numbers]
    borrowed = [n for n in off_page if n in chapter_numbers]
    if invented:
        findings.append(_finding(
            "concept",
            f"Concept states number(s) that appear nowhere in this chapter: "
            f"{', '.join(invented)}. Concept must use the book's own values.",
            check="grounding"))
    if borrowed:
        findings.append(_finding(
            "concept",
            f"Concept uses number(s) from elsewhere in the chapter rather than these "
            f"pages: {', '.join(borrowed)} — correct for the book, worth checking they "
            f"are correct here",
            severity="advisory", check="grounding"))

    book_words = content_words(excerpt)
    concept_words = content_words(concept)
    if concept_words:
        shared = concept_words & book_words
        ratio = len(shared) / len(concept_words)
        # Lowered from 0.18, and the reason is a deliberate change of contract.
        #
        # Concept may now carry the book's idea through a BETTER example than the
        # book printed — a cup the class is holding rather than a picture of one.
        # Word overlap is exactly what that costs: the fact is identical and the
        # nouns are not. Scored at the old floor this check would have fired on
        # precisely the sheets the new rule asks for, which is the failure mode
        # where a validator quietly reverts a design decision.
        #
        # It is kept, low, because a Concept sharing almost NOTHING with its pages
        # is still worth a look — that is no longer "a different example", it is a
        # different topic. The numeric check above is what actually guards the
        # facts, and it is unchanged.
        if ratio < 0.08:
            findings.append(_finding(
                "concept",
                f"only {round(ratio * 100)}% of the Concept section's content words "
                f"appear in the textbook pages it is supposed to teach — a fresh "
                f"example is fine, but check it is still teaching this topic",
                severity="advisory", check="grounding"))

    # "Look at the picture on page 12" when page 12 has no such picture is the one
    # way the new freedom can hurt a teacher, and it happens mid-lesson in front of
    # the class. Pointing AT the book is only safe when a figure was actually
    # ingested for these pages; citing the page as the source of the idea is always
    # safe and is what the contract now asks for.
    points_at_book = re.search(
        r"\b(?:look|see|point|shown?|picture|image|figure|diagram)\b[^.!?]{0,40}"
        r"\b(?:on|at|in)\s+page\s*\d+", concept, re.I)
    # A caption in the excerpt is a real picture, even with no id attached to it.
    #
    # This fired nine times on the first captioned run and every one was wrong. It
    # asked `figureRefs`, which is empty by design for a book whose captions were
    # recovered inline rather than through a classified ingest — so it read 54
    # genuine pictures as none, and told the sheet to stop pointing at illustrations
    # that are printed in the children's book. The same stale assumption the
    # generation prompt carried, in the check that was supposed to catch it.
    #
    # Matched case-INSENSITIVELY, and on the marker rather than on "[Figure:".
    # The inline-caption path writes "[Figure: ...]"; the classified ingest
    # writes "[FIGURE fig_11_1 -> imgs/x.jpg: ...]". A literal lowercase test
    # sees the first and misses the second, so on a classified book this check
    # quietly collapsed back to `figureRefs` alone — reintroducing, for exactly
    # the books with the richest figure data, the false positive the paragraph
    # above describes being fixed.
    has_pictures = bool(material.get("figureRefs") or []) or bool(
        re.search(r"\[FIGURE(?![A-Za-z])", excerpt or "", re.I))
    if points_at_book and not has_pictures:
        findings.append(_finding(
            "concept",
            "Concept tells the class to look at something on the page, but no figure "
            f"was ingested for these pages — \"{points_at_book.group(0)[:60]}\" may point "
            "at nothing. Cite the page for the idea instead, and have them look at a "
            "real object.",
            severity="advisory", check="grounding"))

    if not cited_pages and not re.search(r"page\s*\d+", concept, re.I):
        findings.append(_finding(
            "concept", "Concept cites no page, so the idea it teaches cannot be traced "
                       "back to the book",
            severity="advisory", check="grounding"))
    return findings


def check_book_fidelity(material: dict, spec: dict) -> list[dict]:
    """Did the sheet run the book's own tasks, and in the book's own order?

    The two things "follow the textbook" means that grounding checks do not
    already cover. check_concept_grounding asks whether the sheet INVENTED
    anything; this asks whether it OMITTED or REPLACED something — the other
    direction, and until now the unwatched one. A Challenge that quietly swaps
    the book's paper-folding activity for a library card-sort passes every
    grounding check ever written, because it added no fact.
    """
    findings: list[dict] = []
    moves = spec.get("moves") or material.get("bookMoves") or []
    if not moves:
        return findings

    # 1. The book's activity IS the Challenge, where the book prints one.
    #
    # Scored against EVERY activity these pages print, and satisfied by any one of
    # them. A spread of pages routinely sets more than one task — Class 3 Maths
    # pages 12-13 print both "which is drawn from the top" and two match-the-view
    # exercises — and a period has time for one. Checked against only the first,
    # this blocked a sheet that was running the book's page 12 activity 2(i) and
    # page 13 activity 2(ii), by page and sub-activity number, because the
    # extractor happened to list a different task first.
    printed = [m for m in moves if m.get("type") == "activity" and m.get("verbatim")]
    if printed:
        challenge = section_text(material, "challenge")
        name = ((material.get("challenge") or {}).get("activity")
                if isinstance(material.get("challenge"), dict) else "") or ""
        got = content_words(f"{name} {challenge}")

        def ran(task: dict) -> bool:
            # Scored against the book's words rather than the sheet's, for the
            # same reason title_score is: the question is "is the book's task in
            # here", not "are these the same length". A staged activity is mostly
            # staging.
            wanted = content_words(task["verbatim"])
            return not wanted or len(wanted & got) / len(wanted) >= 0.25

        if not any(ran(task) for task in printed):
            listed = "; ".join(f'"{t["verbatim"][:70]}"' for t in printed[:2])
            findings.append(_finding(
                "challenge",
                f"these pages set the children their own task{'s' if len(printed) > 1 else ''} — "
                f"{listed} — and the Challenge does not appear to be running any of them. "
                f"The children have them printed in front of them; stage one of those "
                f"rather than a substitute.",
                check="book_fidelity"))

    # 2. The book's exercises are used somewhere, not silently dropped. Advisory:
    # a period legitimately may not reach the exercise block printed on its last
    # page, and blocking on that would repair sheets that are correct.
    for move in verbatim_moves(moves):
        if move["type"] != "practice" or not move.get("verbatim"):
            continue
        wanted = content_words(move["verbatim"])
        anywhere = content_words(" ".join(
            section_text(material, s) for s in SECTION_ORDER))
        if wanted and len(wanted & anywhere) / len(wanted) < 0.2:
            findings.append(_finding(
                "levelSet",
                f'the book prints practice on these pages that no section uses — '
                f'"{move["verbatim"][:80]}". The class has it open in front of them.',
                severity="advisory", check="book_fidelity"))
            break

    # 3. Did the model register the order it was given? It cannot express the
    # order structurally — the sections are JSON keys — so its echo is the only
    # cheap signal, and a mismatch means the bullets were probably written for
    # the canonical order instead of this book's.
    derived = material.get("teachingOrder") or list(SECTION_ORDER)
    echo = [canonical_section(s) for s in ((material.get("_meta") or {}).get("teachingOrderEcho") or [])]
    echo = [s for s in echo if s]
    if echo and echo != list(derived):
        findings.append(_finding(
            None,
            "the sheet restated its teaching order as "
            f"{' > '.join(echo)} but this topic's pages teach in the order "
            f"{' > '.join(derived)} — check the sections were written for the "
            "positions they are printed in",
            severity="advisory", check="book_fidelity"))
    return findings


def check_competency_alignment(material: dict, spec: dict) -> list[dict]:
    """Does the sheet teach the competencies the topic was matched on?

    Advisory throughout. Competencies come from an LLM extraction and a fuzzy
    canonical resolution, so "Count Objects" not appearing verbatim is at least
    as likely to be an extraction artefact as a real misalignment — and blocking
    on it would send good sheets back for repair.
    """
    knowledge = spec.get("knowledge") or {}
    competencies = [c for c in (knowledge.get("competencies") or []) if isinstance(c, str)]
    if not competencies:
        return []

    haystack = " ".join([
        material.get("objective") or "",
        section_text(material, "concept"),
        section_text(material, "challenge"),
        section_text(material, "levelSet"),
    ])
    words = content_words(haystack)
    covered = [c for c in competencies if content_words(c) & words]
    if not covered:
        return [_finding(
            None,
            f"none of this topic's competencies ({', '.join(competencies[:3])}) is "
            f"visible in the objective, Concept, Challenge or Level Set",
            severity="advisory", check="competency")]
    return []


def check_pedagogy_rules(material: dict, activity_name: Optional[str],
                         grade, book_task: Optional[dict] = None,
                         is_first: bool = False) -> list[dict]:
    """The rules that apply to every section: student action first, no assessment
    vocabulary, the activity named as selected, one currency."""
    findings: list[dict] = []
    level = engagement_level(grade)

    for section in SECTION_ORDER:
        bullets = bullets_of(material, section) or []
        for i, bullet in enumerate(bullets, start=1):
            if not isinstance(bullet, dict):
                continue
            text = (bullet.get("text") or "").strip()
            if i == 1 and TEACHER_FIRST.match(text):
                findings.append(_finding(
                    section,
                    f"{SECTION_LABELS[section]} opens with the teacher talking "
                    f"(\"{text[:50]}\") — every section must open with what students do",
                    check="pedagogy"))
            # Level 1 is grades 1-3: "very short sentences (about 5-8 words)".
            # The headline is the part read aloud, so it is the part that matters.
            if level == 1 and len(text.split()) > 16:
                findings.append(_finding(
                    section,
                    f"{SECTION_LABELS[section]} bullet {i} headline is "
                    f"{len(text.split())} words — too long to read aloud to this grade",
                    severity="advisory", check="grade-fit"))

    whole = " ".join(section_text(material, s) for s in SECTION_ORDER) + " " + (
        material.get("objective") or "")
    hits = sorted({w for w in BANNED_WORDS
                   if re.search(rf"\b{w}\w*\b", whole, re.I)})
    if hits:
        findings.append(_finding(
            None, f"uses forbidden assessment word(s): {', '.join(hits)} — a prep sheet "
                  f"frames everything as doing, never as being measured",
            severity="advisory", check="pedagogy"))

    challenge = material.get("challenge") or {}
    written = (challenge.get("activity") or "").strip() if isinstance(challenge, dict) else ""
    # Compared through the shared normaliser, not by string equality. A model
    # appending the category — "Object View Sketch (drawing)" — is not a
    # substituted activity, and blocking on it sent every sheet in the batch back
    # for repair over a parenthetical. What this still catches is the real risk:
    # an unrelated activity replacing the one the Pedagogy Library selected.
    #
    # Silent where the book prints its own task on these pages. There the library
    # template is the STAGING, not the Challenge, and the sheet is supposed to
    # name the Challenge for what the book asks the children to do — so the
    # mismatch this check exists to catch is the correct answer, and left
    # unguarded it blocks every sheet that follows the book. What the book's task
    # actually got run is checked properly, and blockingly, by
    # check_book_fidelity.
    if activity_name and written and not book_task and not same_activity(written, activity_name):
        findings.append(_finding(
            "challenge",
            f"Challenge names activity '{written}' but '{activity_name}' was selected "
            f"for this topic",
            check="pedagogy"))

    currencies = {c for c in ("₹", "$", "€", "£") if c in whole}
    if len(currencies) > 1:
        findings.append(_finding(
            None, f"mixes currencies ({', '.join(sorted(currencies))}) — use ₹ only",
            severity="advisory", check="pedagogy"))

    timings = material.get("timings") or {}
    numeric = [v for v in timings.values() if isinstance(v, (int, float))]
    # The first topic legitimately has no Refresher — `_normalise_material`
    # strips it, and OPTIONAL_WHEN_FIRST is why `check_structure` does not
    # complain about the missing section either. This check did not know that,
    # so every chapter's T1 raised "timings cover 5 of 6" for a section the
    # pipeline had deliberately removed: a false advisory on every run, and one
    # that trains people to skim the advisory list.
    expected = len(SECTION_ORDER) - (len(OPTIONAL_WHEN_FIRST) if is_first else 0)
    if timings and len(numeric) < expected:
        findings.append(_finding(
            None, f"timings cover {len(numeric)} of {expected} sections — "
                  f"all six sections run every period",
            severity="advisory", check="structure"))
    return findings


def check_timing_plan(material: dict, plan: dict, duration: int) -> list[dict]:
    """Do the sheet's minutes match what was planned for this period?

    Worth checking separately from "are all six present", because the failure is
    specific and reaches the classroom: an 8-minute Challenge written into a
    3-minute slot means the teacher runs out of time on the one section the
    students were promised. Observed on the first real run — the planner allotted
    Challenge 6 minutes and the sheet gave it 3, with the total still summing to
    30, so every other check passed.

    Advisory, not blocking: the sheet is still teachable, and the teacher can see
    the numbers. Recording it is what lets the feedback loop connect "Challenge
    rated no" to "Challenge was consistently under-timed".
    """
    findings: list[dict] = []
    written = material.get("timings") or {}
    planned = (plan or {}).get("minutes") or {}
    if not written:
        return findings

    # Flex time (Explore's hard 5-minute ceiling, see planning.py's
    # EXPLORE_CEILING) is deliberately carved OUT of the six sections and
    # handed to the teacher instead — the six sections' own timings are
    # expected to sum to `duration` minus that, not to `duration` itself.
    flex_minutes = (plan or {}).get("flexMinutes") or 0
    expected_duration = (duration or 0) - flex_minutes

    total = sum(v for v in written.values() if isinstance(v, (int, float)))
    if expected_duration and abs(total - expected_duration) > max(2, expected_duration * 0.15):
        findings.append(_finding(
            None, f"timings sum to {total} minutes for a {expected_duration}-minute "
                  f"classroom period ({duration} min period, {flex_minutes} min flex)",
            severity="advisory", check="timing"))

    drifted = []
    for section in SECTION_ORDER:
        want, got = planned.get(section), written.get(section)
        if not isinstance(want, (int, float)) or not isinstance(got, (int, float)):
            continue
        # Half or double the planned time is a different lesson for that section;
        # a minute either way is rounding.
        if got < want / 2 or got > want * 2:
            drifted.append(f"{SECTION_LABELS[section]} {got}min vs {want} planned")
    if drifted:
        findings.append(_finding(
            None, "section timings differ sharply from the plan: " + "; ".join(drifted),
            severity="advisory", check="timing"))
    return findings


def check_vocabulary_order(material: dict, index: int, ledger: dict[str, int]) -> list[dict]:
    """A term used before the topic that introduces it.

    Advisory: the ledger comes from the planner's own judgement about what counts
    as new, and a word can legitimately appear in passing before it is taught.
    Worth flagging because it is invisible in a single sheet and obvious across a
    chapter — which is the only place it can be caught.
    """
    whole = " ".join(section_text(material, s) for s in SECTION_ORDER).lower()
    early = sorted(
        term for term, introduced_at in ledger.items()
        if introduced_at > index and re.search(rf"\b{re.escape(term)}\b", whole)
    )
    if not early:
        return []
    return [_finding(
        None,
        "uses term(s) the chapter introduces later: "
        + ", ".join(f"'{t}' (T{ledger[t]})" for t in early[:4]),
        severity="advisory", check="consistency")]


# Clause comparison lives in ..clauses now, because the reasoning node has to run
# the same test on its own chain before caching it and `validation` already
# imports `reasoning`. Re-exported under the private names the checks below have
# always used, so this move changes an import and nothing else.
_root, _roots = clause_root, clause_roots


def _named_things(text: str) -> set[str]:
    """The proper nouns a piece of text names — Rama, Sita, Hyderabad.

    A capitalised word that never appears in lower case anywhere in the same text.
    That second half is what does the work: "This" opens a sentence and "this"
    appears ten lines later, so it is a word rather than a name, while "Rama" is
    capitalised every time it is written.

    SENTENCE POSITION IS THE WRONG SIGNAL, and it was the first rule tried here.
    Skipping the first word of each sentence to avoid capitalised openers throws
    away exactly the occurrences that matter: a textbook introduces its example by
    name at the start of a sentence — "Rama lives with his mother and father" — and
    a transfer task that re-uses it opens the same way. The rule was blind to the
    one case it was written for.

    Still crude, and it still catches "Telugu" and "India" along with the names of
    children. Which is why its one caller requires the same name on both sides of a
    comparison AND in the printed excerpt: a case built on India, taught with
    India, printed on the page as India is the book's own case whatever kind of
    noun India is.
    """
    words = re.findall(r"[A-Za-z][a-z'’-]{2,}", text or "")
    # Possessives normalised away before anything is compared, and this is not
    # a nicety: a textbook writes "Rama lives with his mother" and a transfer
    # task built on that case writes "Rama’s family", so the two sides shared
    # no token at all and the check was silent on the one shape it exists to
    # catch. `clauses.root` strips them first for the same reason.
    def norm(word: str) -> str:
        word = word.lower().replace("’s", "").replace("'s", "")
        return word.strip("'’-")

    lowered = {norm(w) for w in words if w[0].islower()}
    return {norm(w) for w in words
            if w[0].isupper() and len(norm(w)) > 2 and norm(w) not in lowered
            and norm(w) not in _CLAUSE_STOPWORDS}


_mentions, _polarity = clause_mentions, clause_polarity
_CLAUSE_STOPWORDS, _ABSOLUTES = CLAUSE_STOPWORDS, ABSOLUTES


def check_experience_alignment(material: dict, plan: dict,
                               excerpt: str = "") -> list[dict]:
    """Did the sheet stage the experience that was planned for it?

    Two things, and only two, because they are the two a rule can see: the object
    the plan chose has to actually appear in the hands-on sections, and what the
    child was supposed to notice has to be somewhere in the sheet. Everything else
    about "did this deliver the experience" is a judgement, and the LLM reviewer
    already has it.

    Advisory. The plan is one reading of how to teach the topic and a sheet may
    reach the same understanding by another route — worth a look, not a rewrite.

    `excerpt` is this topic's printed pages, and only the transfer check reads it.
    Absent — a chapter whose pages yielded no text — that check is skipped rather
    than approximated: it asks whether a case came off the book, and with no book
    in hand the honest answer is that nobody knows.
    """
    if not plan or not plan.get("derived"):
        return []
    findings: list[dict] = []

    anchor = (plan.get("anchor") or "").strip()
    if anchor:
        hands_on = " ".join(section_text(material, s)
                            for s in ("realLife", "challenge", "explore"))
        if hands_on.strip() and not _mentions(anchor, hands_on):
            findings.append(_finding(
                "realLife",
                f"the experience plan chose '{plan['anchor']}' because it makes this "
                f"topic's thinking happen, and no hands-on section uses it",
                severity="advisory", check="experience"))

    observation = plan.get("observation") or ""
    if observation:
        whole = " ".join(section_text(material, s) for s in SECTION_ORDER)
        if len(_roots(observation) & _roots(whole)) < 2:
            findings.append(_finding(
                None,
                f"nothing in the sheet has the class notice what the plan needs them "
                f"to notice ({observation[:60]})",
                severity="advisory", check="experience"))

    # The gap and the transfer are checked the same way the anchor is, and for the
    # same reason: both are decisions the plan made about what this period has to
    # contain, and a sheet is free to quietly not contain them. Without this they
    # would be two more fields the model is told about and nobody counts.
    #
    # Scored on content-word overlap rather than presence, because unlike an
    # anchor neither is an object with a name — "one contrasting pair of families
    # of the same size" is a thing to stage, and the sheet will stage it in its
    # own words.
    closer = plan.get("gapCloser") or ""
    if closer:
        staged = " ".join(section_text(material, s) for s in ("concept", "challenge"))
        if len(_roots(closer) & _roots(staged)) < 2:
            findings.append(_finding(
                None,
                "the plan found the book supplies no example for "
                f"[{(plan.get('gap') or '')[:50]}] and named one to stage — the sheet "
                "teaches the page as it stands instead",
                severity="advisory", check="experience"))

    transfer = plan.get("transferTask") or ""
    if transfer:
        level_set = section_text(material, "levelSet")
        if len(_roots(transfer) & _roots(level_set)) < 2:
            findings.append(_finding(
                "levelSet",
                "Level Set never puts the unshown case to the class, so nothing here "
                "separates a child who has the idea from one who remembers the "
                f"example ({transfer[:50]})",
                severity="advisory", check="experience"))
        else:
            # AND, having established the transfer reached Level Set, whether what
            # reached it is a transfer at all.
            #
            # The plan is asked for a case the class has NOT been shown, and the
            # cheapest way to write one that passes every other check here is to
            # re-ask the sheet's own example with the question reworded. Rama's
            # family taught in Concept and Rama's family asked in Level Set is a
            # comprehension question in a transfer question's clothes, and it is
            # the single measurement this pipeline has that the period taught the
            # idea rather than the page — the learner gate now grades against it.
            #
            # Proper nouns rather than content words, because content words are
            # shared by construction: a transfer task about family structure and a
            # lesson about family structure both say "family", and a check on that
            # overlap would flag every correct transfer in the chapter. A NAME is
            # different. The book introduced it, the sheet taught with it, and a
            # case built on it is that case.
            taught = " ".join(section_text(material, s)
                              for s in ("concept", "realLife", "challenge"))
            # Three-way, and each side removes a different kind of false positive.
            # The transfer task and the taught sections must share the name, or the
            # sheet did not teach with it. The excerpt must contain it too, or it is
            # a name the SHEET invented — and a name the sheet invented for its own
            # worked example is a legitimate thing to build a transfer case on, as
            # long as the case itself is new.
            shared = (_named_things(transfer) & _named_things(taught)
                      & _named_things(excerpt)) if excerpt else set()
            if shared:
                findings.append(_finding(
                    "levelSet",
                    "the transfer task is set on the book's own case, the one this "
                    f"sheet taught with ({', '.join(sorted(shared)[:3])}) — a child who "
                    "remembers the lesson answers it without having the idea, and the "
                    "learner gate now grades transfer against this task",
                    severity="advisory", check="experience"))
    return findings


def check_cognitive_alignment(material: dict, plan: dict) -> list[dict]:
    """Does the material walk the thinking path, or skip to the end?

    Scored on how much of the trajectory shows up anywhere in the sheet, not on
    the order it shows up in. Order is what the plan is FOR, but a sheet's
    sections are not in trajectory order — a Concept section legitimately states
    where the class is going before the Challenge takes them there — so scoring
    order would punish good sheets for the shape of the contract.

    What this does catch is the real failure: a sheet that names the destination
    and skips the journey, where four of the six steps appear nowhere at all.
    """
    steps = (plan or {}).get("trajectory") or []
    if not plan.get("derived") or len(steps) < 4:
        return []
    whole = _roots(" ".join(section_text(material, s) for s in SECTION_ORDER))
    missing = [step for step in steps if not (_roots(step) & whole)]
    if len(missing) * 2 <= len(steps):
        return []
    return [_finding(
        None,
        f"{len(missing)} of {len(steps)} steps in the planned thinking path appear "
        f"nowhere in the sheet — missing: " + "; ".join(missing[:3])
        + ". The class is being told the answer rather than walked to it.",
        severity="advisory", check="cognitive")]


def check_anchor_use(material: dict, anchors: list[str], contexts: list[str]) -> list[dict]:
    """Does Real Life use something this classroom actually has?

    Two allowed sources, and they are the two the section policy already names:
    the textbook's own contexts, and the chapter's anchor pool — which was chosen
    once, for a room at this resource level. A Real Life section using neither is
    reaching for something invented, and "bring a set of coloured blocks" is a
    lesson that does not run in the classroom it was written for.

    Advisory, because a section can legitimately use a common object nobody
    thought to list, and because the fix is a rewrite of one section rather than
    of the sheet.
    """
    if not anchors:
        return []
    text = section_text(material, "realLife").lower()
    if not text.strip():
        return []
    used = [a for a in anchors if a.lower() in text]
    if used:
        return []
    if any(c.lower() in text for c in contexts if isinstance(c, str) and len(c) > 3):
        return []
    return [_finding(
        "realLife",
        "Real Life uses neither a textbook context nor any of the objects this "
        f"thread was planned around ({', '.join(anchors[:6])}…) — check that what "
        "it asks for is in the room",
        severity="advisory", check="grounding")]


# ── Batch-level checks ───────────────────────────────────────────────────────

def check_knowledge_chain(reasoning: dict, ordered: list[int],
                          topics_by_index: dict) -> list[dict]:
    """Does the understanding actually chain from each topic to the next?

    This is the knowledge seam, and it is checked HERE rather than against the
    generated prose for a reason worth keeping. `bridgesTo` on topic N and
    `assumes` on topic N+1 were written together, in one response, describing the
    same handover from its two sides — so they are supposed to share their nouns,
    exactly as a Refresher is supposed to share nouns with the previous Explore.
    Asked of the sheets instead, the same question is unanswerable: the overlap
    between "a view produces a visible outline" and "identify and trace 2D shapes"
    is near zero, and scoring that arithmetically would condemn every correct
    transition in the chapter.

    Advisory throughout, and that is deliberate. A break here is a defect in the
    reasoning or the plan, not in the sheet — sending the sheet back for repair
    would spend a regeneration on prose that was written correctly against a
    broken chain, and the repaired version would break in the same place.
    """
    chain = (reasoning or {}).get("chain") or {}
    if not chain or len(ordered) < 2:
        return []

    findings: list[dict] = []
    missing = [i for i in ordered if not (chain.get(i) or {}).get("gained")]
    if missing:
        findings.append(_finding(
            None,
            f"{len(missing)} topic(s) have no stated understanding to leave behind: "
            + ", ".join(f"T{i}" for i in missing[:8]) + ("…" if len(missing) > 8 else ""),
            severity="advisory", check="knowledge"))

    threads = strands(reasoning, ordered)
    if len(threads) > 1:
        # Reported as a fact about the chapter, not as a defect. A printed chapter
        # that binds two threads is the book's doing, and a teacher who knows where
        # the seam falls can plan around it. What WOULD be a defect is a bridge
        # written across it, which the loop below catches separately.
        findings.append(_finding(
            None,
            f"this chapter is {len(threads)} separate threads, not one progression: "
            + "; ".join(f"{t['strand']} (T{t['topics'][0]}–T{t['topics'][-1]})"
                        for t in threads)
            + " — the knowledge chain is checked within each, not across them",
            severity="advisory", check="knowledge"))
    boundaries = {t["topics"][-1] for t in threads[:-1]}

    # Does each topic actually TEACH something?
    #
    # The failure this catches is a chain that satisfies every other check in this
    # function while describing a chapter in which nothing is ever learned. On the
    # EVS family chapter nine of twelve topics came back with `gained` word-for-word
    # equal to their own `assumes`:
    #
    #     T5  stands on: can say their family's last name
    #         leaves:    can say their family's last name
    #         becomes:   can sort families into types
    #
    # The chain had slipped by one — `gained[n]` was really `bridgesTo[n-1]`, and
    # `bridgesTo[n]` was the next topic's TITLE rewritten as a capability. Every
    # seam then agrees, because both sides of every seam were written from the same
    # side, and the whole chapter reads as a progression while promising nothing.
    #
    # This is checked before the bridge test below because it explains it: where
    # `gained` is the previous bridge, comparing it to this topic's bridge is
    # comparing two clauses a topic apart, and the three "invented bridges" that
    # test reported were the three places that accidental comparison failed.
    standing_still = []
    for index in ordered:
        entry = chain.get(index) or {}
        gained, assumes = entry.get("gained") or "", entry.get("assumes") or ""
        if not gained or not assumes:
            continue
        if _roots(gained) == _roots(assumes):
            standing_still.append(f"T{index}: [{gained[:52]}]")
    if standing_still:
        findings.append(_finding(
            None,
            f"{len(standing_still)} topic(s) leave exactly what they started with — "
            "the chain names no gain for them, so every period after the first is "
            "standing on ground the chapter never claims to have built: "
            + "; ".join(standing_still[:3]),
            severity="advisory", check="knowledge"))

    # Does each topic's bridge follow from its OWN gain?
    #
    # This is the check the first real chapter needed and did not have. Where a
    # chapter changes subject, a model asked for an unbroken chain will write a
    # bridge that connects to the next topic while having nothing to do with what
    # this topic actually taught — "counting shapes" leaving toward "comparing
    # numbers". Comparing bridge to the NEXT topic's `assumes` cannot see it: those
    # two agree perfectly, because both were written from the next topic's side.
    # Only the gain contradicts it.
    invented: list[str] = []
    for index in ordered:
        entry = chain.get(index) or {}
        gained, bridges = entry.get("gained") or "", entry.get("bridgesTo") or ""
        if not gained or not bridges:
            continue
        if _roots(gained) & _roots(bridges):
            continue
        invented.append(f"T{index} teaches \"{gained[:55]}\" but claims to lead to "
                        f"\"{bridges[:55]}\"")
    if invented:
        findings.append(_finding(
            None,
            f"{len(invented)} topic(s) bridge to something their own content does not "
            "reach — the link was written to keep the chain unbroken rather than "
            "because it is there: " + "; ".join(invented[:3]),
            severity="advisory", check="knowledge"))

    breaks: list[str] = []
    for previous, current in zip(ordered, ordered[1:]):
        if previous in boundaries:
            # A declared thread boundary. The knowledge does not carry across it and
            # is not supposed to — the experience seam still does, and that is
            # check_continuity's business, not this one's.
            continue
        bridges = (chain.get(previous) or {}).get("bridgesTo") or ""
        assumes = (chain.get(current) or {}).get("assumes") or ""
        if not bridges or not assumes:
            continue
        if _roots(bridges) & _roots(assumes):
            continue
        breaks.append(
            f"T{previous} leaves \"{bridges[:60]}\" but T{current} starts from "
            f"\"{assumes[:60]}\"")
    if breaks:
        findings.append(_finding(
            None,
            f"the knowledge chain breaks at {len(breaks)} seam(s) within a thread — "
            "the next topic does not stand on what the previous one leaves: "
            + "; ".join(breaks[:3]),
            severity="advisory", check="knowledge"))

    # The first topic of every thread legitimately stands on nothing in this
    # chapter, so those are excluded before the count means anything.
    thread_starts = {t["topics"][0] for t in threads}
    orphaned = [i for i in ordered[1:]
                if i not in thread_starts
                and not (chain.get(i) or {}).get("assumes")
                and topics_by_index.get(i)]
    if len(orphaned) > max(1, len(ordered) // 4):
        findings.append(_finding(
            None,
            f"{len(orphaned)} of {len(ordered)} topics declare nothing they build on — "
            "the chapter is a list of topics rather than a progression",
            severity="advisory", check="knowledge"))
    return findings


def check_misconception_coverage(materials: dict, reasoning: dict) -> list[dict]:
    """Do the Level Sets surface the misconceptions this chapter is known for?

    Reported once for the batch rather than per sheet. Per sheet it would be a
    stemmed-word comparison between a derived clause and three bullets — right
    often enough to be useful in aggregate, wrong often enough that forty of them
    would be noise, and a validator that always complains is one people stop
    reading. As a single line about the chapter it says something real: the
    misconceptions were named and the assessments went elsewhere.
    """
    tagged = [m for m in (reasoning or {}).get("misconceptions") or [] if m.get("topics")]
    if not tagged or not materials:
        return []

    expected = 0
    probed = 0
    for entry in tagged:
        belief_roots = _roots(entry["belief"])
        if not belief_roots:
            continue
        for index in entry["topics"]:
            material = materials.get(index)
            if material is None:
                continue
            expected += 1
            probe = _roots(section_text(material, "levelSet"))
            if len(belief_roots & probe) >= 2:
                probed += 1

    if expected < 3 or probed / expected >= 0.4:
        return []
    return [_finding(
        None,
        f"only {probed} of {expected} topic(s) with a known misconception appear to "
        "put it in front of the class in Level Set — the misconceptions were "
        "identified for this chapter and the assessments do not go near them",
        severity="advisory", check="knowledge")]



def check_plan_fidelity(experience: dict, reasoning: dict) -> list[dict]:
    """Does each Experience Plan aim at the gain its topic was given?

    The plan is asked to design a route to an understanding that reasoning already
    settled. Two ways it drifts, both seen on real chapters: the `inference` comes
    back restating the topic's MISCONCEPTION rather than its gain — "counting the
    same thing again makes a new number" is the wrong belief, written where the
    right one belongs — or it quietly aims somewhere else entirely.

    Either way the period is now designed to land the wrong idea, and no amount of
    rewriting the prose fixes it, because the prose would be faithfully delivering
    the wrong target. Advisory, and batch: it is a defect in the plan.
    """
    chain = (reasoning or {}).get("chain") or {}
    if not experience or not chain:
        return []

    findings: list[dict] = []
    drifted, inverted = [], []
    for index, plan in sorted(experience.items()):
        if not (plan or {}).get("derived"):
            continue
        inference = plan.get("inference") or ""
        entry = chain.get(index) or {}
        gained = entry.get("gained") or ""
        if not inference or not gained:
            continue
        if not (_roots(inference) & _roots(gained)):
            drifted.append(f"T{index} aims at [{inference[:44]}] not [{gained[:44]}]")
        # The sharper claim, and it needs a sharper test than "shares two words".
        #
        # Counting the raw overlap accused three plans across two chapters and only
        # one deserved it: T4's inference WAS its misconception restated, while T2's
        # merely shared the word "matchbox" with it — which every sentence about
        # that topic does, including the correct ones. Saying a period is designed
        # to land the wrong idea is a strong thing to say, so it now needs most of
        # the belief present, not an object noun they were always going to share.
        # …and even "most of the belief is present" is not enough on its own.
        #
        # A period that TARGETS a misconception must talk about the same thing the
        # misconception talks about — that is what targeting one means — so a
        # correct inference shares the belief's nouns by construction. On the EVS
        # family chapter T5's plan was accused of aiming at the wrong idea while
        # being exactly right: belief "my family name is the ONLY name I have"
        # (roots family/name/only) against inference "I have a family name that is
        # part of my full name" scored 0.67 and tripped the threshold.
        #
        # What actually separates the two is not vocabulary but polarity. The
        # misconception is the absolute claim; the understanding that replaces it
        # is the same subject with the absolute removed. So the overlap now has to
        # come with the belief's closing words INTACT before this says the strong
        # thing it says. A restatement keeps them. A correction drops them.
        belief = plan.get("misconception") or ""
        belief_roots = _roots(belief)
        if belief_roots:
            covered = len(_roots(inference) & belief_roots) / len(belief_roots)
            if covered >= 0.6 and _polarity(inference) == _polarity(belief):
                inverted.append(f"T{index}: [{inference[:44]}]")

    if inverted:
        findings.append(_finding(
            None,
            f"{len(inverted)} experience plan(s) aim at the topic's MISCONCEPTION "
            "rather than its gain — the period is designed to land the wrong idea: "
            + "; ".join(inverted[:3]),
            severity="advisory", check="experience"))
    elif drifted:
        findings.append(_finding(
            None,
            f"{len(drifted)} experience plan(s) aim somewhere other than the "
            "understanding the topic was given: " + "; ".join(drifted[:3]),
            severity="advisory", check="experience"))
    return findings


def check_gap_provenance(experience: dict, reasoning: dict) -> list[dict]:
    """Is the shortfall each period closes one the page was actually audited for?

    The mastery pass reads the printed excerpt and returns up to three audited
    shortfalls per topic. The experience plan is asked to CHOOSE one of them, not
    to think of its own — the audit is made where the excerpt is, and a node
    holding only a trajectory and a concept list answers "what does the book not
    supply" from the only thing in front of it, which is how the gap layer behaved
    before the audit existed.

    So this asks the one question that makes that split checkable: when the audit
    found something and the plan closed something else, the plan invented a
    deficiency. That matters beyond tidiness, because generation is handed the
    plan's gap and not the audit's — an invented gap is real work put in front of
    a teacher at 7am to close a hole that was never open, in a period that is
    thirty minutes long either way.

    Advisory, and batch. Two reasons it is not blocking: a plan may legitimately
    word the same shortfall differently enough to share no roots with it, and a
    gap that is genuinely real but unaudited is still a gap. What this says is
    "these were not chosen from the list", which is worth a look at the audit as
    much as at the plans.

    Silent where the audit found nothing — the prompt explicitly hands those topics
    back to the plan's own judgement, so an unaudited gap is the correct answer
    there rather than a defect.
    """
    chain = (reasoning or {}).get("chain") or {}
    mastery = (reasoning or {}).get("mastery") or {}
    if not experience or not mastery:
        return []

    invented, untargeted = [], []
    for index, plan in sorted(experience.items()):
        if not (plan or {}).get("derived"):
            continue
        audit = mastery.get(index) or mastery.get(str(index)) or {}
        gap = plan.get("gap") or ""
        audited = [item.get("missing") or "" for item in (audit.get("missing") or [])]

        if gap and audited and not any(_roots(gap) & _roots(entry) for entry in audited):
            invented.append(f"T{index} closes [{gap[:44]}]")
        # The other direction, and the more expensive one: the audit found the
        # page short of something and the period closes nothing at all. A topic
        # whose target the page does not reach, taught as the page stands, is the
        # failure this whole layer was added to catch.
        if audited and not gap and chain.get(index):
            untargeted.append(f"T{index} leaves [{audited[0][:44]}] unclosed")

    findings: list[dict] = []
    if invented:
        findings.append(_finding(
            None,
            f"{len(invented)} experience plan(s) close a shortfall the page was not "
            "audited for, so the period adds work for a hole nobody found: "
            + "; ".join(invented[:3]),
            severity="advisory", check="experience"))
    if untargeted:
        findings.append(_finding(
            None,
            f"{len(untargeted)} topic(s) were audited as missing something their "
            "mastery target needs and close none of it — the period teaches the page "
            "as it stands: " + "; ".join(untargeted[:3]),
            severity="advisory", check="experience"))
    return findings


def check_cognitive_band(materials: dict, reasoning: dict, grade,
                         experience: dict = None) -> list[dict]:
    """Is this chapter asking for thinking the age cannot do yet?

    Run against the OBJECTIVES only — the chain clauses and each sheet's
    `objective` — and never against the prose. Those lines are short, declarative
    and about the child, so a verb found in one means what it says. The same verb
    in a `detail` field usually describes the teacher ("the teacher explains"),
    which is not a claim about the child at all, and checking there would flag
    well-written bullets by the dozen.

    The trajectory is checked here too, and that is the whole relationship between
    this module and `experience.py`: the path a topic takes is DERIVED per concept,
    because no table indexed by grade could produce it — and the band is what that
    derivation is bounded by. Guardrail, not source.

    Advisory, and reported once for the batch. A sheet pitched above its band is
    not repairable by rewriting its prose — the objective it was written against
    is the thing that is wrong — and a per-sheet finding would say so six times.
    """
    if not band(grade):
        return []

    flagged: list[str] = []
    for index, entry in sorted(((reasoning or {}).get("chain") or {}).items()):
        gained = (entry or {}).get("gained") or ""
        verbs = over_pitched(gained, grade)
        if verbs:
            flagged.append(f"T{index} aims at [{gained[:46]}] via {verbs[0]}")
    for index in sorted(materials or {}):
        objective = (materials[index] or {}).get("objective") or ""
        verbs = over_pitched(objective, grade)
        if verbs:
            flagged.append(f"T{index} objective [{objective[:46]}] via {verbs[0]}")
    for index, plan in sorted((experience or {}).items()):
        for step in (plan or {}).get("trajectory") or []:
            verbs = over_pitched(step, grade)
            if verbs:
                flagged.append(f"T{index} thinking step [{step[:46]}] via {verbs[0]}")
                break
        for verbs in [over_pitched((plan or {}).get("conceptualJump") or "", grade)]:
            if verbs:
                flagged.append(f"T{index} hardest move needs {verbs[0]}")

    if not flagged:
        return []
    label = band(grade).get("label", "this grade")
    return [_finding(
        None,
        f"{len(flagged)} objective(s) ask for thinking above {label}: "
        + "; ".join(flagged[:3])
        + " — the wording may be simple enough while the act is not",
        severity="advisory", check="grade-fit")]


def check_batch_consistency(materials: dict, topics: list[dict]) -> list[dict]:
    """What only shows up when you look at all forty sheets at once."""
    findings: list[dict] = []
    if not materials:
        return findings

    present = set(materials)
    expected = {t["index"] for t in topics}
    gaps = sorted(expected - present)
    if gaps:
        findings.append(_finding(
            None, f"{len(gaps)} topic(s) have no material at all: "
                  + ", ".join(f"T{i}" for i in gaps[:12])
                  + ("…" if len(gaps) > 12 else ""),
            check="coverage"))

    activities = [
        ((m.get("challenge") or {}).get("activity") or "").strip()
        for m in materials.values() if isinstance(m.get("challenge"), dict)
    ]
    overused = [(name, count) for name, count in Counter(a for a in activities if a).items()
                if count > max(2, len(materials) // 8)]
    for name, count in sorted(overused, key=lambda x: -x[1]):
        findings.append(_finding(
            "challenge", f"activity '{name}' is used {count} times across "
                         f"{len(materials)} topics",
            severity="advisory", check="variety"))

    # Bullets repeated verbatim between topics: the clearest sign the batch has
    # started producing filler rather than lessons.
    seen: dict[str, int] = {}
    duplicates: list[str] = []
    for index in sorted(materials):
        for section in SECTION_ORDER:
            for bullet in bullets_of(materials[index], section) or []:
                if not isinstance(bullet, dict):
                    continue
                key = re.sub(r"[^a-z0-9 ]", "", (bullet.get("text") or "").lower()).strip()
                if len(key) < 20:
                    continue
                if key in seen and seen[key] != index:
                    duplicates.append(f"T{seen[key]}/T{index}: \"{bullet['text'][:48]}\"")
                else:
                    seen[key] = index
    if duplicates:
        findings.append(_finding(
            None, f"{len(duplicates)} bullet(s) repeated verbatim between topics — "
                  + "; ".join(duplicates[:3]),
            severity="advisory", check="variety"))

    lengths = Counter(
        len([b for b in (bullets_of(m, s) or []) if isinstance(b, dict)])
        for m in materials.values() for s in SECTION_ORDER
    )
    off_spec = sum(count for length, count in lengths.items() if length != BULLETS_PER_SECTION)
    total_sections = len(materials) * len(SECTION_ORDER)
    if total_sections and off_spec / total_sections > 0.25:
        findings.append(_finding(
            None, f"{off_spec} of {total_sections} sections do not have exactly "
                  f"{BULLETS_PER_SECTION} bullets — the batch is structurally inconsistent",
            check="structure"))
    return findings


_CONSISTENCY_PROMPT = """You are reviewing a chapter's worth of lesson prep material as a whole. The
mechanical checks have already run — sections, bullet counts, page citations,
whether each Refresher picks up the previous Explore. Do NOT repeat those.

Judge only what a rule cannot: whether these {count} sheets read as ONE chapter
taught by one teacher, and whether any single period is pitched wrongly for the
grade.

CHAPTER: {chapter_title} | GRADE: {grade} | SUBJECT: {subject}
ARC: {arc}

THE SHEETS, in teaching order:
{digest}

Look for exactly these, and report nothing else:
- A topic that is out of place — it needs something a later topic teaches, or it
  repeats what an earlier one already did.
- Difficulty that jumps or sags: three easy periods then a wall.
- A section that has gone formulaic across the chapter — the same move dressed up
  {count} times.
- A period pitched wrong for grade {grade}: too abstract, or babyish.
- Tone drift: sheets that read as though a different person wrote them.

Say nothing when there is nothing to say. An empty findings list is a valid and
common answer for a well-generated chapter, and inventing a concern to look
thorough costs a teacher a regeneration they did not need.

Return ONLY valid JSON, no markdown fences:
{{
  "verdict": "coherent | uneven | incoherent",
  "chapterNote": "2-3 sentences a reviewer would want to read first",
  "findings": [
    {{"index": 7, "section": "concept | challenge | ... | null", "severity": "blocking | advisory", "message": "one specific sentence naming what is wrong"}}
  ]
}}
"""


def _digest(materials: dict, topics: list[dict], plans: dict, reasoning: dict) -> str:
    by_index = {t["index"]: t for t in topics}
    chain = (reasoning or {}).get("chain") or {}
    lines: list[str] = []
    for index in sorted(materials):
        material = materials[index]
        spec = by_index.get(index, {})
        plan = plans.get(index) or {}
        challenge = material.get("challenge") or {}
        # The intended understanding is in the digest because the reviewer's first
        # instruction is to find a topic that is out of place, and "needs something
        # a later topic teaches" is a question about this chain, not about prose.
        gained = (chain.get(index) or {}).get("gained")
        lines.append(
            f"T{index}. {spec.get('topic', '?')} — objective: "
            f"{material.get('objective') or '(none)'}"
            + (f"\n    should leave them able to: {gained}" if gained else "")
            + f"\n    difficulty step: {plan.get('difficultyStep', '?')}"
            f" | heaviest: {(plan.get('emphasis') or {}).get('heaviest') or '?'}"
            f"\n    concept: {section_text(material, 'concept')[:200]}"
            f"\n    challenge ({challenge.get('activity', '?')}): "
            f"{section_text(material, 'challenge')[:160]}"
            f"\n    explore: {section_text(material, 'explore')[:160]}"
        )
    return "\n".join(lines)


async def validation_node(state: dict) -> dict:
    topics = state.get("topics") or []
    materials = state.get("materials") or {}
    plans = state.get("plans") or {}
    selections = state.get("selections") or {}
    config = state.get("config") or {}
    grade = state.get("grade")

    by_index = {t["index"]: t for t in topics}
    ordered = [t["index"] for t in topics]
    last_index = ordered[-1] if ordered else None
    ledger = vocabulary_ledger(plans)
    reasoning = state.get("reasoning") or {}
    # Resolved per topic below: the pools are per strand, and checking a counting
    # lesson against a pool of things to look at is how false advisories are made.

    issues: dict[int, list[dict]] = {}
    for index in ordered:
        material = materials.get(index)
        if material is None:
            issues[index] = [_finding(
                None, "no material was generated for this topic", check="coverage")]
            continue

        spec = by_index[index]
        is_first = index == ordered[0]
        activity = ((selections.get(index) or {}).get("activity") or {}).get("name")

        findings: list[dict] = []
        findings += check_structure(material, is_first)
        findings += check_continuity(
            material, materials.get(index - 1),
            by_index.get(index - 1, {}).get("topic", ""), is_first, spec)
        findings += check_handoff(material, plans.get(index) or {}, index == last_index)
        findings += check_explore_voice(material)
        findings += check_concept_grounding(
            material, spec.get("excerpt") or "", state.get("chapter_markdown") or "")
        findings += check_competency_alignment(material, spec)
        # Straight after grounding, and deliberately adjacent to it: the two
        # halves of "did this teach the book". Grounding catches what the sheet
        # added, this catches what it dropped or swapped out.
        findings += check_book_fidelity(material, spec)
        findings += check_pedagogy_rules(
            material, activity, grade, book_activity(spec.get("moves") or []),
            is_first=is_first)
        findings += check_timing_plan(
            material, plans.get(index) or {},
            int((state.get("teacher_settings") or {}).get("duration", 30)))
        findings += check_vocabulary_order(material, index, ledger)
        # The plan's chosen object supersedes the strand pool: checking both would
        # report the same sheet twice for the same reason.
        plan_for_topic = (state.get("experience") or {}).get(index) or {}
        if plan_for_topic.get("derived") and plan_for_topic.get("anchor"):
            findings += check_experience_alignment(material, plan_for_topic,
                                                   spec.get("excerpt") or "")
        else:
            findings += check_anchor_use(
                material,
                anchors_for(reasoning, (spec.get("reasoning") or {}).get("strand")),
                (spec.get("knowledge") or {}).get("contexts") or [])
        findings += check_cognitive_alignment(material, plan_for_topic)

        # The pilot's own shape validator, for the fields this one does not
        # duplicate — visuals in particular, whose failures are silent and
        # surface in front of a class.
        if material.get("visuals") is not None:
            findings += [_finding("visuals", msg, severity="advisory", check="visuals")
                         for msg in validate_visuals(material.get("visuals"))]

        issues[index] = findings

    batch_issues = check_batch_consistency(materials, topics)
    # The knowledge chain is judged on the plan's own two ends, so it is worth
    # reporting even for a chapter whose generation half failed — a broken chain
    # is the one finding here that a rerun would reproduce exactly.
    batch_issues += check_knowledge_chain(reasoning, ordered, by_index)
    batch_issues += check_misconception_coverage(materials, reasoning)
    batch_issues += check_plan_fidelity(state.get("experience") or {}, reasoning)
    batch_issues += check_gap_provenance(state.get("experience") or {}, reasoning)
    batch_issues += check_cognitive_band(materials, reasoning, grade,
                                         state.get("experience") or {})

    # The LLM pass runs only when there is a chapter to judge and the mechanical
    # checks have not already condemned it — a batch where a third of the sheets
    # are missing does not need an opinion on its tone.
    blocking_now = sum(1 for f in issues.values()
                       for i in f if i["severity"] == "blocking")
    if materials and blocking_now <= max(3, len(materials) // 3):
        try:
            data = await call_json(
                _CONSISTENCY_PROMPT.format(
                    count=len(materials),
                    chapter_title=state.get("chapter_title") or "(untitled)",
                    grade=grade, subject=state.get("subject"),
                    arc=state.get("chapter_arc") or "(none recorded)",
                    digest=_digest(materials, topics, plans, reasoning),
                ),
                label="consistency", required=("findings",),
                temperature=0.3, max_tokens=4000,
            )
            verdict = (data.get("verdict") or "").strip()
            note = (data.get("chapterNote") or "").strip()
            if note:
                batch_issues.append(_finding(
                    None, f"reviewer ({verdict or 'no verdict'}): {note}",
                    severity="advisory", check="consistency"))
            for entry in data.get("findings") or []:
                if not isinstance(entry, dict) or not (entry.get("message") or "").strip():
                    continue
                try:
                    index = int(entry.get("index"))
                except (TypeError, ValueError):
                    batch_issues.append(_finding(
                        entry.get("section"), entry["message"].strip(),
                        severity="advisory", check="consistency"))
                    continue
                if index not in issues:
                    continue
                # The reviewer's severity is accepted but capped: a model reading
                # a digest cannot see enough to justify spending a regeneration,
                # and this pass is about judgement, not defects.
                issues[index].append(_finding(
                    # Normalised for the same reason the optimizer's keys are: the
                    # digest this reviewer reads names sections by their labels, so
                    # that is what it answers with.
                    canonical_section(entry.get("section")),
                    entry["message"].strip(), severity="advisory", check="consistency"))
        except Exception as exc:
            batch_issues.append(_finding(
                None, f"the chapter-level consistency review did not complete: {exc}",
                severity="advisory", check="consistency"))

    blocking = {i: [f for f in findings if f["severity"] == "blocking"]
                for i, findings in issues.items()}
    repair_targets = sorted(i for i, findings in blocking.items() if findings)

    total_blocking = sum(len(f) for f in blocking.values())
    total_advisory = sum(len(f) for f in issues.values()) - total_blocking
    ratio = len(repair_targets) / max(1, len(ordered))
    abort = ratio > float(config.get("repair_abort_ratio", 0.6))

    # Keyed by the revision each sheet was at WHEN JUDGED, so a later pass over a
    # repaired sheet lands beside its predecessor rather than on top of it. The
    # reducer deduplicates on that revision, so a re-validation of unchanged
    # material cannot inflate the record.
    validation_history = {
        i: [{"revision": int(((materials.get(i) or {}).get("_meta") or {})
                             .get("revision", 0)),
             "blocking": len(blocking.get(i) or []),
             "advisory": len(findings) - len(blocking.get(i) or [])}]
        for i, findings in issues.items() if i in materials
    }

    return {
        "issues": issues,
        "batch_issues": batch_issues,
        "validation_history": validation_history,
        # Cleared for anything this pass targets. `repair_sections` is a merging
        # map, so a stale entry from a learner diagnosis would otherwise narrow a
        # structural rewrite to the two sections that failed a different test.
        "repair_sections": {i: [] for i in ([] if abort else repair_targets)},
        # Nothing to repair when most of the batch has failed: at that point the
        # inputs or the prompt are wrong, and re-rolling topic by topic burns the
        # budget without changing the outcome. Better to hand a human the report.
        "repair_targets": [] if abort else repair_targets,
        "metrics": {
            "blocking_findings": total_blocking,
            "advisory_findings": total_advisory,
            "topics_needing_repair": len(repair_targets),
            "repair_aborted_wholesale": abort,
        },
    }
