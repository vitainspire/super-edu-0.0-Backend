"""The book's own explanatory order, inside one topic.

sequencing.py already makes the sheet follow the book BETWEEN topics: T1…T40
walk the chapter forward and never reorder it. This module is the same rule one
level down — WITHIN a topic, in what order does the book actually explain the
thing, and does the sheet explain it that way too.

The distinction that makes this worth a module: a textbook does not present a
topic as prose to be summarised, it performs a sequence of teaching moves. Some
books open on a village scene and only name the rule three paragraphs later;
some state the rule and then illustrate it. Those are different lessons, and the
six-section contract used to flatten both into the same one, because
SECTION_ORDER was a constant. Here the order is read off the page instead.

    the book's moves            what EduLearn does with each

    hook / observe        ──>   stages it — the scene is the book's, the
                                classroom framing is ours
    concept / definition  ──>   restates it faithfully: same fact, same
    worked_example              sequence, our words and our example
    activity / practice   ──>   RUNS THE BOOK'S OWN TASK. The children have
                                this printed in front of them; substituting a
                                library activity for it is the one substitution
                                a teacher notices from the back of the room.
    recall / summary      ──>   ours to write, on the book's cue

So three fidelity levels, assigned per move rather than per section:

    VERBATIM  the book's task, reused as printed
    FAITHFUL  the book's fact and the book's order, our wording
    STAGED    the book supplies the occasion, we supply the content

That is the whole design principle in one table: THE BOOK DECIDES THE ORDER OF
THE MOVES AND THE CONTENT OF THE GROUNDED ONES; WE DECIDE HOW EACH MOVE IS
PLAYED IN THE ROOM.
"""
import re
from typing import Optional

from .sections import SECTION_ORDER

# Enough of a topic to read its structure off. Deliberately smaller than
# tools.TOPIC_EXCERPT_CHARS: this call answers "in what order does this explain
# itself", which the opening pages settle, and a 40-topic chapter pays for it
# forty times.
MAX_MOVE_EXCERPT_CHARS = 4_000

# A primary-school topic that genuinely performs more than this many distinct
# moves has not been split finely enough by sequencing — the cap is a signal as
# much as a bound.
MAX_MOVES = 12

# The book's own words, when we reuse them, are quoted onto the sheet. Bounded
# so one long exercise block cannot crowd the generation prompt.
MAX_VERBATIM_CHARS = 400

VERBATIM, FAITHFUL, STAGED = "verbatim", "faithful", "staged"

# The move vocabulary. Listed in the order a book would typically perform them,
# which is ONLY a tiebreak for the fallback — the real order always comes off
# the page.
#
# `section` is the six-section slot that carries this move. It is what turns a
# move sequence into a teaching order, and it is why the mapping lives here next
# to the vocabulary rather than inside the generation prompt: a prompt that maps
# moves to sections maps them differently every run.
MOVE_TYPES: dict[str, dict] = {
    "recall": {
        "label": "Recall",
        "section": "refresher",
        "fidelity": STAGED,
        "describe": "brings back something the class already learnt, before teaching anything new",
    },
    "hook": {
        "label": "Opening scene",
        "section": "realLife",
        "fidelity": STAGED,
        "describe": "opens on a story, a scene or a question from everyday life, before any explaining",
    },
    "observe": {
        "label": "Directed observation",
        "section": "realLife",
        "fidelity": STAGED,
        "describe": "tells the reader to look at a picture, a table or an object and notice something",
    },
    "concept": {
        "label": "Explanation",
        "section": "concept",
        "fidelity": FAITHFUL,
        "describe": "explains the idea itself — the reasoning, the rule, the how or the why",
    },
    "definition": {
        "label": "Naming",
        "section": "concept",
        "fidelity": FAITHFUL,
        "describe": "names a term and says what it means",
    },
    "worked_example": {
        "label": "Worked example",
        "section": "concept",
        "fidelity": FAITHFUL,
        "describe": "works one instance through to its answer, showing the steps",
    },
    "activity": {
        "label": "The book's activity",
        "section": "challenge",
        "fidelity": VERBATIM,
        "describe": "a printed task the children DO — 'Do this', 'Try this', 'Let us find out'",
    },
    "practice": {
        "label": "The book's exercise",
        "section": "levelSet",
        "fidelity": VERBATIM,
        "describe": "exercise or practice questions printed for the children to answer",
    },
    "summary": {
        "label": "Recap",
        "section": "levelSet",
        "fidelity": STAGED,
        "describe": "gathers up what the topic taught, at its end",
    },
}

# Refresher opens every sheet and Explore closes it, whatever the book does, and
# this is the one place the book does NOT get a vote. Both belong to the chain
# BETWEEN lessons rather than to the topic: Refresher(n) is defined by
# Explore(n-1) and Explore(n) is owed to Refresher(n+1) — see
# sections.handoff_summary. A book that ends a topic on an exercise has said
# nothing about how tomorrow's period opens, because the book has no concept of
# tomorrow's period.
FIXED_FIRST = "refresher"
FIXED_LAST = "explore"
# The sections whose order the book is allowed to decide.
MOVEABLE = tuple(s for s in SECTION_ORDER if s not in (FIXED_FIRST, FIXED_LAST))


def fidelity_of(move: dict) -> str:
    return MOVE_TYPES.get(move.get("type") or "", {}).get("fidelity") or STAGED


def section_of(move: dict) -> Optional[str]:
    return MOVE_TYPES.get(move.get("type") or "", {}).get("section")


def label_of(move: dict) -> str:
    return MOVE_TYPES.get(move.get("type") or "", {}).get("label") or "Move"


def _positional_section(move: dict, moves: list[dict]) -> Optional[str]:
    """The section a move belongs to, given WHERE in the topic it falls.

    One move type needs this and the rest do not. `practice` normally means the
    exercises a topic ends on, which is a Level Set. But a book can also teach
    THROUGH questions, and Class 3 Maths pages 22-23 do exactly that: a cricket
    scoreboard, then six questions about it, then more questions, and only near
    the end a worked "Example: 62 -> 60". Every one of those questions is the
    lesson, not a check on it.

    Read naively, that page produced Level Set BEFORE Concept — a period that
    asks "which is bigger, 45 or 54?" before it has taught anything about
    comparing two-digit numbers. Nobody could teach from it.

    So: practice that comes before the topic's first explanation is part of the
    explanation, and only practice AFTER it is a check. Which is also just what
    the words mean — you cannot check an understanding the period has not
    reached yet.
    """
    kind = move.get("type")
    if kind != "practice":
        return section_of(move)
    first_teaching = next(
        (m.get("ord", 0) for m in moves or [] if m.get("type") in ("concept", "definition",
                                                                  "worked_example")),
        None)
    if first_teaching is None or move.get("ord", 0) < first_teaching:
        return "concept"
    return "levelSet"


def teaching_order(moves: list[dict]) -> list[str]:
    """The six sections, reordered into the sequence THIS topic's book performs.

    Built by walking the moves in printed order, mapping each to the section
    that carries it, and keeping first occurrences. A book that hooks before it
    explains yields realLife before concept; a book that states the rule first
    yields the canonical order back unchanged, which is why this is safe to run
    on every topic rather than only on the unusual ones.

    Sections the book never touches are NOT dropped — they keep their canonical
    position relative to whatever surrounds them. Dropping them was the first
    version of this function and it was wrong: a topic whose pages happen to
    print no activity still gets a Challenge, because a period still needs one.
    The book decides ORDER here, never PRESENCE; presence is the six-section
    contract's business and OPTIONAL_WHEN_FIRST is its only exception.
    """
    seen: list[str] = []
    for move in moves or []:
        section = _positional_section(move, moves)
        if section in MOVEABLE and section not in seen:
            seen.append(section)
    if not seen:
        return list(SECTION_ORDER)

    # Splice the untouched sections back in, each one immediately after the LAST
    # of its canonical predecessors that the book did place. Counting
    # predecessors instead of locating them was the obvious version and it drifts:
    # on a revision topic that opens on the exercises, it lands Real Life ahead of
    # the Concept it is supposed to follow, because two of Real Life's
    # predecessors were merely absent rather than early.
    ordered = list(seen)
    for section in MOVEABLE:
        if section in ordered:
            continue
        canonical_pos = MOVEABLE.index(section)
        at = 0
        for i, placed in enumerate(ordered):
            if MOVEABLE.index(placed) < canonical_pos:
                at = i + 1
        # TRIED AND REVERTED, 2026-08-25: forcing untouched sections to never
        # precede the book's first performed move — `ordered.insert(max(at, 1))`.
        #
        # The theory was sound and the measurement did not support it. Concept
        # has no canonical predecessors, so `at` is 0 for it every time, and on a
        # Class 3 maths chapter — where every topic's book opens on "take a
        # match-box and draw along the edges" — that puts the explanation before
        # the class has done anything. Keeping Challenge first looked like the
        # obvious fix.
        #
        # Measured over 3 repeats against a pinned baseline: `book_order` fell
        # 3.25 -> 2.25, the overall mean 3.34 -> 3.23, and would-use-as-is
        # 33% -> 8%. Inside the noise band (book_order carries the widest
        # interval of the ten, ±1.16 at this sample size) so it is not proof of
        # harm — but a change aimed at one criterion that moves it a full point
        # the wrong way has not earned its place.
        #
        # The likelier root cause, untested: `_positional_section` maps every
        # `activity` move to Challenge, and a textbook activity that IS the
        # teaching ("trace a matchbox to discover 2D shapes") is a Concept move
        # wearing an activity's clothes. If so the fault is in extraction, not in
        # this splice, and reordering here was treating a symptom.
        ordered.insert(at, section)
    return [FIXED_FIRST, *ordered, FIXED_LAST]


def reordered(moves: list[dict]) -> bool:
    """Whether this topic's book order differs from the canonical six.

    Worth reporting on its own: on a chapter where this is true for thirty
    topics out of forty, the canonical order is the wrong default and that is a
    finding about the design, not about the chapter.
    """
    return teaching_order(moves) != list(SECTION_ORDER)


def inversions(order: list[str], moves: list[dict]) -> list[tuple[str, str]]:
    """Section pairs the given order puts the opposite way round to the book.

    Returned as pairs rather than as a bool because the validator's finding has
    to say WHICH inversion: "the sheet explains before it hooks, the book hooks
    first" is actionable and "order mismatch" is not.
    """
    wanted = teaching_order(moves)
    positions = {section: i for i, section in enumerate(order or [])}
    found: list[tuple[str, str]] = []
    for i, first in enumerate(wanted):
        for second in wanted[i + 1:]:
            if first in positions and second in positions and positions[first] > positions[second]:
                found.append((first, second))
    return found


# The moves that make up the explanation proper, in the order this module cares
# about them: showing something and naming something are different acts, and
# which comes first is the single most consequential thing a book decides.
_SHOWS = ("hook", "observe", "worked_example")
_NAMES = ("definition", "concept")


def names_before_showing(moves: list[dict]) -> Optional[bool]:
    """Does the book give the term before it gives an instance? None if it does
    only one of the two, in which case there is no order to be faithful to."""
    first_show = next((m["ord"] for m in moves or [] if m.get("type") in _SHOWS), None)
    first_name = next((m["ord"] for m in moves or [] if m.get("type") in _NAMES), None)
    if first_show is None or first_name is None:
        return None
    return first_name < first_show


def staging_note(moves: list[dict]) -> str:
    """How the Concept section stages its bullets, for THIS book.

    The one place where following the book costs EduLearn something it values.
    The Concept rule used to end "students meet the idea in pairs and find it
    BEFORE it is named" — unconditionally, for every topic in every book. That is
    excellent teaching and it is also, for a book that states the rule and then
    illustrates it, a reordering of the book's explanation: the sheet works the
    example first and names the term last, in the opposite order to the page the
    children are reading along with.

    So the discovery staging is kept where the book already does it, and inverted
    where the book does not — and inverting it costs less than it sounds, because
    the alternative is not passive. A class that is given the word and then
    immediately goes and finds three of them is doing as much as a class that
    finds three first and is then given the word. What changes is only which end
    the word sits at, and that end is the book's to choose.
    """
    order = names_before_showing(moves)
    if order is None:
        return (
            "STAGING (the book does not settle this, so it is yours): students meet the "
            "idea in pairs and find it BEFORE it is named, and only the last bullet has "
            "the teacher confirm it aloud."
        )
    if order:
        return (
            "STAGING — THIS BOOK NAMES THE IDEA FIRST, AND SO DOES YOUR SHEET. The "
            "children are reading along with a page that gives the word and then shows "
            "it, so do not invert that into discovery: bullet 1 has the teacher give the "
            "term plainly, and bullets 2 and 3 send the class straight out to find and "
            "test instances of it in pairs. Named first is not the same as told and left "
            "sitting — the doing moves after the naming, it does not disappear."
        )
    return (
        "STAGING — THIS BOOK SHOWS BEFORE IT NAMES, which is what you would have done "
        "anyway: students meet the idea in pairs and find it BEFORE it is named, and "
        "only the last bullet has the teacher confirm the term aloud."
    )


def book_activity(moves: list[dict]) -> Optional[dict]:
    """The book's own printed activity for this topic, if it prints one.

    What activity selection has to beat before it may reach for the Pedagogy
    Library, and what the validator checks the Challenge actually ran.
    """
    return next((m for m in moves or [] if m.get("type") == "activity" and m.get("verbatim")), None)


def verbatim_moves(moves: list[dict]) -> list[dict]:
    """The moves whose content is the book's to keep — its activities and its
    exercises. These are what a teacher can see being replaced."""
    return [m for m in (moves or []) if fidelity_of(m) == VERBATIM]


def normalise_moves(raw, page_range: tuple[Optional[int], Optional[int]]) -> list[dict]:
    """Force an extraction into a usable move sequence.

    Every rule here is something a model actually returns on a real chapter: a
    type it invented, a page from a different topic, the same move listed twice
    because it spans a page break, or twenty moves for one period.
    """
    if not isinstance(raw, list):
        return []
    lo, hi = page_range
    out: list[dict] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        kind = str(entry.get("type") or "").strip().lower().replace(" ", "_")
        # 'example' -> 'worked_example', 'exercise' -> 'practice': near-misses
        # the model reaches for, mapped rather than dropped, because dropping a
        # move silently shortens the book's sequence.
        kind = {"example": "worked_example", "exercise": "practice",
                "question": "practice", "task": "activity",
                "introduction": "hook", "conclusion": "summary"}.get(kind, kind)
        if kind not in MOVE_TYPES:
            continue

        page = entry.get("page")
        try:
            page = int(str(page).strip())
        except (TypeError, ValueError):
            page = None
        if page is not None and lo is not None and hi is not None and not (lo <= page <= hi):
            # A page outside the topic's own range means the model read the
            # marker from a neighbouring slice. The move is probably real; the
            # page number is not, so only the page is discarded.
            page = None

        gist = " ".join(str(entry.get("gist") or "").split())[:220]
        if not gist:
            continue

        move = {"type": kind, "gist": gist, "page": page,
                "section": MOVE_TYPES[kind]["section"],
                "fidelity": MOVE_TYPES[kind]["fidelity"]}

        # Only the verbatim moves carry the book's text forward. Quoting the
        # book's explanation back at the model is exactly how a sheet ends up
        # transcribed rather than taught, and the excerpt is in the prompt
        # anyway.
        if MOVE_TYPES[kind]["fidelity"] == VERBATIM:
            text = " ".join(str(entry.get("verbatim") or "").split())[:MAX_VERBATIM_CHARS]
            if text:
                move["verbatim"] = text

        # The same move twice running is a page break, not a second move.
        if out and out[-1]["type"] == kind and out[-1]["gist"][:40] == gist[:40]:
            continue
        out.append(move)
        if len(out) >= MAX_MOVES:
            break

    for i, move in enumerate(out, start=1):
        move["ord"] = i
    return out


def moves_prompt_block(moves: list[dict], topic: str = "") -> str:
    """The move sequence as the generation prompt sees it.

    Says the order, says what each move is for, and says per move how much of it
    is ours — which is the part a single "follow the textbook" instruction never
    manages to convey.
    """
    if not moves:
        return (
            "THE BOOK'S TEACHING ORDER for this topic could not be read from the pages, "
            "so use the standard section order and stay especially close to the excerpt."
        )

    lines = [
        f'HOW THE BOOK ITSELF EXPLAINS {("“" + topic + "”") if topic else "THIS TOPIC"}, move by move.',
        "",
        "This is the order the children's own book performs, read off these pages. It is",
        "the order your sheet teaches in. You are not free to explain first because",
        "explaining first is tidier — if the book opens on a scene and names the rule",
        "afterwards, so does the period.",
        "",
    ]
    for move in moves:
        meta = MOVE_TYPES[move["type"]]
        where = f" (Page {move['page']})" if move.get("page") else ""
        lines.append(f"{move['ord']}. [{meta['label'].upper()}]{where} {move['gist']}")
        if move.get("verbatim"):
            lines.append(f'   THE BOOK\'S OWN WORDS: "{move["verbatim"]}"')

    order = teaching_order(moves)
    lines += [
        "",
        "WHICH MEANS YOUR SECTIONS RUN IN THIS ORDER: " + " → ".join(order),
        "",
        "HOW MUCH OF EACH MOVE IS YOURS:",
        "- VERBATIM (the book's activity, the book's exercise) — RUN THE BOOK'S OWN TASK.",
        "  The children have it printed in front of them. You may stage it, pair it, time",
        "  it, add the worked answer the book omits. You may NOT swap it for a different",
        "  activity, and where a task above carries THE BOOK'S OWN WORDS, the sheet must",
        "  set that same task.",
        "- FAITHFUL (explanation, naming, worked example) — same fact, same sequence, your",
        "  words and your example. Simplify freely; reorder never.",
        "- STAGED (opening scene, observation, recall, recap) — the book supplies the",
        "  occasion, you supply what happens in the room.",
    ]
    return "\n".join(lines)


_MOVES_PROMPT = """You are reading one topic of an Indian primary-school textbook in order to
record HOW IT TEACHES — not what it says.

TOPIC: {topic}{subtopic_note}
GRADE: {grade} | SUBJECT: {subject} | PAGES: {page_start}-{page_end}

--- BEGIN THE BOOK'S OWN PAGES ---
{excerpt}
--- END THE BOOK'S OWN PAGES ---

Break these pages into the sequence of TEACHING MOVES the book performs, in the
order it performs them. A move is one thing the book does to the reader.

The move types, and nothing outside this list:
{vocabulary}

Rules:
- IN PRINTED ORDER. Your first move is whatever the book does first on these
  pages, even if that is an activity or a picture rather than an explanation.
  Do not reorder into what would be tidier to teach.
- One move per thing the book does. A page that explains, then works an example,
  then sets a task is three moves.
- Merge nothing and invent nothing. If the book never opens with a real-life
  scene, there is no `hook` move — an absent move is a fact about this book.
- `gist` is 8-20 words saying what THIS move does, in your words.
- `page` is the page it sits on, from the `<!-- page N -->` markers. Never a
  page you did not see.
- For `activity` and `practice` ONLY, also return `verbatim`: the book's own
  wording of that task, copied exactly, up to 60 words. These are the tasks the
  children have printed in front of them, so the exact words matter. Leave
  `verbatim` out for every other type.

Return ONLY valid JSON, no markdown fences:
{{
  "moves": [
    {{"type": "hook", "gist": "opens on children sharing rotis at a village meal", "page": 12}},
    {{"type": "activity", "gist": "children fold paper strips into equal parts", "page": 13, "verbatim": "Do this: Take a strip of paper..."}}
  ]
}}
"""


_MOVES_WINDOW_PROMPT = """You are reading several consecutive topics of an Indian primary-school textbook
in order to record HOW EACH ONE TEACHES — not what it says.

GRADE: {grade} | SUBJECT: {subject}

The move types, and nothing outside this list:
{vocabulary}

{topics}

For EACH topic above, break its pages into the sequence of TEACHING MOVES the
book performs, in the order it performs them. A move is one thing the book does
to the reader.

Rules:
- IN PRINTED ORDER, per topic. The first move for a topic is whatever the book
  does first on THAT topic's pages, even if that is an activity or a picture
  rather than an explanation. Do not reorder into what would be tidier to teach.
- One move per thing the book does. A page that explains, then works an example,
  then sets a task is three moves.
- Merge nothing and invent nothing. If the book never opens with a real-life
  scene, there is no `hook` move — an absent move is a fact about this book.
- `gist` is 8-20 words saying what THIS move does, in your words.
- `page` is the page it sits on, from the `<!-- page N -->` markers inside that
  topic's own pages. Never a page you did not see, and never a page belonging to
  a different topic.
- For `activity` and `practice` ONLY, also return `verbatim`: the book's own
  wording of that task, copied exactly, up to 60 words. These are the tasks the
  children have printed in front of them, so the exact words matter. Leave
  `verbatim` out for every other type.

READ EACH TOPIC'S PAGES SEPARATELY. They are printed here together because they
are consecutive, not because they are one lesson. Two topics in this window will
often be taught in genuinely different orders — one opens on a picture and one
opens on a definition — and a window that returns the same sequence for every
topic in it has read the first one and copied it.

Return ONLY valid JSON, no markdown fences. One entry per topic listed above,
same index, same order:
{{
  "topics": [
    {{"index": {first},
      "moves": [
        {{"type": "hook", "gist": "opens on children sharing rotis at a village meal", "page": 12}},
        {{"type": "activity", "gist": "children fold paper strips into equal parts", "page": 13, "verbatim": "Do this: Take a strip of paper..."}}
      ]}}
  ]
}}
"""


def _move_vocabulary() -> str:
    return "\n".join(
        f"- `{name}` — {meta['describe']}" for name, meta in MOVE_TYPES.items())


def build_moves_window_prompt(specs: list[dict], grade: str, subject: str) -> str:
    """One prompt for several consecutive topics.

    The per-topic prompt above is unchanged and still used when the window is 1.
    This exists because the per-topic call re-sent the move vocabulary and the
    eight rules once per topic — the comment on MAX_MOVE_EXCERPT_CHARS notes that
    a 40-topic chapter pays for this forty times, and the rules are most of what
    it was paying for. The excerpts themselves are sent exactly once either way,
    so what a window saves is the preamble and 39 round trips, not the book.

    Extraction rather than judgement is what makes the window safe here. The
    learner and teacher gates are deliberately NOT batched: a model asked for a
    verdict on four sheets in one response will carry one across, and both of
    those modules' docstrings describe the run where that happened. "In what
    order does this page explain itself" has a different answer for each topic
    that is written on the page itself, and `normalise_moves` below still checks
    every returned page against the topic's own range.
    """
    blocks = []
    for spec in specs:
        subtopic = (spec.get("subtopic") or "").strip()
        blocks.append(
            f"═══ TOPIC {spec['index']}: {spec.get('topic') or ''}"
            + (f" — {subtopic}" if subtopic else "")
            + f"  (pages {spec.get('page_start')}-{spec.get('page_end')}) ═══\n"
            + "--- BEGIN THIS TOPIC'S OWN PAGES ---\n"
            + (spec.get("excerpt") or "")[:MAX_MOVE_EXCERPT_CHARS]
            + "\n--- END THIS TOPIC'S OWN PAGES ---"
        )
    return _MOVES_WINDOW_PROMPT.format(
        grade=grade, subject=subject,
        vocabulary=_move_vocabulary(),
        topics="\n\n".join(blocks),
        first=specs[0]["index"] if specs else 1,
    )


def build_moves_prompt(spec: dict, grade: str, subject: str, excerpt: str) -> str:
    vocabulary = "\n".join(
        f"- `{name}` — {meta['describe']}" for name, meta in MOVE_TYPES.items())
    subtopic = (spec.get("subtopic") or "").strip()
    return _MOVES_PROMPT.format(
        topic=spec.get("topic") or "",
        subtopic_note=f" — {subtopic}" if subtopic else "",
        grade=grade,
        subject=subject,
        page_start=spec.get("page_start"),
        page_end=spec.get("page_end"),
        excerpt=excerpt[:MAX_MOVE_EXCERPT_CHARS],
        vocabulary=vocabulary,
    )


_HEADING_CUES = (
    ("activity", ("do this", "let us do", "try this", "let us try", "activity",
                  "let us find out", "do it yourself", "let us play")),
    ("practice", ("exercise", "practice", "questions", "answer the following",
                  "fill in the blanks", "solve", "test yourself")),
    ("summary", ("what we learnt", "what we have learnt", "summary", "recap",
                 "key points", "let us recall")),
    ("recall", ("you have learnt", "we have already", "recall", "revision")),
)

# Cues matched on word boundaries, not as substrings. Matched loosely, a bare
# 'play' cue read "(C) 2 players (Mahesh, Madhu) scored more than 50 runs" as the
# book setting an activity — three false activities in one Class 3 Maths chapter,
# each of which would have taken the Challenge section hostage to a line from a
# cricket scoreboard.
_CUE_PATTERNS = {
    kind: re.compile(r"\b(?:" + "|".join(re.escape(c) for c in cues) + r")\b", re.I)
    for kind, cues in _HEADING_CUES
}


def moves_from_headings(markdown: str, page_range: tuple[Optional[int], Optional[int]]) -> list[dict]:
    """A move sequence read off the headings alone, with no LLM.

    The fallback when extraction fails, and the reason the fallback is worth
    having rather than dropping to the canonical order: the two moves this
    catches reliably — the book's activity and the book's exercise — are exactly
    the two the design refuses to let a library activity replace. A sequence of
    just those, in printed order, still keeps the Challenge honest.
    """
    out: list[dict] = []
    page = page_range[0]
    for line in (markdown or "").splitlines():
        marker = re.match(r"<!--\s*page\s+(\d+)\s*-->", line.strip(), re.I)
        if marker:
            page = int(marker.group(1))
            continue
        heading = re.match(r"^#{1,6}\s+(.+?)\s*$", line)
        raw = (heading.group(1) if heading else line).strip()
        # The front-anchor below is what separates a task from a mention, so the
        # length cap is only here to skip whole paragraphs. Set at 80 it also
        # skipped "Activity-1: Drawing shapes by tracing objects like a matchbox,
        # a bangle and a leaf" — a real activity, printed as one long line, and
        # the first thing that chapter asks the children to do.
        if not raw or len(raw) > 200:
            continue
        for kind in _CUE_PATTERNS:
            match = _CUE_PATTERNS[kind].search(raw)
            # Anchored near the front: "Activity-3: paper folding" is the book
            # setting a task, "…which you did in the activity above" is a
            # back-reference.
            if match and match.start() <= 12:
                out.append({"type": kind, "gist": raw[:220], "page": page})
                break
    return normalise_moves(out, page_range)


# ── Naming the Challenge from the book's own task ────────────────────────────

_ASKS = re.compile(
    r"^\s*(the\s+)?(book|page|text|it)?\s*"
    r"(asks?|tells?|wants?|invites?|requires?|instructs?)\s+"
    r"(the\s+)?(child(ren)?|student(s)?|reader(s)?|pupil(s)?|them|us)?\s*(to\s+)?",
    re.I)

# A gist often names the DOER instead of the instruction — "children identify
# parts belonging to different animals". Stripping the subject turns the gist
# into the imperative a Challenge title wants: "Identify parts belonging to
# different animals". Kept separate from _ASKS because it runs after it: a gist
# can carry both ("the book asks children to ..." leaves "children ...").
_DOER = re.compile(
    r"^\s*(the\s+)?(child(ren)?|student(s)?|reader(s)?|pupil(s)?|kids?|"
    r"learners?|they)\s+(are\s+asked\s+to\s+|should\s+|must\s+|will\s+|"
    r"have\s+to\s+|need\s+to\s+)?",
    re.I)

# What disqualifies a name the MODEL wrote, when the book set a task. Any of
# these means it copied the page instead of naming the task.
_ENDS_QUESTION = re.compile(r"\?\s*$")
_SENTENCE_BREAK = re.compile(r"[.!?]\s+\S")


def usable_challenge_name(name: str, *, max_words: int = 12) -> bool:
    """Is this short enough, and shaped enough like a title, to print?

    WHY THIS EXISTS. When the book sets a task the model is told to name that
    task. Across two books it instead copied the page's own heading verbatim —
    "Where do birds live?", and in one Class 5 topic a seventy-word monologue —
    and the old backstop let both through because it only replaced a name that
    was EMPTY or that matched the activity library. A name the model invented
    was trusted unconditionally, which is exactly the case that fails.
    """
    name = (name or "").strip()
    if not name:
        return False
    if len(name.split()) > max_words:
        return False
    if _ENDS_QUESTION.search(name):      # a question is a prompt, not a task name
        return False
    if _SENTENCE_BREAK.search(name):     # prose lifted off the page
        return False
    return True


def challenge_name_from(move: dict, *, max_words: int = 9) -> str:
    """A short Challenge name taken from the book's own activity move.

    WHY THIS IS NOT A MODEL CALL. The Challenge has to be named for what the book
    asks whenever the book asks something, and three separate attempts to get a
    model to do that from the prompt produced the library template's name
    instead — the last of them after the field label was fixed. The move's `gist`
    already says what the book asks ("asks child to identify which house view is
    top, front or side"); turning that into a title is string work, and string
    work belongs in code.

    Returns "" when the gist yields nothing usable, so the caller can fall back
    rather than print a mangled title.
    """
    gist = (move or {}).get("gist") or ""
    gist = _ASKS.sub("", gist.strip()).strip(" .,:;—-")
    # A gist that was ALL preamble ("asks child to") leaves a stray "to" behind,
    # because the regex only strips it when something follows.
    gist = re.sub(r"^to\s+", "", gist, flags=re.I).strip(" .,:;—-")
    gist = _DOER.sub("", gist).strip(" .,:;—-")
    gist = re.sub(r"^to\s+", "", gist, flags=re.I).strip(" .,:;—-")
    if len(gist.split()) < 2:
        return ""
    words = gist.split()
    if len(words) > max_words:
        words = words[:max_words]
        # Never end a title on a word that promises more.
        while words and words[-1].lower().rstrip(",") in {
                "and", "or", "the", "a", "an", "of", "to", "in", "on", "with",
                "for", "by", "from", "which", "that", "using", "than", "as",
                "is", "are", "into", "at", "so"}:
            words.pop()
        # Cutting at the word cap can leave a preposition dangling one or two
        # words back — "identify parts belonging to different animals in a
        # drawn" — which reads as a sentence someone stopped typing. If the
        # tail still opens a phrase it cannot finish, drop the phrase.
        _OPENS = {"in", "on", "with", "of", "to", "for", "by", "from", "at",
                  "into", "about", "using"}
        for back in (2, 3):
            if len(words) > back and words[-back].lower().rstrip(",") in _OPENS:
                del words[-back:]
                break
    name = " ".join(words)
    return (name[:1].upper() + name[1:]) if name else ""
