"""Node 2, as the architecture in §13 draws it.

    Context Profile
          v
    Deterministic Normalization          normalise_node
          v
    Factor Activation + Topic Relevance  activate_node
          v
    ONE Structured LLM Reasoning Call    reason_node
          v
    Context Reinforcement Plan
          v
    Deterministic Equity / Safety Gate   gate_node
          v
    Existing Generation                  (downstream — not this package)

FOUR NODES, THREE OF THEM FREE. Normalisation, activation and the gate make no
model call at all; only `reason_node` spends anything, and it spends one call per
window of topics. That ratio is the node's whole cost argument (§14): the
expensive step sees only what the free steps decided it should see.

WHY IT IS A GRAPH AND NOT A FUNCTION. It could be four function calls — it very
nearly is. It is a LangGraph graph for two reasons that are not symmetry with
Node 1: the reasoning step is the only paid one, so a run that dies in the gate
should resume without paying for it twice; and the plan has to be logged whether
or not it is applied (§16), which means every path has to reach the end rather
than raise.

SHADOW MODE IS THE DEFAULT AND IS ENFORCED HERE. `apply()` returns the plan for
Generation only when the run was not in shadow mode AND the gate passed. §16 is
a sequence — run both arms, have teachers rate the proposals, compare a 20–30
topic golden set, and only then move Node 2 into the generation path — and a
default of "applied" would skip every step of it.
"""
import uuid
from typing import Optional

from langgraph.graph import END, START, StateGraph

from . import activation as activation_module
from . import gate as gate_module
from . import plan as plan_module
from . import feasibility
from . import profile as profile_module
from . import reasoning as reasoning_module
from . import shadow as shadow_module
from . import store as store_module
from .state import DEFAULT_CONFIG, ContextState

try:
    from prep_flow.contract import PRESERVE
    from prep_flow.llm import gather_bounded
except ImportError:  # pragma: no cover — the package layout in the real backend
    from ..prep_flow.contract import PRESERVE
    from ..prep_flow.llm import gather_bounded


# ── 1. Deterministic normalization ───────────────────────────────────────────

async def normalise_node(state: ContextState) -> dict:
    """Raw intake in, one shape out — with everything unusable dropped and counted.

    Nothing here is a judgement. A field is kept if some factor reads it, it has
    a value, its provenance is not `unknown`, and — for community and cultural
    fields — it is verified. `profile.py` states why each of those is a rule
    rather than a preference.
    """
    profile = profile_module.normalise(
        state.get("raw_profile") or {},
        school_id=state.get("school_id"),
        class_id=state.get("class_id"),
        teacher_settings=(state.get("contract") or {}).get("deliveryAssumptions") or {},
    )
    summary = profile.summary()
    return {
        "profile": profile.as_dict(),
        "profile_summary": summary,
        "metrics": {
            "profile_fields": summary["fields"],
            "profile_verified": summary["verified"],
            "profile_dropped": summary["dropped"],
        },
    }


# ── 2. Factor activation + topic relevance ───────────────────────────────────

def _rebuild_profile(state: ContextState) -> profile_module.ContextProfile:
    """The ContextProfile back from the serialised state.

    The state holds plain dicts so the checkpointer can carry it; the checks
    want `Field` objects with `.verified` and `.stale`. Rebuilt rather than
    stored, because a namedtuple in a checkpoint is a schema nobody can migrate.
    """
    stored = (state.get("profile") or {}).get("fields") or {}
    fields = {
        name: profile_module.Field(
            name, entry.get("value"), entry.get("provenance"),
            entry.get("origin"), entry.get("updatedAt"))
        for name, entry in stored.items()
    }
    return profile_module.ContextProfile(
        fields, dropped=(state.get("profile") or {}).get("dropped") or [],
        school_id=state.get("school_id"), class_id=state.get("class_id"))


async def activate_node(state: ContextState) -> dict:
    """Which factors may speak, per topic. No model, by design (§5)."""
    profile = _rebuild_profile(state)
    rows = (state.get("contract") or {}).get("topics") or []

    activation: dict = {}
    proposable_total = 0
    for row in rows:
        index = row.get("index")
        if index is None:
            continue
        entries = activation_module.activate(profile, row)
        summary = activation_module.summarise(entries)
        activation[index] = summary
        proposable_total += len(summary["proposable"])

    return {
        "activation": activation,
        "metrics": {
            "topics": len(rows),
            "factors_proposable_total": proposable_total,
            # The number a reviewer should watch. Zero across a whole chapter
            # means Node 2 has nothing to work with — which is a data problem at
            # the school, not a model problem, and is invisible from the plan
            # alone because an empty plan is also what "nothing needed changing"
            # looks like.
            "factors_proposable_mean": (
                round(proposable_total / len(rows), 2) if rows else 0),
        },
    }


# ── 3. The one reasoning call ────────────────────────────────────────────────

async def reason_node(state: ContextState) -> dict:
    """One structured call per window. The only paid step in this node."""
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    document = state.get("contract") or {}
    rows = document.get("topics") or []
    if not rows:
        return {"plan": plan_module.empty_plan("the contract carried no topics"),
                "metrics": {"reasoning_calls": 0}}

    profile = _rebuild_profile(state)
    # Recomputed rather than read off `activation`: the state holds the SUMMARY
    # (lists of ids, for logging), and the prompt builder needs the Activation
    # objects with their `exposes` and `hint`. Cheap — no model, no I/O — and
    # the alternative is storing the registry in a checkpoint.
    activations_by_index = {
        row["index"]: activation_module.activate(profile, row)
        for row in rows if row.get("index") is not None}

    size = max(1, int(config.get("window_size", 3)))
    windows = [rows[i:i + size] for i in range(0, len(rows), size)]

    grade = str(document.get("grade") or "")
    subject = document.get("subject") or ""
    duration = int((document.get("deliveryAssumptions") or {}).get("durationMinutes") or 30)

    jobs = [
        reasoning_module.propose(
            rows=window, profile=profile,
            activations_by_index={r["index"]: activations_by_index[r["index"]]
                                  for r in window if r.get("index") in activations_by_index},
            grade=grade, subject=subject, duration=duration,
            # Both only read when `config["deep_agents"]` is on. The contract is
            # passed whole because the agent's `get_academic_contract` tool reads
            # a topic out of it, and the alternative — re-deriving the rows from
            # `window` — would give the agent a different view of the contract
            # from the one the gate checks against.
            config=config, contract=document,
            # The cohort's reinforcement policy, or None. Reaching the prompt is
            # the whole point: this is the only route by which teacher feedback
            # changes what Node 2 PROPOSES, rather than merely which proposals
            # survive the gate.
            policy=state.get("policy"))
        for window in windows
    ]
    results = await gather_bounded(jobs, limit=int(config.get("concurrency", 3)))

    adaptations: list[dict] = []
    errors: list[str] = []
    notes: list[str] = []
    calls = 0
    for window, result in zip(windows, results):
        first = window[0].get("index")
        last = window[-1].get("index")
        if isinstance(result, BaseException):
            # One window failing must not cost the others. A chapter with 27
            # topics' worth of contextual advice and a note about the three that
            # errored is worth having; a raised exception is worth nothing, which
            # is the same argument Node 1's persist path makes.
            errors.append(f"context-reinforcement[{first}-{last}]: {result}")
            continue
        adaptations += result.get("adaptations") or []
        if result.get("note"):
            notes.append(f"T{first}-{last}: {result['note']}")
        # A window with no proposable factor makes no call — `propose` says so in
        # its note and returns early. Counting calls from the notes rather than
        # from the windows keeps the token metric honest.
        if result.get("adaptations") or "no model call was made" not in (result.get("note") or ""):
            calls += 1

    # The adaptation's identity for the rest of its life, stamped at the one
    # moment the final ordering exists. Everything after this point sees a
    # SUBSET — the gate marks some rejected, `apply()` hands generation only the
    # accepted ones, and Node 3 reads that same subset — so a position in any of
    # those lists is a different number each time. The stored row and the
    # adoption verdict written back to it have to agree on which proposal they
    # mean, and re-deriving that later by matching on (topic, section, factor)
    # would collapse exactly the pair migration 036's UNIQUE constraint keeps
    # apart: a rejected proposal and an accepted one that agree on all three.
    for ordinal, adaptation in enumerate(adaptations):
        adaptation["ordinal"] = ordinal

    merged = {
        "adaptations": adaptations,
        # From the CONTRACT, never from the model. §7's `preserve` is a statement
        # of what may not move, and a plan that got to declare its own would be
        # a plan that could shorten the list.
        "preserve": list(PRESERVE),
        # True if ANY window said so. A single window judging that it lowered the
        # standard refuses the plan (see gate.py) — the alternative is shipping
        # the other windows beside a self-declared failure.
        "lowersStandard": any(
            r.get("lowersStandard") for r in results
            if not isinstance(r, BaseException)),
        "note": " | ".join(notes),
    }
    return {
        "plan": merged,
        "errors": errors,
        "metrics": {"reasoning_calls": calls, "reasoning_windows": len(windows),
                    "adaptations_proposed": len(adaptations)},
    }


# ── 4. The deterministic equity / safety gate ────────────────────────────────

async def gate_node(state: ContextState) -> dict:
    """Every adaptation checked against the room, the target and §18's failures."""
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    document = state.get("contract") or {}
    rows = {r["index"]: r for r in (document.get("topics") or []) if r.get("index") is not None}
    profile = _rebuild_profile(state)
    activations_by_index = {
        index: activation_module.activate(profile, row) for index, row in rows.items()}

    plan = dict(state.get("plan") or {})
    plan["adaptations"] = [dict(a) for a in (plan.get("adaptations") or [])]
    # The policy's second route. It re-orders each topic's survivors and may
    # lower the per-period cap; it can never accept what a check rejected.
    report = gate_module.run(plan, contract_rows=rows, profile=profile,
                             activations_by_index=activations_by_index,
                             policy=state.get("policy"))

    shadow = bool(config.get("shadow_mode", True))
    if not report["passed"]:
        status = "refused"
    elif shadow:
        status = "shadowed"
    else:
        status = "applied"

    proposed = report["proposed"] or 0
    ratio = (report["rejected"] / proposed) if proposed else 0.0
    if proposed and ratio >= float(config.get("rejection_alarm_ratio", 0.5)):
        print(f"[context_flow:gate] {report['rejected']}/{proposed} adaptations "
              f"rejected ({ratio:.0%}) — at this rate the problem is the prompt "
              f"or the context profile, not the individual proposals. "
              f"By check: {report['byCheck']}")

    return {
        "plan": plan,
        "gate": report,
        "shadow": shadow or not report["passed"],
        "status": status,
        "metrics": {
            "adaptations_accepted": report["accepted"],
            "adaptations_rejected": report["rejected"],
            "gate_rejection_ratio": round(ratio, 3),
            "gate_lowers_standard": report["lowersStandard"],
            **{f"gate_{check}": count for check, count in report["byCheck"].items()},
        },
    }


# ── 5. Persist ───────────────────────────────────────────────────────────────

async def persist_node(state: ContextState) -> dict:
    """Write the run and every proposal it made, accepted or not.

    ADDED WHEN THE REINFORCEMENT LOOP NEEDED EVIDENCE. Until then this node did
    not exist and Node 2 wrote nothing: `report()` built a shadow record for a
    human to read, and one JSON file per run is not something you can ask "did
    adaptations of this kind help, across the last three batches" — that is a
    join, and there was nothing to join.

    SHADOW RUNS ARE STORED TOO, and marked. A shadow run's proposals never
    reached generation, so no rating can be evidence about them — but the
    proposals themselves are exactly what §16's review is for, and the `mode`
    column is what keeps attribution from mistaking one for the other.

    NEVER RAISES. `store.save_plan` is best-effort by the same convention every
    Supabase write in this codebase follows: losing a plan because a bookkeeping
    insert failed would be the worst possible trade. A run that could not be
    stored contributes nothing to the loop and still returns its plan.
    """
    if not (state.get("config") or {}).get("persist", True):
        return {}

    context_run_id = await store_module.save_plan(
        state,
        # Stamped from the policy actually used, falling back to the config. A
        # run recorded under a version it did not plan with would put its ratings
        # in the wrong window, which is the one error the whole loop is unable to
        # detect later.
        reinforcement_version=(state.get("policy") or {}).get("_version",
            (state.get("config") or {}).get("reinforcement_version")))
    if context_run_id is None:
        return {"metrics": {"persisted": 0}}
    return {"context_run_id": context_run_id, "metrics": {"persisted": 1}}


# ── Graph construction ───────────────────────────────────────────────────────

def build_context_graph(checkpointer=None):
    graph = StateGraph(ContextState)

    graph.add_node("normalise", normalise_node)
    graph.add_node("activate", activate_node)
    graph.add_node("reason", reason_node)
    graph.add_node("gate", gate_node)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "normalise")
    graph.add_edge("normalise", "activate")
    graph.add_edge("activate", "reason")
    graph.add_edge("reason", "gate")
    # Every path ends here, refused plans included — the same rule Node 1's
    # graph follows. A refused plan is the most informative row the loop can
    # have about a factor, and discarding it because the gate said no would
    # leave "this never helps" and "this never got through" looking identical.
    graph.add_edge("gate", "persist")
    graph.add_edge("persist", END)

    return graph.compile(checkpointer=checkpointer)


# ── Entry point ──────────────────────────────────────────────────────────────

async def _load_policy(contract: dict, school_id) -> Optional[dict]:
    """The reinforcement policy in force for this cohort.

    LOADED HERE RATHER THAN ASKED OF THE CALLER. Every caller would otherwise
    have to remember to fetch it, and the one that forgot would plan neutral
    while believing it had planned under the policy — a difference invisible in
    the output and fatal to the next cycle, which would attribute those ratings
    to a version that never influenced them.

    Best-effort: a cohort with no policy, an unapplied migration 037, or no
    database at all all return None, and None is exactly the cold start Node 2
    is designed around.
    """
    try:
        from prep_flow.db import scope_key
        from reinforcement.store import active_policy
    except Exception:                   # noqa: BLE001 - the node runs without it
        return None
    grade = str(contract.get("grade") or "")
    subject = contract.get("subject") or ""
    if not grade or not subject:
        return None
    try:
        return await active_policy(scope_key(school_id, grade, subject))
    except Exception as exc:            # noqa: BLE001
        print(f"[context_flow] the reinforcement policy could not be read "
              f"({str(exc)[:120]}); planning neutral.")
        return None


async def run_context(
    *, contract: dict, context_profile: dict = None,
    school_id: str = None, class_id: str = None,
    config: dict = None, run_id: str = None, thread_id: str = None,
    policy: dict = None, graph=None, checkpointer=None,
) -> dict:
    """Run Node 2 over one Node 1 contract. Returns the final state.

    The plan is `result["plan"]`; whether Generation may use it is
    `apply(result)`, which is not the same question — see §16.

    `contract` is Node 1's document, whole. Nothing else of Node 1's is read,
    which is what makes a logged contract replayable against a later Node 2.
    """
    merged = {**DEFAULT_CONFIG, **(config or {})}
    run_id = run_id or str(uuid.uuid4())

    # An explicit `policy` wins — that is how the A/B arms are held apart, and
    # how a caller replays an old contract under a policy that is no longer in
    # force. `policy={}` is a deliberate "plan neutral" and is not overridden.
    if policy is None and merged.get("load_policy", True):
        policy = await _load_policy(contract, school_id)

    initial: ContextState = {
        "run_id": run_id,
        "thread_id": thread_id or f"context:{run_id}",
        "school_id": school_id,
        "class_id": class_id,
        "contract": contract or {},
        "raw_profile": context_profile or {},
        "policy": policy or None,
        "config": merged,
        "status": "running",
        "metrics": {},
        "errors": [],
    }
    invoke_config = {"configurable": {"thread_id": initial["thread_id"]},
                     "recursion_limit": 12}

    if graph is not None:
        return await graph.ainvoke(initial, invoke_config)
    return await build_context_graph(checkpointer).ainvoke(initial, invoke_config)


def apply(state: dict) -> Optional[dict]:
    """The plan Generation may actually consume — or None.

    THE ONE PLACE SHADOW MODE IS DECIDED, so a caller cannot accidentally read
    `state["plan"]` and use it. A plan exists on every run; a plan Generation is
    allowed to act on exists only when the gate passed and the run was not in
    shadow mode.

    Returns only the ACCEPTED adaptations, with the gate's own verdicts stripped
    — Generation should be handed instructions, not a mix of instructions and
    rejected proposals it has to filter.
    """
    if state.get("shadow") or state.get("status") != "applied":
        return None
    plan = state.get("plan") or {}
    return {
        # `ordinal` deliberately survives this filter while `accepted` and
        # `rejectedBecause` do not. Those two are the gate's working notes and
        # mean nothing to a generator handed only survivors; the ordinal is the
        # row identity, and Node 3 needs it to write its adoption verdict back to
        # the right proposal.
        "adaptations": [
            {k: v for k, v in a.items() if k not in ("accepted", "rejectedBecause")}
            for a in plan_module.accepted(plan)
        ],
        # The stored run these adaptations came from, or None when persistence
        # was off or failed. Node 3 needs it for the same reason as the ordinal;
        # its absence is how Node 3 knows there is nothing to write back to,
        # rather than discovering that one update at a time.
        "contextRunId": state.get("context_run_id"),
        "preserve": plan.get("preserve") or list(PRESERVE),
        "constraints": {
            **room_constraints(_rebuild_profile(state)),
            # The anchor edits ride with the limits rather than with the
            # adaptations, because they are not proposals: no model chose them
            # and the gate has nothing to weigh. See `feasibility.py`.
            "anchorSubstitutions": feasibility.anchor_substitutions(
                state.get("contract") or {}, _rebuild_profile(state)),
        },
        "lowersStandard": False,
        "note": plan.get("note") or "",
    }


def room_constraints(profile) -> dict:
    """What this room makes IMPOSSIBLE — derived from the profile, by rule.

    WHY THIS TRAVELS SEPARATELY FROM THE ADAPTATIONS. An adaptation is a change
    somebody proposed and the gate allowed. A constraint is not a proposal at
    all: it is a fact about the room that no amount of good writing may
    contradict, and it holds whether or not any adaptation happened to mention
    it.

    Leaving it implicit produced exactly the failure it exists to stop. A run
    against a school recording "basic textbook and notebooks" produced a period
    that opens "Distribute matchsticks to each pair" and lists matchsticks as a
    required material — with Node 1 having already flagged, on that very topic,
    that there were "no actual matchsticks or similar manipulatives to build
    with". Nothing lied. Generation was handed the room's facts as prose and
    left to infer a rule from them, and it inferred the comfortable one.

    THREE STATES, NOT TWO. A material is confirmed present, confirmed absent, or
    unrecorded — and the third is not the first. `materialsRecorded` says
    whether the question was ever answered, so a school that has told us nothing
    gets no verdict rather than a pass.
    """
    forbidden, present = [], []
    for field, name in (("has_projector", "a projector"),
                        ("has_internet", "internet access"),
                        ("electricity", "mains electricity"),
                        ("has_board", "a board"),
                        ("has_paper", "paper")):
        if not profile.has(field):
            continue
        (present if profile.value(field) else forbidden).append(name)

    out = {
        "forbidden": forbidden,
        "confirmedPresent": present,
        "materials": sorted(profile_module.available_materials(profile)),
        "materialsRecorded": profile_module.materials_recorded(profile),
    }

    # Out-of-school work is a feasibility fact, not a preference. A class whose
    # homework is recorded as not feasible cannot carry mastery home, and an
    # "optional" home task that mastery quietly depends on is the same failure
    # wearing a softer word.
    if profile.has("homework_feasible") and not profile.value("homework_feasible"):
        out["noRequiredHomework"] = True
    return out


def report(state: dict) -> dict:
    """Everything §16 and §17 want logged from one run, in one object."""
    return shadow_module.record(state)
