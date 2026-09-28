"""Node 3's graph state.

Three inputs, and naming them is worth doing precisely because two of the three
are optional and the node behaves differently without them:

    contract   Node 1's, REQUIRED. Everything the checks measure against — the
               mastery target, the chain, the plan, the activity, the section
               spec — comes from here.
    materials  the generated sheets, REQUIRED. This node has nothing to say
               about a chapter nobody wrote.
    plan       Node 2's Context Reinforcement Plan, OPTIONAL. Without it the
               adoption check is silent rather than failing: a chapter generated
               with no context plan has not ignored one.
    sources    the textbook text per topic, OPTIONAL. Without it the two
               grounding checks say so in an advisory rather than passing
               quietly. `coverage.groundingChecked` on the verdict records which
               happened.

NO REPAIR STATE, and its absence is the design. `repair_round`, `sim_round`,
`material_history` and the revision-selection record were all in the old
in-package ChapterState because validation and repair were one loop. They are
not one loop any more: this node names what needs rewriting and stops. Whoever
owns generation owns the loop, re-runs it, and calls this node again with the new
material — which also means each call is stateless and independently replayable,
where the old loop's verdict depended on how many rounds had already been spent.
"""
import operator
from typing import Annotated, Literal, Optional, TypedDict


def merge_by_index(left: dict, right: dict) -> dict:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class ValidationState(TypedDict, total=False):
    # ── Inputs ───────────────────────────────────────────────────────────────
    run_id: str
    thread_id: Optional[str]
    contract: dict
    materials: dict                 # index -> the generated sheet
    plan: Optional[dict]            # Node 2's plan, when one was used
    sources: dict                   # index -> textbook excerpt
    chapter_text: str
    config: dict

    # The adapted state the revived checks read. Built once by `prepare_node`
    # and threaded through, rather than rebuilt per pass: `checks.py` and
    # `learner.py` must judge the same material against the same contract, and
    # two independent adaptations of the same inputs is how they would come to
    # disagree about which topic T7 is.
    adapted: dict

    # ── Structural pass (checks.py) ─────────────────────────────────────────
    issues: Annotated[dict, merge_by_index]     # index -> [finding, ...]
    batch_issues: list
    repair_targets: list
    repair_sections: Annotated[dict, merge_by_index]

    # ── Learner gate (learner.py) ───────────────────────────────────────────
    learner: Annotated[dict, merge_by_index]

    # ── Integrity (integrity.py) ────────────────────────────────────────────
    integrity_findings: list

    # ── Output ───────────────────────────────────────────────────────────────
    verdict: dict
    status: Literal["pending", "running", "ship", "needs_review", "refuse", "failed"]
    metrics: Annotated[dict, merge_by_index]
    errors: Annotated[list, operator.add]


DEFAULT_CONFIG = {
    # Whether generated sheets face the simulated learner. ON, because the
    # design's whole claim is that material should not be published until it has
    # been shown to teach — but it is ~1 extra call per topic, so it is
    # switchable, and `coverage.learnerGateRan` on the verdict says which
    # happened rather than leaving a cheap run looking like a thorough one.
    "simulate_learner": True,
    "learner_pass_threshold": 0.75,
    # Whether generated sheets face the classroom-reality gate (realism.py):
    # could this period run with one teacher, 30-60 children, a blackboard and
    # the textbook, and would those children follow it? ON, for the same reason
    # the learner gate is — material that cannot run in the room it was written
    # for should not reach a teacher as though it can. Costs ~1 call per topic,
    # so it is switchable, and `metrics.realism_gate_judged` records how many
    # sheets were actually judged rather than letting a skipped gate read as a
    # passed one.
    "verify_realism": True,
    # A dimension scoring zero fails the sheet whatever the weighted total says.
    # `cannot` / `unprepared` mean the sheet does not address that dimension at
    # all, and an average lets that through whenever the others carry it.
    "zero_dimension_fails": True,
    # Whether APPLICATION is pass/fail rather than a contribution to the average.
    # ON, because a sheet shown not to travel past its own example has taught the
    # example, and at 25% of the weighted total that finding was absorbed by the
    # other three often enough to ship at 0.79.
    "gate_transfer": True,
    "concurrency": 4,
    # Beyond this share of topics failing, the problem is the prompt or the
    # inputs rather than the individual sheets. This node does not repair, so it
    # does not abort — it reports the ratio and says so, which is the honest
    # version of the same judgement for a node that only advises.
    "failure_alarm_ratio": 0.6,
}
