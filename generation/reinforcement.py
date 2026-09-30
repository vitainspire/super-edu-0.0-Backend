"""The generator contract: how Node 2's plan reaches the prompt.

§18 names "Generation ignores Node 2" as a failure mode and gives its guardrail
as "context adoption metric + generator contract". Node 3 computes the metric.
This is the contract — the other half, and the half that has to exist first,
because a metric measuring whether the generator used a plan it was never shown
measures nothing.

ONE BLOCK, PLACED LAST, AND WORDED AS INSTRUCTIONS. Three decisions, each of
which was available to be got wrong:

  * **Last in the topic block**, after the pages, the plan, the chain and the
    experience. Everything above it establishes what the lesson IS; this says
    what to do differently about it. Put earlier, an adaptation reads as one more
    input to weigh, and the model weighs it against the textbook — which it
    should not, because Node 2 already decided the adaptation is justified and
    the gate already decided it is safe.

  * **Grouped by section**, not listed flat. The model writes the sheet section
    by section, so an adaptation that names `concept` has to be readable at the
    moment Concept is written. A flat list at the top of a six-section prompt is
    a list the model has stopped tracking by section four.

  * **The reason travels with the change.** An instruction with no reason is one
    the model will trade away when it conflicts with something else in the
    prompt — and the trade will be silent. "Say it in Marathi first BECAUSE the
    recorded home language differs from the medium of instruction" survives a
    conflict that "say it in Marathi first" does not.

WHAT IS DELIBERATELY NOT SAID. The block does not carry `confidence`, and it does
not carry `dataSource`. Both are Node 2's own bookkeeping about how sure it is
and how good its evidence was, and both were already acted on: the gate refused
everything whose provenance did not hold up. Handing the survivors' confidence
scores to the generator would invite it to re-litigate a decision that has
already been made by a node with more information — and a 0.62 adaptation is not
an optional one, it is one that passed.

ACCEPTED ONLY. `context_flow.graph.apply()` already strips the rejected ones,
but this filters again on `accepted is not False`, because the two calls are in
different packages and a plan that arrives by some other route — read from a
log, replayed from a shadow record — must not smuggle a refused adaptation into
a prompt.
"""
from typing import Optional

try:
    from prep_flow.sections import SECTION_LABELS, SECTION_ORDER
except ImportError:  # pragma: no cover
    from ..prep_flow.sections import SECTION_LABELS, SECTION_ORDER

# What each adaptation type asks the generator to DO, in the imperative. The
# type is Node 2's word for the shape of the change (§8) and this is its
# translation into an instruction — kept here rather than in the plan, because
# Node 2's vocabulary should not have to double as prompt text.
_INSTRUCTION = {
    "add":        "ADD this. It is not in the plan above; put it in.",
    "modify":     "CHANGE the planned step to this.",
    "substitute": "REPLACE what the plan calls for with this. What it replaces is "
                  "not available in this room.",
    "fallback":   "USE THIS INSTEAD if the planned version cannot be run here.",
    "no_change":  "",
}


def accepted_for(plan: Optional[dict], index: int) -> list[dict]:
    """This topic's usable adaptations, in section order.

    Sorted by the section they land in, so the block below reads down the sheet
    in the order the sheet is written.
    """
    if not plan:
        return []
    order = {name: position for position, name in enumerate(SECTION_ORDER)}
    mine = [a for a in (plan.get("adaptations") or [])
            if a.get("topicIndex") == index
            and a.get("accepted") is not False
            and a.get("type") != "no_change"
            and (a.get("change") or "").strip()]
    return sorted(mine, key=lambda a: order.get(a.get("section"), len(order)))


def block(plan: Optional[dict], index: int) -> str:
    """The prompt block for one topic. Empty string when there is nothing to say.

    An empty string rather than a "no adaptations needed" notice: a prompt that
    tells the model there was nothing to adapt has spent tokens saying nothing,
    and on a chapter where Node 2 is inactive it would say it forty times.
    """
    adaptations = accepted_for(plan, index)
    limits = constraints_block(plan)
    if not adaptations:
        # NOT an early return any more. A room's limits hold whether or not
        # anybody proposed an adaptation for this topic — a class that cannot
        # take work home still cannot take work home on the topic Node 2 had
        # nothing to say about.
        return limits

    lines = [
        "",
        "  ── THIS CLASSROOM. Apply every one of these. ──",
        "  These are not suggestions and they are not preferences. Each was derived",
        "  from something recorded about THIS room, and each has already been checked",
        "  against the mastery target — none of them lowers it. Where one conflicts",
        "  with a habit of yours, the adaptation wins; where one appears to conflict",
        "  with the textbook's CONTENT, keep the content and adapt the route to it.",
    ]

    by_section: dict[str, list] = {}
    for a in adaptations:
        by_section.setdefault(a.get("section") or "", []).append(a)

    for section, items in by_section.items():
        label = SECTION_LABELS.get(section, "Anywhere on the sheet")
        lines.append("")
        lines.append(f"    In {label}:")
        for a in items:
            lines.append(f"      * {a['change']}")
            if a.get("reason"):
                lines.append(f"          why: {a['reason']}")
            instruction = _INSTRUCTION.get(a.get("type") or "", "")
            if instruction:
                lines.append(f"          {instruction}")
    return "\n".join(lines) + limits


def constraints_block(plan) -> str:
    """What this room makes impossible. Rendered whether or not anything adapted.

    THESE ARE NOT ADAPTATIONS and are deliberately worded harder than the block
    above. An adaptation improves the route to the target; a constraint says a
    route does not exist. The distinction was learned the expensive way: handed
    a school recording "basic textbook and notebooks", generation wrote
    "Distribute matchsticks to each pair" and listed matchsticks as required,
    on a topic where Node 1 had already recorded that no matchsticks existed.
    The room's facts were in the prompt as prose. Prose is a thing to weigh;
    this is a thing to obey.
    """
    limits = (plan or {}).get("constraints") or {}
    if not limits:
        return ""

    lines = ["", "  ── WHAT THIS ROOM CANNOT DO. These are limits, not preferences. ──"]

    if limits.get("forbidden"):
        lines.append(f"    This room does NOT have: {', '.join(limits['forbidden'])}.")
        lines.append("    No step may need any of them, not even as an alternative.")

    if limits.get("materialsRecorded"):
        have = ", ".join(limits.get("materials") or []) or "nothing at all"
        lines += [
            f"    The materials this room is recorded as having: {have}.",
            "    Do NOT require any physical material outside that list — not in the",
            "    Materials line, not in a step, not 'if available'. Where the textbook's",
            "    own activity needs something absent, KEEP THE THINKING AND SWAP THE",
            "    OBJECT for one on the list, and write the step with the substitute as",
            "    though it were the plan. Paper, a board and the children themselves are",
            "    usually enough to stand in for a manipulative.",
        ]
    else:
        lines.append("    Nobody recorded what this room has. Plan for a bare room:")
        lines.append("    a board, the textbook, and the children.")

    if limits.get("noRequiredHomework"):
        lines += [
            "    Work does NOT go home. Homework is recorded as not feasible for this",
            "    class, so no part of the mastery target may depend on anything done",
            "    outside the period — including an 'optional' look at home. Anything you",
            "    would have sent home happens inside the lesson instead.",
        ]

    return "\n".join(lines)


def summary(plan: Optional[dict]) -> dict:
    """What was handed over, for the run record.

    Counted per topic and per section, because "eleven adaptations" tells a
    reader nothing about whether one sheet took all of them.
    """
    if not plan:
        return {"adaptations": 0, "topics": 0, "bySection": {}}
    live = [a for a in (plan.get("adaptations") or [])
            if a.get("accepted") is not False and a.get("type") != "no_change"]
    by_section: dict[str, int] = {}
    for a in live:
        key = a.get("section") or "(unplaced)"
        by_section[key] = by_section.get(key, 0) + 1
    return {
        "adaptations": len(live),
        "topics": len({a.get("topicIndex") for a in live}),
        "bySection": by_section,
        "byFactor": {f: sum(1 for a in live if a.get("factor") == f)
                     for f in sorted({a.get("factor") for a in live if a.get("factor")})},
    }
