"""Activity Selection Agent — what each of the six sections actually DOES.

Two selections happen here, and only one of them needs a model.

  1. The Challenge activity. Comes from the Pedagogy Library, matched on the
     topic's competencies. Deterministic, and deliberately so: the library is
     curated by people who know which activities work in a level-0 classroom of
     forty children, and asking a model to second-guess that adds cost and
     removes the audit trail.
  2. The section format for the creative sections — is Explore a physical hunt or
     a prediction game, is Level Set a partner tick-list or a build-it-home
     invitation. Chosen from a small fixed taxonomy, rotated so that forty
     consecutive topics do not all run "turn and tell".

The model is used for exactly one thing: naming an activity for topics the
library does not cover, and all of those are batched into a single call.

Variety is the whole point of this node. The failure it exists to prevent is not
a bad activity — `select_activity_and_context` already picks a good one — it is
the SAME good activity landing on topics 3, 9, 14 and 22, which is what a
per-topic best-match does when nothing is watching the batch.
"""
import zlib
from collections import Counter
from typing import Optional

from ..deps import select_activity_and_context
from ..llm import call_json
from ..moves import book_activity
from ..sections import CREATIVE_SECTIONS
from ..state import ChapterState

# Formats for the sections the model invents freely. Curated here rather than in
# the database because these are shapes of classroom interaction, not curriculum
# — they do not vary by school, and an admin has no reason to edit them.
SECTION_FORMATS: dict[str, tuple[str, ...]] = {
    "refresher": (
        "hands-up recall of the previous object",
        "partner re-tell in one breath",
        "one-question quick round on the previous scene",
        "act out what the class did last time",
    ),
    "realLife": (
        "market-stall scene",
        "home-kitchen scene",
        "walk-to-school scene",
        "festival preparation scene",
        "farm or field scene",
        "cricket or playground scene",
        "local shop transaction",
    ),
    "challenge": (
        "pair race against a shared target",
        "team relay at the board",
        "build-and-check with local materials",
        "sorting circle on the floor",
        "estimate-then-verify",
        "role-play with assigned jobs",
    ),
    "levelSet": (
        "partner tick-list self-check",
        "thumbs and reasons round",
        "one-thing-I-can-now-do share",
        "choose-your-own take-home challenge",
        "draw-what-you-learned card",
    ),
    "explore": (
        "curiosity object reveal",
        "predict-then-test",
        "spot-the-odd-one-out hunt",
        "story stopped at the interesting moment",
        "measure-something-outside dare",
        "puzzle with a hidden rule",
        "what-would-happen-if question",
    ),
}


def _rotate(formats: tuple[str, ...], recent: list[str], window: int, seed: int = 0) -> str:
    """The least recently used format, ties broken by a per-topic seed rather
    than declaration order.

    Declaration order as the tie-break meant every FIRST pick in an empty
    history landed on formats[0] — "market-stall scene" for realLife, every
    time, for every chapter. Invisible on a full 30-topic run, where the
    rotation has long since cycled past index 0 by the time anyone reads
    topic 8 — but format_history starts empty on every run, and a two-topic
    batch (prep_flow/topic_cli.py's default, or generating one sheet at a time)
    never gets past topic 1. The seed is stable per (topic, section), not
    random, so regenerating the same topic after a prompt fix still
    reproduces the same pick — comparing two runs still means something.
    """
    seen = set(recent[-window:])
    n = len(formats)
    for offset in range(n):
        candidate = formats[(seed + offset) % n]
        if candidate not in seen:
            return candidate
    counts = Counter(recent[-window * 2:] or recent)
    return min(formats, key=lambda f: (counts.get(f, 0), formats.index(f)))


def _topic_seed(section: str, chapter_number, topic_title: str, variation: int = 0) -> int:
    """A stable, non-random seed — Python's own hash() is salted per-process,
    which would make the same topic pick a different format on every CLI
    invocation and defeat the determinism _rotate is built around.

    `variation` is the one deliberate exception, and it defaults to 0 (no
    effect) for exactly one reason: gate.py's activation gate runs this same
    node twice against the SAME frozen fixture — once under the old adaptive
    state, once under the new — specifically to isolate what the adaptive
    state changed. Perturbing the format pick by default would inject noise
    into that comparison. A caller that wants a visibly different sheet on
    every click (the viewer's "Generate sheet") sets config.variation_seed
    itself; nothing else needs to know this parameter exists.
    """
    return zlib.crc32(f"{section}:{chapter_number}:{topic_title}:{variation}".encode("utf-8"))


def _category_weight(activity: dict, adaptive: dict) -> float:
    """How much the feedback loop wants this activity's category, 1.0 = neutral.

    Bounded on purpose. An optimizer that has seen four "no" responses on
    hands-on activities should nudge selection away from them, not ban a whole
    third of the Pedagogy Library on a sample of four.
    """
    weights = (adaptive or {}).get("activityCategoryWeights") or {}
    category = (activity.get("category") or "").strip().lower()
    try:
        weight = float(weights.get(category, 1.0))
    except (TypeError, ValueError):
        weight = 1.0
    return max(0.4, min(1.6, weight))


# The Challenge section is staged PLAY -> REFLECT -> ACT and the generation
# prompt forbids the words "quiz", "test", "assess" and "evaluate" outright. An
# Assessment-category template is therefore the wrong shape for this slot even
# when it matches the competencies perfectly.
#
# This is not hypothetical tidying. Once competency mapping started working, every
# candidate matched exactly one competency, so the base score tied across the
# board and `max()` fell through to declaration order — which is sorted by
# category, so "Assessment" won alphabetically on the first topic of every
# chapter. A flat score means the tie-break IS the selection.
_CHALLENGE_FIT = {
    "assessment": 0.55,     # right skill, wrong shape for a play activity
    "worksheet": 0.7,
    "movement": 1.25,
    "hands-on": 1.25,
    "game": 1.25,
    "communication": 1.1,
    "observe": 1.05,
}


def _score(activity: dict, recent_names: list[str], recent_categories: list[str],
           adaptive: dict, window: int) -> float:
    matched = len(activity.get("matchedCompetencyIds") or [])
    # +1 so a single-competency match is not indistinguishable from no match at
    # all once the multipliers below apply.
    score = float(matched) + 1.0
    name = activity.get("name") or ""
    category = (activity.get("category") or "").strip().lower()
    score *= _CHALLENGE_FIT.get(category, 1.0)
    # Challenge is collaborative by construction; a solo template can still win,
    # but it should not win a tie.
    if (activity.get("grouping") or "").strip().lower() in ("pair", "group"):
        score *= 1.08

    # Recency penalties, scaled by how recently. The name penalty is severe
    # because a repeated activity name is the visible failure; the category
    # penalty is mild because a chapter legitimately leans on one or two
    # categories.
    if name in recent_names[-window:]:
        distance = window - recent_names[-window:][::-1].index(name)
        score -= 6.0 * (distance / window)
    if category and category in recent_categories[-3:]:
        score -= 0.75

    return score * _category_weight(activity, adaptive)


def _pick_context(activity: dict, textbook_contexts: list[str],
                  recent_contexts: list[str]) -> Optional[dict]:
    """The activity's context, preferring one the textbook itself named.

    Same precedence the pilot's prompt argues for — the book's own examples
    outrank invented ones because the class has already met them on the page —
    but applied at selection time, where it can also avoid repeating a context
    the last few topics already used.
    """
    contexts = [c for c in (activity.get("contexts") or []) if isinstance(c, dict)]
    if not contexts:
        return None

    wanted = [c.strip().lower() for c in textbook_contexts if c and c.strip()]
    matches = [
        c for c in contexts
        if any(w in (c.get("name") or "").lower() or w in (c.get("category") or "").lower()
               for w in wanted)
    ]
    pool = matches or contexts
    fresh = [c for c in pool if (c.get("name") or "") not in recent_contexts[-4:]]
    return (fresh or pool)[0]


_INVENT_PROMPT = """The Pedagogy Library has no activity template for these topics, so name one for
each. You are naming and describing an activity, not writing the lesson.

GRADE: {grade} | SUBJECT: {subject} | CLASS SIZE: {class_size}
RESOURCE LEVEL: {resource_level} — 0 means NOTHING but the room, the children and
the blackboard; 1 adds notebook and pencil; 2 adds free local materials (stones,
seeds, sticks, bottle caps, string).

Activity categories already in use in this chapter, for consistency of naming:
{categories}

TOPICS NEEDING AN ACTIVITY:
{topics}

Rules:
- The activity must be runnable at the stated resource level, with that many
  children, in about 8 minutes. If it needs printed worksheets or manipulatives
  the classroom does not have, it is the wrong activity.
- The name is a NAME — 2-5 words, title case, the thing a teacher would write on
  a timetable ("Shape Hunt Relay", "Seed Count Race"). Not a sentence.
- Vary the activities across the list. Two topics on the same page should not get
  the same activity.
- "grouping" is one of individual | pair | group.

Return ONLY valid JSON, no markdown fences:
{{
  "activities": [
    {{
      "index": 1,
      "name": "Shape Hunt Relay",
      "category": "movement",
      "description": "one sentence: what the children physically do",
      "grouping": "pair",
      "context": "the concrete local thing it uses, or \\"\\" if none"
    }}
  ]
}}
"""


async def _invent_missing(gaps: list[dict], state: ChapterState,
                          categories: list[str]) -> dict[int, dict]:
    if not gaps:
        return {}
    settings = state.get("teacher_settings") or {}
    lines = "\n".join(
        f"T{spec['index']}. {spec['topic']}"
        + (f" — {spec['subtopic']}" if spec.get("subtopic") else "")
        + f"\n    teaches: {', '.join((spec.get('knowledge') or {}).get('competencies') or []) or '(unknown)'}"
        + f"\n    textbook's own examples: {', '.join((spec.get('knowledge') or {}).get('contexts') or []) or '(none)'}"
        for spec in gaps
    )
    try:
        data = await call_json(
            _INVENT_PROMPT.format(
                grade=state.get("grade"), subject=state.get("subject"),
                class_size=settings.get("classSize", 40),
                resource_level=settings.get("resourceLevel", 0),
                categories=", ".join(sorted(set(categories))) or "(none yet)",
                topics=lines,
            ),
            label="activity-invention", required=("activities",),
            temperature=0.7, max_tokens=3000,
        )
    except Exception as exc:
        print(f"[prep_flow:activity] invention failed for {len(gaps)} topic(s): {exc}")
        return {}

    invented: dict[int, dict] = {}
    ordered = [a for a in (data.get("activities") or []) if isinstance(a, dict)]
    by_index = {}
    for entry in ordered:
        try:
            by_index[int(entry.get("index"))] = entry
        except (TypeError, ValueError):
            continue
    for position, spec in enumerate(gaps):
        entry = by_index.get(spec["index"]) or (ordered[position] if position < len(ordered) else None)
        if not entry:
            continue
        name = (entry.get("name") or "").strip()
        if not name:
            continue
        invented[spec["index"]] = {
            "id": None,
            "name": name,
            "category": (entry.get("category") or "invented").strip(),
            "description": (entry.get("description") or "").strip(),
            "grouping": (entry.get("grouping") or "pair").strip(),
            "classroomType": "both",
            "assessmentMethod": "observation",
            "contexts": ([{"name": entry["context"].strip(), "category": None}]
                         if (entry.get("context") or "").strip() else []),
            "matchedCompetencyIds": [],
            "source": "invented",
        }
    return invented


def _selection_reason(activity: dict, candidates: list, adaptive: dict) -> str:
    """One sentence: why THIS activity, out of what was actually available.

    Built from exactly the signal _score() already computed to make the pick
    — matched competency count, category, recency, the adaptive weight —
    rather than re-deriving anything. Nothing here decides the winner; this
    only explains a decision already made, so a developer (or a founder)
    watching the console sees the reasoning, not just the outcome.
    """
    if not candidates:
        return "no Pedagogy Library candidates matched this topic's competencies -- invented"
    matched = len(activity.get("matchedCompetencyIds") or [])
    category = (activity.get("category") or "").strip()
    bits = [f"matched {matched} competenc{'y' if matched == 1 else 'ies'}"]
    if category:
        bits.append(f"category '{category}'")
    weight = _category_weight(activity, adaptive)
    if weight > 1.05:
        bits.append("this category is favoured by recent teacher feedback")
    elif weight < 0.95:
        bits.append("this category is de-emphasised by recent teacher feedback")
    if len(candidates) > 1:
        bits.append(f"best of {len(candidates)} candidate(s) considered")
    return "; ".join(bits)


async def activity_selection_node(state: ChapterState) -> dict:
    topics = state.get("topics") or []
    if not topics:
        return {"status": "failed", "errors": ["activity selection: no topics"]}

    config = state.get("config") or {}
    adaptive = state.get("adaptive_state") or {}
    window = int(config.get("no_repeat_window", 5))
    variation = int(config.get("variation_seed") or 0)

    recent_names: list[str] = []
    recent_categories: list[str] = []
    recent_contexts: list[str] = []
    format_history: dict[str, list[str]] = {s: [] for s in SECTION_FORMATS}

    selections: dict[int, dict] = {}
    gaps: list[dict] = []

    for spec in topics:
        knowledge = spec.get("knowledge") or {}
        textbook_contexts = [c for c in (knowledge.get("contexts") or []) if isinstance(c, str)]
        candidates = spec.get("activities") or []

        activity = context = None
        reason = None
        if candidates:
            best = max(candidates, key=lambda a: _score(
                a, recent_names, recent_categories, adaptive, window))
            # Fall back to the pilot's own chooser when the diversity pass finds
            # nothing it prefers over the plain best match — same function, same
            # tie-breaking, so a single-topic run and a batch run agree.
            activity = best or select_activity_and_context(candidates)[0]
            context = _pick_context(activity, textbook_contexts, recent_contexts)
            reason = _selection_reason(activity, candidates, adaptive)
        else:
            gaps.append(spec)
            reason = _selection_reason({}, [], adaptive)

        formats = {
            section: _rotate(
                SECTION_FORMATS[section], format_history[section], window,
                seed=_topic_seed(section, state.get("chapter_number"), spec.get("topic") or "",
                                 variation=variation))
            for section in ("refresher", "realLife", *CREATIVE_SECTIONS)
            if section in SECTION_FORMATS
        }
        for section, chosen in formats.items():
            format_history[section].append(chosen)

        # Where the book prints its own task on these pages, the template stops
        # being the Challenge and becomes the STAGING for the Challenge — the
        # pairing, the timing, the reflect turn. Recorded rather than acted on:
        # the selector still picks the best template, because a book activity
        # printed as three bare words ("Fold the paper") needs one. What changes
        # is that the sheet, the validator and the provenance all now know which
        # of the two is the task and which is the wrapper.
        printed = book_activity(spec.get("moves") or [])
        selections[spec["index"]] = {
            "index": spec["index"],
            "activity": activity,
            "context": context,
            "formats": formats,
            "candidateCount": len(candidates),
            "reason": reason,
            "bookActivity": printed,
            "activityRole": "staging for the book's own task" if printed else "the task itself",
        }
        if activity:
            recent_names.append(activity.get("name") or "")
            recent_categories.append((activity.get("category") or "").strip().lower())
        if context:
            recent_contexts.append(context.get("name") or "")

    invented = await _invent_missing(gaps, state, recent_categories)
    for index, activity in invented.items():
        selections[index]["activity"] = activity
        selections[index]["context"] = (activity.get("contexts") or [None])[0]
        selections[index]["invented"] = True

    still_missing = [i for i, s in selections.items() if not s.get("activity")]
    named = [s["activity"]["name"] for s in selections.values() if s.get("activity")]
    repeats = sum(count - 1 for count in Counter(named).values() if count > 1)

    return {
        "selections": selections,
        "errors": ([f"activity selection: no activity for topic(s) "
                    f"{', '.join(f'T{i}' for i in sorted(still_missing))} — generation "
                    f"will be asked to invent one inline"] if still_missing else []),
        "metrics": {
            "activities_from_library": len(named) - len(invented),
            "activities_invented": len(invented),
            "activities_missing": len(still_missing),
            "activity_repeats": repeats,
            "distinct_activities": len(set(named)),
        },
    }
