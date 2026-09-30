"""The generation stage: contract (+ optional context plan) in, six-section sheets out.

    NODE 1 contract ──┐
                      ├──> GENERATION ──> the material ──> NODE 3
    NODE 2 plan ──────┘   (this package)

NOT A NODE, A STAGE, and the distinction is the framework's (§1): Node 1 answers
what must be mastered, Node 2 answers what the room changes about the route, and
generation answers *how those two decisions appear in the final prep material*.
It decides nothing on its own — everything it writes traces to a contract field
or a plan adaptation.

WHY IT TAKES A CONTRACT RATHER THAN A ChapterState. It used to run inside Node 1
and read that node's working state directly. Reading the contract instead is what
makes the A/B in `compare.py` possible at all: a contract is a closed document,
so the same one can be generated from twice, months apart, with and without a
plan, and the only difference between the runs is the plan.

`adapt.py`'s job — rebuilding the state shape `compose.py` reads — is the same
job `validation_flow/adapter.py` does for Node 3, and the two are deliberately
separate. They look alike today and diverge the moment either stage needs a field
the other does not; a shared adapter would make one stage's needs the other's
constraint.
"""
import uuid
from typing import Optional

from . import adapt as adapt_module
from . import compose as compose_module
from . import reinforcement

try:
    from prep_flow.sections import SECTION_ORDER
except ImportError:  # pragma: no cover
    from ..prep_flow.sections import SECTION_ORDER

DEFAULT_CONFIG = {
    # Topics per generation call. NOT the whole chapter: the
    # Refresher-from-previous-Explore chain means the batch has to be written in
    # order anyway, and one call holding forty six-section sheets would truncate
    # long before it finished. Within a window the model sees consecutive topics
    # together and chains them itself; across windows an explicit handoff carries
    # the seam.
    "window_size": 3,
    "include_visuals": False,
    # Variation across repeated runs of the same topic. 0 is deterministic, which
    # is what the A/B wants: `compare.py` runs a second BASELINE arm to measure
    # the sampler's own noise, and a seed that varied per arm would put that
    # variation inside the measurement instead of beside it.
    "variation_seed": 0,
}


async def run_generation(
    *, contract: dict, context_plan: Optional[dict] = None,
    sources: Optional[dict] = None, chapter_text: str = "",
    config: Optional[dict] = None, label: str = "",
    run_id: str = None,
) -> dict:
    """Write every topic in the contract. Returns {"materials": {index: sheet}, ...}.

    `context_plan` is Node 2's, and passing None is a first-class case rather
    than a degraded one: it produces the prompt this stage used before Node 2
    existed, byte for byte, which is what the baseline arm of the A/B needs.

    Windows are run in SEQUENCE, not concurrently, and that is not an oversight.
    Each window's Refresher is written from the previous window's last Explore
    (`_carry_in` in compose.py reads `materials`), so a concurrent window would
    open on a handoff that had not been written yet.
    """
    merged = {**DEFAULT_CONFIG, **(config or {})}
    rows = [r for r in (contract.get("topics") or []) if r.get("index") is not None]
    if not rows:
        return {"materials": {}, "errors": ["the contract carried no topics"],
                "metrics": {"topicsGenerated": 0}}

    state = adapt_module.to_state(contract, sources=sources,
                                  chapter_text=chapter_text, config=merged)
    state["context_plan"] = context_plan
    # THE ANCHOR EDIT, APPLIED RATHER THAN ASKED FOR. Node 2 decides in code
    # which periods are built on an object this room does not have; here the
    # spec is rewritten so the substitute IS the plan, before a single token is
    # spent. Telling the model about the swap in the prompt was tried and lost
    # to the contract, which is the right thing for a prompt to lose to.
    swapped = adapt_module.apply_anchor_substitutions(state, context_plan)
    if swapped:
        state.setdefault("metrics", {})["anchors_substituted"] = swapped
    state["run_id"] = run_id or str(uuid.uuid4())
    state["materials"] = {}
    state["cursor"] = 0
    state["generated_order"] = []
    state["errors"] = []

    total = len(state["topics"])
    windows = 0
    while int(state.get("cursor") or 0) < total:
        before = int(state["cursor"])
        delta = await compose_module.generation_node(state)
        # Merged by hand rather than by a graph reducer: this stage is one loop
        # over one state, and a StateGraph here would add a checkpointer, a
        # recursion limit and a superstep budget to something that is a `while`.
        state["materials"] = {**state["materials"], **(delta.get("materials") or {})}
        state["generated_order"] += delta.get("generated_order") or []
        state["errors"] += delta.get("errors") or []
        state["cursor"] = int(delta.get("cursor", before))
        state["topics"] = delta.get("topics") or state["topics"]
        windows += 1
        if state["cursor"] <= before:
            # The cursor is the only thing that advances this loop. A window that
            # returned without moving it would spin forever, and a generation
            # stage that hangs is worse than one that stops and says why.
            state["errors"].append(
                f"{label or 'generation'}: the cursor did not advance past "
                f"{before} — stopping rather than looping")
            break

    return {
        "label": label,
        "materials": state["materials"],
        "errors": state["errors"],
        "contextPlanUsed": context_plan is not None,
        "metrics": {
            "topicsGenerated": len(state["materials"]),
            "topicsTotal": total,
            "windows": windows,
            "sectionsPerSheet": len(SECTION_ORDER),
            **reinforcement.summary(context_plan),
        },
    }


async def run_repair(
    *, contract: dict, materials: dict, issues: dict,
    repair_targets: list, repair_sections: Optional[dict] = None,
    context_plan: Optional[dict] = None, sources: Optional[dict] = None,
    chapter_text: str = "", config: Optional[dict] = None,
    repair_round: int = 0, run_id: str = None,
) -> dict:
    """Rewrite the sheets Node 3 could not vouch for, using WHY it could not.

    The public door onto `compose.repair_node`, which has always been able to do
    this and has had no caller: validation names what is wrong and stops, by
    design, so somebody outside both stages has to carry the findings back. That
    somebody is whoever owns the loop — `app/lib/prep_pipeline_bridge.py` here.

    `issues` and `repair_sections` are Node 3's, untranslated: the findings text
    is quoted back into the repair prompt, and a repair told only WHICH sections
    to rewrite rewrites them to the same standard that just failed. The sections
    list also narrows what is asked for and paid for — an empty list means the
    whole sheet, which is what a structural failure gets because it has no
    narrower surface.

    Returns `compose.repair_node`'s delta: {"materials": {index: rewritten},
    ...}. ONLY the repaired topics are in it, so a caller merges rather than
    replaces — the topics that already passed must not be re-written, and at
    this point they have not been.
    """
    merged = {**DEFAULT_CONFIG, **(config or {})}
    targets = sorted({int(i) for i in (repair_targets or [])})
    if not targets:
        return {"materials": {}, "errors": [], "repair_round": repair_round,
                "metrics": {}}

    state = adapt_module.to_state(contract, sources=sources,
                                  chapter_text=chapter_text, config=merged)
    state["context_plan"] = context_plan
    state["run_id"] = run_id or str(uuid.uuid4())
    # int keys throughout, for the reason to_state's own docstring gives: the
    # contract and the verdict both survive JSON round trips, and JSON has no
    # integer keys, so a caller handing back what it was given would index every
    # map with a string and silently find nothing on every topic.
    state["materials"] = {int(k): v for k, v in (materials or {}).items()}
    state["issues"] = {int(k): v for k, v in (issues or {}).items()}
    state["repair_sections"] = {int(k): list(v or [])
                                for k, v in (repair_sections or {}).items()}
    state["repair_targets"] = targets
    state["repair_round"] = int(repair_round)

    return await compose_module.repair_node(state)
