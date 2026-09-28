"""Shadow-mode records, and the metrics §17 asks for.

§16 is a sequence, not a switch: run the existing path, run Node 2 beside it, log
what it proposed, have teachers rate whether the proposals are relevant,
feasible, authentic and useful, compare a 20–30 topic golden set, and only then
move Node 2 into the generation path. This module is what makes the middle three
steps possible — a record per run that a person can read and a comparison can
aggregate.

WHAT IS DELIBERATELY NOT COMPUTED HERE. Four of §17's nine metrics cannot be
derived from a Node 2 run at all, and this module reports them as absent rather
than approximating them:

    context adoption          needs the generated sheet, to see whether the
                              accepted adaptation actually appears in it
    learner outcome delta     needs Node 3's learner gate, twice
    teacher readiness delta   needs the same, twice
    context usefulness        needs a teacher

Naming them with `None` is the point. A dashboard that quietly omits the four
metrics measuring whether Node 2 HELPED, while showing the five measuring whether
it RAN, is a dashboard that will conclude Node 2 works.

The five that ARE computable here are computed here, because they are properties
of the plan and the gate:

    mastery preservation rate  the share of topics whose target was untouched —
                               which after the gate is by construction 1.0, and
                               is reported anyway so a regression in the gate
                               shows up as a number rather than as silence
    context provenance validity  the share of accepted LOCAL adaptations backed
                               by a verified field
    feasibility compliance     the share of accepted adaptations that survived
                               the resource check
    over-adaptation rate       the share of topics that got the maximum number
                               of changes — §18's teacher overload, as a rate
    token cost per lesson      calls per topic, which is the honest proxy until
                               the ledger is wired through
"""
from datetime import datetime, timezone

from . import factors as factors_module
from . import plan as plan_module


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rate(numerator: int, denominator: int):
    return round(numerator / denominator, 3) if denominator else None


def metrics(state: dict) -> dict:
    """§17's list, with the four that need a downstream node left as None."""
    document = state.get("contract") or {}
    rows = document.get("topics") or []
    plan = state.get("plan") or {}
    gate = state.get("gate") or {}
    adaptations = plan.get("adaptations") or []
    accepted = plan_module.accepted(plan)

    local_accepted = [a for a in accepted
                      if (factors_module.BY_ID.get(a.get("factor")) or
                          factors_module.Factor("", 0, "", "", "", "", (), "", ())
                          ).tier == factors_module.LOCAL]
    local_with_verified = [a for a in local_accepted
                           if a.get("dataSource") == "verified" and a.get("evidence")]

    by_check = gate.get("byCheck") or {}
    resource_failures = by_check.get("resource_contradiction", 0)

    touched: dict = {}
    for a in accepted:
        if a.get("type") == plan_module.NO_CHANGE:
            continue
        touched[a.get("topicIndex")] = touched.get(a.get("topicIndex"), 0) + 1
    at_cap = sum(1 for count in touched.values()
                 if count >= plan_module.MAX_ADAPTATIONS_PER_TOPIC)

    calls = int((state.get("metrics") or {}).get("reasoning_calls") or 0)

    return {
        # Computable here
        "masteryPreservationRate": _rate(
            len(rows) - len({a.get("topicIndex") for a in adaptations
                             if not a.get("accepted")
                             and "lower the standard" in " ".join(a.get("rejectedBecause") or [])}),
            len(rows)),
        "contextProvenanceValidity": _rate(len(local_with_verified), len(local_accepted)),
        "feasibilityCompliance": _rate(
            len(accepted), len(accepted) + resource_failures),
        "overAdaptationRate": _rate(at_cap, len(rows)),
        "tokenCallsPerLesson": _rate(calls, len(rows)),

        # Not computable without a later node. Named, not omitted.
        "contextAdoption": None,          # needs the generated sheet
        "learnerOutcomeDelta": None,      # needs Node 3, both arms
        "teacherReadinessDelta": None,    # needs Node 3, both arms
        "contextUsefulness": None,        # needs a teacher

        "_pending": {
            "contextAdoption": "requires the generated sheet — compare accepted "
                               "adaptations against what Generation actually wrote",
            "learnerOutcomeDelta": "requires Node 3's learner gate on both arms",
            "teacherReadinessDelta": "requires Node 3 on both arms",
            "contextUsefulness": "requires a teacher or reviewer rating",
        },
    }


def record(state: dict) -> dict:
    """One run's shadow record — what was offered, what was proposed, what survived.

    Shaped to be read by a person first and aggregated second. The
    `proposals` list carries the REJECTED entries too, with their reasons,
    because §16 asks reviewers to judge whether the proposals are any good and a
    record showing only the survivors would be asking them to review the gate's
    taste rather than the model's.
    """
    document = state.get("contract") or {}
    plan = state.get("plan") or {}
    gate = state.get("gate") or {}
    # WHAT GENERATION IS ALSO HANDED. `graph.apply` derives the room's hard
    # limits and passes them alongside the adaptations; the record has to carry
    # them too, or the file a person feeds to `generation.cli --plan` is a
    # weaker instruction set than the in-process path, and the matchsticks come
    # back on exactly the runs somebody is inspecting by hand.
    from . import feasibility
    from .graph import room_constraints, _rebuild_profile
    try:
        profile = _rebuild_profile(state)
        constraints = {
            **room_constraints(profile),
            "anchorSubstitutions": feasibility.anchor_substitutions(
                state.get("contract") or {}, profile),
        }
    except Exception:                                      # noqa: BLE001
        constraints = {}
    profile_summary = state.get("profile_summary") or {}
    rows = {r.get("index"): r for r in (document.get("topics") or [])}

    proposals = []
    for a in plan.get("adaptations") or []:
        row = rows.get(a.get("topicIndex")) or {}
        proposals.append({
            "topicIndex": a.get("topicIndex"),
            "topic": row.get("topic"),
            "masteryTarget": (row.get("academic") or {}).get("masteryTarget"),
            "factor": a.get("factor"),
            "factorName": (factors_module.BY_ID.get(a.get("factor")) or
                           factors_module.Factor("", 0, a.get("factor") or "?", "",
                                                 "", "", (), "", ())).name,
            "type": a.get("type"),
            "purpose": a.get("purpose"),
            "section": a.get("section"),
            "change": a.get("change"),
            "reason": a.get("reason"),
            "supports": a.get("supports"),
            "evidence": a.get("evidence"),
            "dataSource": a.get("dataSource"),
            "confidence": a.get("confidence"),
            "accepted": a.get("accepted"),
            "rejectedBecause": a.get("rejectedBecause"),
            # The three questions §16 asks reviewers, left blank for them.
            # Present in the record rather than in a separate form so a rating
            # cannot be filed against a proposal nobody can see.
            "review": {"relevant": None, "feasible": None, "authentic": None,
                       "useful": None, "note": ""},
        })

    return {
        "recordedAt": now_iso(),
        "constraints": constraints,
        "runId": state.get("run_id"),
        "schoolId": state.get("school_id"),
        "classId": state.get("class_id"),
        "grade": document.get("grade"),
        "subject": document.get("subject"),
        "chapter": (document.get("chapter") or {}).get("title"),
        # Which arm this was. §16's step 2 is "run Node 2 independently against
        # the same lessons and LOG its proposed adaptations" — a record that did
        # not say whether the plan was applied would be useless for exactly the
        # comparison it exists to feed.
        "mode": "shadow" if state.get("shadow") else "applied",
        "status": state.get("status"),
        "contextProfile": profile_summary,
        # Why factors were off, per topic. The single most useful field for a
        # school asking why their lessons come back unlocalised.
        "activation": state.get("activation") or {},
        "gate": {k: v for k, v in gate.items() if k != "findings"},
        "findings": gate.get("findings") or [],
        "proposals": proposals,
        "metrics": metrics(state),
        "errors": state.get("errors") or [],
    }


def summary_line(state: dict) -> str:
    """One line, for a terminal or an SSE event."""
    gate = state.get("gate") or {}
    document = state.get("contract") or {}
    topics = len(document.get("topics") or [])
    return (f"[context_flow] {state.get('status')} — "
            f"{gate.get('accepted', 0)}/{gate.get('proposed', 0)} adaptation(s) "
            f"accepted across {topics} topic(s)"
            + (f" | rejected: {gate.get('byCheck')}" if gate.get("rejected") else "")
            + (" | SHADOW (not given to generation)" if state.get("shadow") else ""))
