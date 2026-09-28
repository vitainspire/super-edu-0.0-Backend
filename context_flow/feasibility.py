"""Can this room actually supply the object the lesson is built on?

Node 1 chooses each period's ANCHOR — the physical thing children handle while
the idea lands — from the textbook, knowing nothing whatever about the room. It
is the right place to choose it: the book's own object usually carries the book's
own reasoning. But nothing downstream then asked whether the school HAS it.

WHAT THAT COST, from a real run. A school recorded its materials as "Basic
textbook and notebooks; limited supplementary materials". Node 1 set period 2's
anchor to `match-sticks` and recorded, in the same contract, that the topic was
missing "no actual matchsticks or similar manipulatives to build with".
Generation then wrote "Distribute matchsticks to each pair" and printed
matchsticks on the teacher's materials line. Nobody lied at any step; nobody was
ever asked the question.

Prompt pressure does not fix this, and we know because it was tried: told
plainly that the room has only textbook and notebooks, Node 2's resource factor
proposed "provide each pair of learners with a small bundle of actual
matchsticks". A constraint in a prompt argues with a contract, and the contract
wins — correctly, since the contract is the closed document.

So the substitution happens HERE, in code, as an edit to the anchor.

WHY THAT IS ALLOWED. `contract["preserve"]` lists masteryTarget,
requiredConcepts, competencies, knowledgeChain and prerequisites — the anchor is
not among them, and deliberately so. The anchor is the ROUTE to the target, not
the target. Swapping match-sticks for paper strips changes nothing a child must
end up able to do; refusing to swap them changes whether the lesson can happen.

THREE STATES, NOT TWO. A material is confirmed present, confirmed absent, or
never recorded — and the third is not the first. A school that has told us
nothing gets no verdict rather than a pass, because the alternative is a system
that substitutes everything at the schools that documented least.

WHAT IS *NOT* SUBSTITUTED, and this is most of what stops this being silly.
Quantity is the test. One cup the teacher holds up is not a procurement problem
and never becomes one; forty bottle caps, one per pair, is. Only anchors that a
class needs in BULK are checked, so a demonstration object is left exactly as
Node 1 chose it.
"""
from __future__ import annotations

import re

from . import profile as profile_module

AVAILABLE, UNAVAILABLE, UNKNOWN = "available", "unavailable", "unknown"

# Anchors a lesson needs one of PER CHILD OR PER PAIR. Nothing outside this set
# is ever substituted: a single object the teacher holds costs the school
# nothing to find, and rewriting it would be officious rather than helpful.
#
# Grouped by what the object DOES in the lesson, because that is what the
# substitute has to reproduce. A stand-in for match-sticks has to be straight
# and countable; a stand-in for bottle caps only has to be countable.
_BULK = {
    "straight": (
        "match-stick", "matchstick", "match stick", "stick", "twig", "straw",
        "rod", "strip", "ice-cream stick", "lolly stick", "broomstick",
    ),
    "countable": (
        "bottle cap", "cap", "seed", "tamarind seed", "bead", "button",
        "marble", "pebble", "stone", "counter", "token", "coin", "shell",
        "grain", "chalk piece", "block", "cube",
    ),
    "card": (
        "number card", "card", "flash card", "flashcard", "slip",
    ),
}

# What can stand in, best first, and what each needs the room to actually have.
# Paper leads everywhere it can: it tears into strips, folds into counters and
# takes a written number, so one recorded resource covers all three roles.
_STAND_INS = {
    "straight": (
        ("paper", "strips torn from a sheet of paper"),
        ("board", "straight lines drawn on the board, one per stick"),
    ),
    "countable": (
        ("paper", "small squares torn from a sheet of paper"),
        ("board", "tally marks on the board, rubbed out and redrawn"),
    ),
    "card": (
        ("paper", "numbers written on torn scraps of paper"),
        ("board", "numbers written on the board and pointed to"),
    ),
}


def _role_of(anchor: str) -> str | None:
    """What this object DOES, or None if it is not a bulk manipulative."""
    text = (anchor or "").strip().lower()
    if not text:
        return None
    for role, words in _BULK.items():
        for word in words:
            # Word-boundary matched: "a cup" must not match "cap", and
            # "bottle caps" must match "bottle cap".
            if re.search(rf"\b{re.escape(word)}s?\b", text):
                return role
    return None


def anchor_state(anchor: str, profile) -> str:
    """AVAILABLE, UNAVAILABLE or UNKNOWN — never a bare boolean.

    A non-bulk anchor is AVAILABLE by definition: the question this module asks
    is about supply, and one object is not a supply problem.
    """
    if _role_of(anchor) is None:
        return AVAILABLE
    if not profile_module.materials_recorded(profile):
        return UNKNOWN
    have = profile_module.available_materials(profile)
    text = (anchor or "").lower()
    for name in have:
        # `available_materials` can hold a whole recorded sentence, so the
        # containment is checked both ways round.
        if not name:
            continue
        if name in text or text in name:
            return AVAILABLE
    return UNAVAILABLE


def substitute_for(anchor: str, profile):
    """(object, why) to use instead — or None when nothing recorded can stand in.

    Returning None matters as much as returning a substitute: it means the room
    cannot run this period as designed and nobody should pretend otherwise by
    inventing a stand-in the school also does not have.
    """
    role = _role_of(anchor)
    if role is None:
        return None
    have = profile_module.available_materials(profile)
    for needs, description in _STAND_INS.get(role, ()):
        if any(needs in name for name in have):
            return description, needs
    return None


def anchor_substitutions(contract: dict, profile) -> dict:
    """{topic index: {from, to, state, why}} for every period that needs one.

    Only UNAVAILABLE anchors are substituted. UNKNOWN is left alone and
    reported: a school that recorded nothing has not told us it lacks
    match-sticks, and silently rewriting its lessons would punish it for the
    gap in its paperwork.
    """
    out: dict = {}
    for row in (contract or {}).get("topics") or []:
        index = row.get("index")
        anchor = ((row.get("experiencePlan") or {}).get("anchor") or "").strip()
        if index is None or not anchor:
            continue
        state = anchor_state(anchor, profile)
        if state != UNAVAILABLE:
            continue
        swap = substitute_for(anchor, profile)
        if not swap:
            out[str(index)] = {
                "from": anchor, "to": None, "state": state,
                "why": f"this room has no {anchor} and nothing recorded can "
                       f"stand in for it — the period needs a material the "
                       f"school does not have",
            }
            continue
        description, needs = swap
        out[str(index)] = {
            "from": anchor, "to": description, "state": state,
            "why": f"{anchor} is not among this room's recorded materials; "
                   f"{needs} is, and {description} does the same job",
        }
    return out
