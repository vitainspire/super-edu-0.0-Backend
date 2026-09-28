"""Node 2's graph state.

Far smaller than Node 1's, and the size is the point. §19: "a single
orchestration node with a deterministic context processor and one structured
reasoning call". There are no revisions here, no repair rounds and no histories,
because there is nothing iterative to record — the node runs once per lesson
window and produces a patch.

THE INPUT IS THE CONTRACT, NOT THE STATE OF NODE 1. `contract` is the whole
document Node 1 handed over (`prep_flow/contract.py`), and this node reads
nothing else of Node 1's. That is what makes shadow-mode replay possible: a
contract logged in March can be fed to a Node 2 changed in June with no other
part of the pipeline present.
"""
import operator
from typing import Annotated, Literal, Optional, TypedDict


def merge_by_index(left: dict, right: dict) -> dict:
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class ContextState(TypedDict, total=False):
    # ── Inputs ───────────────────────────────────────────────────────────────
    run_id: str
    thread_id: Optional[str]
    school_id: Optional[str]
    class_id: Optional[str]
    # Node 1's output, whole. The only thing this node knows about the lesson.
    contract: dict
    # The raw context profile as the intake surface supplied it, before
    # normalisation — kept so a log can show what was offered as well as what
    # survived. See profile.py.
    raw_profile: dict
    config: dict

    # ── Deterministic normalisation (§13, step 1) ───────────────────────────
    # The normalised profile as a plain dict (ContextProfile.as_dict()), so the
    # state stays JSON-serialisable for the checkpointer.
    profile: dict
    profile_summary: dict

    # ── Factor activation + topic relevance (§13, step 2) ───────────────────
    # index -> the activation summary for that topic. The Activation objects
    # themselves are rebuilt per node rather than stored: they hold a Factor
    # namedtuple, and a checkpointer should not be asked to carry the registry.
    activation: Annotated[dict, merge_by_index]

    # ── The reinforcement policy in force for this cohort ───────────────────
    # What teacher feedback has established about which adaptations are worth
    # making here. Read once at the start of a run and never re-read: a policy
    # that changed mid-chapter would split one batch across two of them, which
    # is the exact inconsistency a batch exists to prevent. None on a cohort that
    # has never completed a cycle, which is the ordinary starting state and not a
    # missing value.
    policy: Optional[dict]

    # ── The one reasoning call (§13, step 3) ────────────────────────────────
    plan: dict                      # the Context Reinforcement Plan (§7)

    # ── The deterministic gate (§13, step 4) ────────────────────────────────
    gate: dict

    # ── Bookkeeping ──────────────────────────────────────────────────────────
    # Whether this run was allowed to hand its plan to Generation, or only to
    # log it. §16 — shadow mode is the DEFAULT, and `shadow` records which one
    # actually happened rather than which one was configured, because a run
    # that fell back to shadow because the gate refused should say so.
    shadow: bool
    # The stored row this run became, when persistence was on and succeeded.
    # Node 3 needs it to attach adoption verdicts to the right proposals, and
    # its absence is how a caller knows the run contributed nothing to the loop.
    context_run_id: Optional[str]
    status: Literal["pending", "running", "applied", "shadowed", "refused", "failed"]
    metrics: Annotated[dict, merge_by_index]
    errors: Annotated[list, operator.add]


DEFAULT_CONFIG = {
    # §16: "Before Node 2 modifies production prep material, run it in shadow
    # mode." ON by default, and it stays on until a golden-set comparison says
    # otherwise — the framework asks for 20–30 topics rated by teachers before
    # this flips. A default of False would make the evaluation optional in
    # practice, since nothing would ever be in shadow mode to evaluate.
    "shadow_mode": True,
    # Topics per reasoning call. §14 permits batching "small lesson windows if
    # cross-topic context is useful", and it is — the room does not change
    # between T7 and T8, so a shared call will not propose the same language
    # bridge twice. Kept small: a window of ten would make one bad response cost
    # ten topics, and traceability is explicitly not to be sacrificed for it.
    "window_size": 3,
    # Reasoning calls in flight at once.
    "concurrency": 3,
    # Beyond this share of proposals rejected by the gate, the problem is the
    # prompt or the profile rather than the individual adaptations — the same
    # argument as Node 1's `repair_abort_ratio`. The run still completes and
    # still logs; the metric is what a reviewer is meant to notice.
    "rejection_alarm_ratio": 0.5,

    # ── The reinforcement loop (reinforcement/) ──────────────────────────────
    #
    # Write the run and its proposals to Supabase. ON by default: the
    # reinforcement loop learns from nothing else, and a persistence default of
    # False would make it opt-in for exactly the runs that matter. Turned off by
    # tests and by anyone replaying a contract who does not want the replay
    # counted as evidence.
    "persist": True,
    # The reinforcement policy version this run planned under, stamped onto the
    # stored row. None means "before the loop existed", which is a legitimate
    # baseline arm rather than missing data — see migration 036.
    "reinforcement_version": None,
    # Read the cohort's reinforcement policy at the start of a run. ON by
    # default: a node that planned neutral while believing it had planned under
    # the policy would mis-stamp its own version, and the next cycle would
    # attribute those ratings to a policy that never influenced them. Turned off
    # by the A/B arms, which supply their own.
    "load_policy": True,

    # ── The Deep Agent layer (deep_agents/) ──────────────────────────────────
    #
    # OFF by default, like everything else here that changes what reaches a
    # teacher. True routes the ONE reasoning call this node makes through an
    # agent that can look up each topic's contract and the room's resource and
    # language profiles before proposing. It is still one boundary per window —
    # §19's "single orchestration node with a deterministic context processor
    # and one structured reasoning call" — and not a swarm of contextual agents.
    #
    # Note the interaction with `shadow_mode`: both default safe, and turning
    # this on while shadow mode is still on is the intended order. An
    # agent-derived plan is logged and compared before it is ever allowed to
    # change a sheet.
    "deep_agents": False,
    "deep_agents_max_steps_context": 8,
    "deep_agents_workspace": None,
    "deep_agents_hitl": False,
}
