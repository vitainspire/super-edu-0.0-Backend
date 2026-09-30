"""The Context Reinforcement Plan — Node 2's only generative output.

§7: "a compact, structured reinforcement plan. It should behave like a patch
specification for Generation." Not a second lesson, not a rewritten sheet — a
list of changes, each one carrying why it exists and what evidence it rests on.

The spec's own example is the schema this module normalises to, with three
fields added because the deterministic checks downstream cannot run without
them:

    factor       ─┐
    purpose       │  §7, verbatim
    change        │
    reason        │
    supports      │
    data_source   │
    confidence   ─┘
    type          §8's five adaptation types, as a field rather than as prose.
                  "Substitute the projector step with a board version" and "Add
                  a local example" need different validation — the first must
                  name something the room actually has, the second must not
                  invent a community — and a gate that has to infer which kind
                  it is looking at will infer wrong.
    section       §12 maps each of the six sections to the influence it can
                  take. An adaptation that does not say where it lands is one
                  Generation has to place by guessing.
    topic_index   Because a plan covers a lesson window, not one period.

WHY `no_change` IS A TYPE AND NOT AN ABSENCE. §8's last bullet and §18's
"localization theater" are the same point from two directions: the correct
output for a topic that needs nothing is a statement that it needs nothing, not
an empty list. An empty list is also what a failed call produces, and a system
that cannot tell "we looked and there was nothing to do" from "this did not run"
will eventually report the second as the first.

NOTHING HERE VALIDATES. This module gives the plan a shape; `gate.py` decides
whether it is allowed. The split is deliberate — normalisation that silently
drops a bad adaptation would make the gate's report say a plan was clean when it
was merely tidied.
"""
from typing import Any, Optional

from . import factors as factors_module

# ── §8: the five adaptation types ────────────────────────────────────────────
ADD, MODIFY, SUBSTITUTE, FALLBACK, NO_CHANGE = (
    "add", "modify", "substitute", "fallback", "no_change")
TYPES = (ADD, MODIFY, SUBSTITUTE, FALLBACK, NO_CHANGE)

# The two that make a claim about the physical room and must therefore survive
# the feasibility check in §10. An `add` that names a material does too, but
# these two cannot be made without naming one.
RESOURCE_CLAIMING = frozenset({SUBSTITUTE, FALLBACK})

# ── §9: the three purposes ───────────────────────────────────────────────────
PURPOSES = (factors_module.LEARNING, factors_module.ACCESS, factors_module.DELIVERY)

# ── The six sections a patch can land on (§12) ───────────────────────────────
SECTIONS = ("refresher", "concept", "realLife", "challenge", "levelSet", "explore")

# §2's "Minimal justified intervention" and §18's "Teacher overload", as a
# number. Three is a judgement about lessons rather than about models: a teacher
# handed four changes to a thirty-minute period is being handed a different
# period. The model is told the cap; the gate enforces it by dropping the
# lowest-confidence surplus rather than by rejecting the plan, because three good
# adaptations and one too many is not a failed plan.
MAX_ADAPTATIONS_PER_TOPIC = 3


def _clause(value: Any, limit: int = 240) -> str:
    return " ".join(str(value or "").split())[:limit].strip()


def _one(raw: dict, *, default_index: Optional[int]) -> Optional[dict]:
    if not isinstance(raw, dict):
        return None

    factor_id = _clause(raw.get("factor"), 60).lower().replace(" ", "_")
    kind = _clause(raw.get("type"), 20).lower()
    purpose = _clause(raw.get("purpose"), 20).lower()
    section = _clause(raw.get("section"), 20)
    # Case-insensitively, because `realLife` is the only camelCase name in the
    # six and a model writing `reallife` is not making a mistake worth failing.
    section = next((s for s in SECTIONS if s.lower() == section.lower()), "")

    try:
        index = int(raw.get("topicIndex", raw.get("topic_index", default_index)))
    except (TypeError, ValueError):
        index = default_index

    try:
        confidence = float(raw.get("confidence"))
    except (TypeError, ValueError):
        # Absent rather than 0.0. A missing number is not low confidence, and
        # the gate's provenance rules do not read this field at all — inventing
        # a value here would only make a later report look more precise than the
        # model was.
        confidence = None
    if confidence is not None:
        confidence = max(0.0, min(1.0, confidence))

    supports = [_clause(s, 80) for s in (raw.get("supports") or [])]
    return {
        "topicIndex": index,
        "factor": factor_id,
        "type": kind if kind in TYPES else "",
        "purpose": purpose if purpose in PURPOSES else "",
        "section": section,
        "change": _clause(raw.get("change"), 400),
        "reason": _clause(raw.get("reason"), 400),
        "supports": [s for s in supports if s],
        # The profile fields this adaptation says it rests on. NOT in §7's
        # example, and the single most important addition: without it the
        # provenance check in §10 can only ask "did the model claim verified",
        # which is the model marking its own homework. With it the gate can go
        # and look at the field.
        "evidence": [_clause(e, 60) for e in (raw.get("evidence") or []) if _clause(e, 60)],
        "dataSource": _clause(raw.get("data_source", raw.get("dataSource")), 20).lower(),
        "confidence": confidence,
        # Filled by the gate, never by the model.
        "accepted": None,
        "rejectedBecause": [],
    }


def normalise(raw: dict, *, topic_indexes: list[int]) -> dict:
    """Shape whatever the model returned into the plan the gate reads."""
    default_index = topic_indexes[0] if topic_indexes else None
    adaptations = []
    for entry in (raw.get("adaptations") or []):
        one = _one(entry, default_index=default_index)
        if one is not None:
            adaptations.append(one)

    # Stable, and by topic then confidence rather than by the order the model
    # happened to emit. The cap below drops from the end, so an unsorted list
    # would drop by luck.
    adaptations.sort(key=lambda a: (a["topicIndex"] if a["topicIndex"] is not None else 0,
                                    -(a["confidence"] or 0.0)))

    return {
        "adaptations": adaptations,
        # Echoed from the contract by the caller, not trusted from the model —
        # see `graph.py`. Present here so the plan is self-describing when it is
        # read back out of a log months later.
        "preserve": [_clause(p, 60) for p in (raw.get("preserve") or [])],
        # §10's "residual judgement": the one thing deterministic code cannot
        # decide. Defaults to True when the model omits it, because the default
        # must be the answer that stops a plan rather than the one that ships it.
        "lowersStandard": bool(raw.get("lowers_standard", raw.get("lowersStandard", True))),
        "note": _clause(raw.get("note"), 400),
    }


def for_topic(plan: dict, index: int) -> list[dict]:
    return [a for a in (plan.get("adaptations") or []) if a.get("topicIndex") == index]


def accepted(plan: dict) -> list[dict]:
    return [a for a in (plan.get("adaptations") or []) if a.get("accepted")]


def empty_plan(reason: str) -> dict:
    """What a topic with nothing to say produces — and what a failed call must
    NOT be able to imitate.

    `note` carries the reason either way, so the two are distinguishable in a
    log. See the module docstring.
    """
    return {"adaptations": [], "preserve": [], "lowersStandard": False,
            "note": reason}


def summarise(plan: dict) -> dict:
    adaptations = plan.get("adaptations") or []
    live = [a for a in adaptations if a.get("accepted")]
    by_purpose: dict[str, int] = {}
    by_type: dict[str, int] = {}
    for a in live:
        by_purpose[a.get("purpose") or "?"] = by_purpose.get(a.get("purpose") or "?", 0) + 1
        by_type[a.get("type") or "?"] = by_type.get(a.get("type") or "?", 0) + 1
    return {
        "proposed": len(adaptations),
        "accepted": len(live),
        "rejected": len([a for a in adaptations if a.get("accepted") is False]),
        "byPurpose": by_purpose,
        "byType": by_type,
        "topicsTouched": sorted({a["topicIndex"] for a in live
                                 if a.get("topicIndex") is not None}),
        "lowersStandard": plan.get("lowersStandard"),
    }
