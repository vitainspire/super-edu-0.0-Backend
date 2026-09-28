"""The merged stages: two graph nodes where there were six.

    sequencing ─> CURRICULUM ─> LESSON DESIGN ─> persist
                       │              │
      moves            │              │  experience
      knowledge        │              │  planning
      canonical        │              │  activity selection
      chain + mastery ─┘              └─

WHY COMPOSE RATHER THAN REWRITE. Each merged node calls the node functions that
were already there, in the order the graph used to call them, and merges their
returned state itself. Nothing about what any of them asks a model changes here.

That is the whole design. The prompts in those modules carry a great deal of
hard-won detail — the anchor pools grouped per strand because a counting thread
reached for marigolds, the chain gate that refuses to cache a degenerate chain —
and a merge that reimplemented them would put every one of those at risk to save
graph supersteps. The savings that ARE worth having are inside the individual
agents (windowing move extraction, fusing the experience and planning prompts)
and they are made there, where they can be judged one at a time.

WHAT A MERGE ACTUALLY COSTS, said plainly, because the diagram hides it: a
LangGraph node is the unit of checkpointing, so three nodes collapsed into one
means a run that dies half way through the merged stage resumes at the start of
it rather than in the middle. The old graph could resume a forty-topic chapter
after move extraction without paying for it twice.

That is bought back below by SKIPPING SUB-STEPS WHOSE WORK IS ALREADY IN STATE.
`_needs_moves` and friends ask whether the previous attempt got that far, so a
resumed run re-runs only what it has to. It is not free — the check is a
heuristic about state rather than a checkpoint — but it recovers the property
that mattered, which was never "resume at an arbitrary point" but "do not pay
for forty extractions twice".

THERE WAS A THIRD MERGED STAGE HERE — the quality gate: the validator plus the
repair budget applied to what it found. It is gone with the node split, not
disabled. Validation and the learner simulation are questions about generated
material, this package no longer generates any, and both modules are archived
under `_deferred/` waiting for Node 3. What that means for what ships is stated
where it will be read: `prep_flow/graph.py` has the whole list of what left.

The two stages that remain are the two that were always about the BOOK rather
than about a sheet, which is what Node 1 is.
"""
from ..state import ChapterState
from .activity_selection import activity_selection_node
from .context_assembly import context_assembly_node
from .experience import experience_node
from .move_extraction import move_extraction_node
from .planning import planning_node
from .reasoning import reasoning_node


# ── State merging ────────────────────────────────────────────────────────────
#
# A node returns a PARTIAL state and LangGraph applies the reducers declared in
# state.py to fold it in. Composing nodes by hand means applying those reducers
# by hand, and getting one wrong is silent: the run continues with a field that
# was replaced where it should have accumulated, and the loss shows up as a
# missing history two stages later rather than as an error here.
#
# So the rules live in one table, named after the reducer in state.py they
# mirror, and anything not listed is a plain replace — which is LangGraph's own
# default for an unannotated field.
_CONCAT = ("errors",)
_MERGE_BY_INDEX = ("plans", "selections", "experience", "metrics")


def _merge(into: dict, out: dict) -> dict:
    """Fold one node's return value into the accumulating stage output."""
    for key, value in (out or {}).items():
        if key in _CONCAT:
            into[key] = list(into.get(key) or []) + list(value or [])
        elif key in _MERGE_BY_INDEX:
            into[key] = {**(into.get(key) or {}), **(value or {})}
        else:
            into[key] = value
    return into


def _failed(out: dict) -> bool:
    """Did that sub-step give up on the chapter?

    Every node here signals an unusable chapter the same way — `status: failed`
    with an error — and a merged stage must stop rather than hand the next
    sub-step a state it already knows is empty. The stage still RETURNS what it
    has; persist_node is reachable from every failure path and thirty complete
    topic contracts with a report on the rest is worth having.
    """
    return (out or {}).get("status") == "failed"


def _view(state: ChapterState, accumulated: dict) -> ChapterState:
    """What the next sub-step reads: the incoming state plus what this stage has
    produced so far.

    Sub-steps inside a stage depend on each other — reasoning reads the concepts
    context assembly extracted, planning reads the experience plan — and in the
    old graph each read them off a committed checkpoint. Here they read them off
    this dict, which is why it is threaded through every call rather than being
    assembled at the end.
    """
    return {**state, **accumulated}


# ── 1. Curriculum + Learning Model ───────────────────────────────────────────

def _needs_moves(state: ChapterState) -> bool:
    topics = state.get("topics") or []
    return bool(topics) and not any(t.get("moves_source") for t in topics)


def _needs_knowledge(state: ChapterState) -> bool:
    return not bool(state.get("context_ready"))


def _needs_reasoning(state: ChapterState) -> bool:
    return not bool(state.get("reasoning"))


async def curriculum_node(state: ChapterState) -> dict:
    """What the book teaches, in what order, and what mastering it requires.

    Three sub-steps that were three nodes, and the order between them is load
    bearing in both directions:

      moves        must come first, because the order the BOOK explains a topic
                   in becomes the order the sheet teaches it in, and everything
                   after this allocates against that order rather than the
                   canonical six.
      knowledge    must come before the chain, which reasons about the concepts
                   it extracts.
      chain +      must come last, and the mastery audit inside it must come
      mastery      after the chain — the audit's whole job is to name what
                   mastery requires BEYOND the gain the chain settled, so with no
                   gain to exceed it has no standard to write against.

    Knowledge extraction stays the pilot's own prompt and is not merged into
    anything. `context_assembly` says why, and it is not a detail: material from
    the single-topic pipeline and material from this one have to stay comparable,
    and a second extraction prompt would quietly make them not. It is also the
    one sub-step here that WRITES to the shared canonical library, so a merge
    that re-ran it differently would seed that library differently too.
    """
    out: dict = {}

    if _needs_moves(state):
        out = _merge(out, await move_extraction_node(_view(state, out)))
        if _failed(out):
            return out
    else:
        print("[prep_flow:curriculum] moves already read for this chapter; skipping")

    if _needs_knowledge(_view(state, out)):
        out = _merge(out, await context_assembly_node(_view(state, out)))
        if _failed(out):
            return out
    else:
        print("[prep_flow:curriculum] context already assembled; skipping")

    if _needs_reasoning(_view(state, out)):
        out = _merge(out, await reasoning_node(_view(state, out)))
    else:
        print("[prep_flow:curriculum] reasoning already derived; skipping")

    return out


# ── 2. Experience + Lesson Planning ──────────────────────────────────────────

async def lesson_design_node(state: ChapterState) -> dict:
    """The path a child takes to the understanding, and the period around it.

    Experience, planning and activity selection, in the order they always ran.
    The dependency that fixes it is one-directional and worth naming: planning
    prints each topic's trajectory, hardest move and chosen object under the
    topic it belongs to, and allocates minutes against them. Reverse the two and
    the planner is doing arithmetic on a period whose shape has not been decided.

    Activity selection is last and is almost entirely code — it scores the
    Pedagogy Library candidates context assembly looked up, rotates section
    formats to keep a chapter from repeating itself, and only calls a model for
    the topics where the library had nothing to offer. It is in this stage rather
    than its own because it reads the plan and writes the selection the generator
    needs, and a node that usually makes no model call at all does not need a
    checkpoint of its own.
    """
    out: dict = {}

    out = _merge(out, await experience_node(_view(state, out)))
    if _failed(out):
        return out

    out = _merge(out, await planning_node(_view(state, out)))
    if _failed(out):
        return out

    return _merge(out, await activity_selection_node(_view(state, out)))
