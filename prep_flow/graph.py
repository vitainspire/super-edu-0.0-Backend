"""The LangGraph orchestrator: two graphs, and the routers between their nodes.

                        ┌──────────────┐
        START ─────────>│  sequencing  │───(chapter unusable)──┐
                        └──────┬───────┘                       │
                               v                               │
                  ┌─────────────────────────┐                  │
                  │      curriculum         │  moves, then     │
                  │                         │  knowledge, then │
                  │  moves / knowledge /    │  the chain the   │
                  │  chain + mastery audit  │  first two feed  │
                  └────────────┬────────────┘                  │
                               v                               │
                  ┌─────────────────────────┐                  │
                  │     lesson design       │  the path, the   │
                  │                         │  period around   │
                  │  experience / planning  │  it, and the     │
                  │  / activity selection   │  activity for it │
                  └────────────┬────────────┘                  │
                               v                               │
                     ┌─────────────┐<───────────────────────────┘
                     │   persist   │  build the contract, write the rows
                     └──────┬──────┘
                            v
                           END

FOUR NODES. This is Node 1 of the master orchestration — textbook representation
and transformation — and it now ends where its name says it ends: with a
structured academic contract (`prep_flow/contract.py`), not with a sheet.

WHAT LEFT, and where it went. Generation, validation, the learner simulation and
the repair loop were four of the eight nodes this graph used to run. They are
archived under `_deferred/` and claimed by later nodes:

  * generation belongs downstream of NODE 2 (contextual reinforcement), because
    a sheet cannot be composed until the route through it has been adapted to
    the room it will be taught in. Writing it here and patching it afterwards
    was the alternative, and it is the one the framework's §7 rejects: Node 2
    returns a patch specification, not a second lesson.
  * validation and the learner gate belong to NODE 3, because both of them are
    questions about generated material and there is none here to ask them of.

WHAT THAT COST, stated plainly where somebody will look for it: this graph no
longer has a loop. The repair budget, the abort ratio, the revision selection
and the three separate round counters were the whole reason `after_gate` and
`after_simulation` existed, and with nothing to repair there is nothing to
bound. A run is now four supersteps and a persist, and `recursion_limit_for`
below is correspondingly boring.

What is NOT lost is the evidence trail. `persist_node` still reverts nothing and
still writes a provenance trace, because the trace was never about materials —
it is about which stage read what, and every stage that remains still does.

Two properties survive the removal and are load-bearing:

  * every failure path leads to `persist`, never to a raised exception. A
    chapter with thirty usable topic contracts and a report on the ten that
    could not be derived is worth a great deal to the node downstream; a
    traceback is worth nothing.
  * a node is the unit of checkpointing, so a chapter that dies inside
    `curriculum` resumes at the start of `curriculum` rather than between two of
    its three sub-steps. The sub-steps check state and skip work that is already
    done — see `_needs_moves` and friends in stages.py — which recovers the
    property that actually mattered, that forty extractions are not paid for
    twice.
"""
import uuid
from typing import Optional

from langgraph.graph import END, START, StateGraph

from . import contract as contract_module
from . import db, provenance
from .agents.feedback import (
    feedback_optimization_node,
    pattern_matcher_node,
    telemetry_node,
)
from .agents.sequencing import sequencing_node
from .agents.stages import curriculum_node, lesson_design_node
from .state import DEFAULT_CONFIG, ChapterState, FeedbackState


# ── Routers ──────────────────────────────────────────────────────────────────
#
# One question, asked three times: did that stage leave anything to work with?
# There were two more routers here — `after_gate` and `after_simulation` — and
# both of them existed to decide whether a failing sheet was worth another
# rewrite. With generation and validation gone there are no sheets and no
# rewrites, so what is left is the check every stage always got.

def after_sequencing(state: ChapterState) -> str:
    if state.get("status") == "failed" or not state.get("topics"):
        return "persist"
    return "curriculum"


def after_curriculum(state: ChapterState) -> str:
    """On to designing the lessons, or out.

    A merged stage can fail part way through — no topics to read moves off, an
    extraction that took the whole chapter with it — and it returns rather than
    raises, so the check is the same one every stage here gets.
    """
    if state.get("status") == "failed" or not state.get("topics"):
        return "persist"
    return "lesson_design"


def after_lesson_design(state: ChapterState) -> str:
    if state.get("status") == "failed" or not state.get("topics"):
        return "persist"
    return "persist"


# ── Persist ──────────────────────────────────────────────────────────────────

async def persist_node(state: ChapterState) -> dict:
    """Build the contract, write everything out, settle on a final status.

    THE CONTRACT IS BUILT HERE, and here specifically, for the same reason the
    provenance trace is: this is the one point at which every stage's
    contribution to the state exists at once. A contract composed stage by stage
    would describe a chapter whose later stages had not run.

    `needs_review` rather than `failed` whenever any topic came out ready: a
    chapter with 34 usable topic contracts and 6 that could not be completed is a
    usable handoff with a to-do list, and calling that a failure would hide the
    34. What changed with the node split is what "usable" means — it used to be
    counted in generated sheets that passed a gate, and it is now counted in
    topics whose academic contract is complete enough for Node 2 to adapt and
    generation to write from. `contract.readiness` is that count, made once, in
    the module that knows what a complete topic is.
    """
    topics = state.get("topics") or []
    run_id = state.get("run_id")

    document = contract_module.build_chapter_contract(state)
    readiness = document.get("readiness") or {}
    ready = int(readiness.get("topicsReady") or 0)

    if state.get("status") == "failed" and not ready:
        status = "failed"
    elif not topics or not ready:
        status = "failed"
    elif not readiness.get("generationReady"):
        status = "needs_review"
    else:
        status = "validated"

    # Built unconditionally, from state alone — no DB round trip needed for this
    # part. This is the one point every node's contribution to `metrics` (and the
    # final topic count) exists at once, so the trace is told from what actually
    # happened rather than from a running commentary. Returned in state (so a
    # `--no-persist` CLI run can still render it) and, when persistence is on,
    # written to the row. The contract is passed in explicitly because it is
    # still local at this point — it reaches the graph's state only through this
    # node's return value, which happens after the trace is built.
    trace = provenance.build_run_provenance(
        {**state, "contract": document},
        background_task=bool(state.get("background_task")),
        run_id=run_id, thread_id=state.get("thread_id"))

    if run_id and db.configured():
        # Ids derived from (run_id, index) rather than random, so a resumed run
        # upserts its own rows instead of inserting a second T7 beside the first.
        topic_rows = []
        by_index = {row.get("index"): row for row in (document.get("topics") or [])}
        for spec in topics:
            index = spec["index"]
            topic_rows.append({
                **spec,
                "id": str(uuid.uuid5(uuid.NAMESPACE_URL, f"{run_id}/topic/{index}")),
                "plan": (state.get("plans") or {}).get(index) or {},
                "selection": _serialisable_selection(
                    (state.get("selections") or {}).get(index)),
                # The per-topic slice of the contract, stored beside the plan
                # that shaped it. Node 2 is handed the whole document; a reviewer
                # asking "what was T7's contract" wants one row.
                "contract": by_index.get(index) or {},
            })

        await db.save_topics(run_id, topic_rows)
        # Best-effort, like everything else here: a contract that cannot be
        # stored must not cost the run, because it is also being returned in the
        # state the caller already has.
        await db.save_contract(run_id, document)

        await db.finish_run(
            run_id, status,
            arc=state.get("chapter_arc"),
            topic_count=len(topics),
            metrics={**(state.get("metrics") or {}),
                     "contract_topics_ready": ready,
                     "contract_topics_total": int(readiness.get("topicsTotal") or 0),
                     "contract_generation_ready": bool(readiness.get("generationReady"))},
            error="; ".join((state.get("errors") or [])[:5]) or None,
            adaptive_version=(state.get("adaptive_state") or {}).get("_version"),
            provenance=trace,
        )

    # `contract` is returned so a caller reading the final state gets the handoff
    # without a database round trip — which is the whole point of a node that
    # ends in an artefact rather than in rows.
    return {"status": status, "contract": document, "run_provenance": trace}


def _serialisable_selection(selection: Optional[dict]) -> dict:
    """Drop the full activity template from the stored selection.

    The template's own row already lives in `activity_templates`; copying every
    field of it onto forty topic rows makes the run record several times larger
    than the material it describes, for data that is a join away.
    """
    if not selection:
        return {}
    activity = selection.get("activity") or {}
    return {
        "activity": {
            "id": activity.get("id"),
            "name": activity.get("name"),
            "category": activity.get("category"),
            "source": activity.get("source", "library" if activity.get("id") else "invented"),
        } if activity else None,
        "context": (selection.get("context") or {}).get("name"),
        "formats": selection.get("formats") or {},
        "candidateCount": selection.get("candidateCount", 0),
    }


# ── Graph construction ───────────────────────────────────────────────────────

def build_chapter_graph(checkpointer=None):
    graph = StateGraph(ChapterState)

    graph.add_node("sequencing", sequencing_node)
    graph.add_node("curriculum", curriculum_node)
    graph.add_node("lesson_design", lesson_design_node)
    graph.add_node("persist", persist_node)

    graph.add_edge(START, "sequencing")
    graph.add_conditional_edges("sequencing", after_sequencing,
                                {"curriculum": "curriculum", "persist": "persist"})
    # Sequencing settles which pages each period teaches from; curriculum then
    # reads, off those same pages, the order in which they explain themselves,
    # what they contain, and what mastering that content requires. Everything
    # after it inherits an order it did not choose — see prep_flow/moves.py.
    graph.add_conditional_edges("curriculum", after_curriculum,
                                {"lesson_design": "lesson_design", "persist": "persist"})
    # Curriculum settles WHAT must be learned; lesson design settles the path a
    # child takes to it, the object that makes that path happen, and the minutes
    # around both. Persist then states all of it as one contract — this used to
    # run on into generation, and the four nodes that did are in `_deferred/`.
    graph.add_conditional_edges("lesson_design", after_lesson_design,
                                {"persist": "persist"})
    graph.add_edge("persist", END)

    return graph.compile(checkpointer=checkpointer)


def build_feedback_graph(checkpointer=None):
    graph = StateGraph(FeedbackState)
    graph.add_node("telemetry", telemetry_node)
    graph.add_node("pattern_matcher", pattern_matcher_node)
    graph.add_node("optimization", feedback_optimization_node)

    graph.add_edge(START, "telemetry")
    graph.add_edge("telemetry", "pattern_matcher")
    graph.add_edge("pattern_matcher", "optimization")
    graph.add_edge("optimization", END)
    return graph.compile(checkpointer=checkpointer)


# ── Entry points ─────────────────────────────────────────────────────────────

def recursion_limit_for(topic_count: int, config: dict) -> int:
    """Enough supersteps for the four nodes, with slack.

    It used to be a real calculation: the generation window loop alone could
    exceed LangGraph's default of 25 on a 40-topic chapter, and the repair and
    learner rounds each added their own. All three loops left with generation
    and validation, so this is now a constant with room in it. The signature
    keeps both arguments — every caller passes them, and a Node 1 that grows a
    loop again will want them back.
    """
    return 12


async def run_chapter(
    *, grade: str, subject: str, chapter_title: str, chapter_markdown: str,
    chapter_number: int = None, page_start: int = None, page_end: int = None,
    chapter_figures: list = None,
    school_id: str = None, class_id: str = None,
    teacher_settings: dict = None, teacher_preferences: str = None,
    config: dict = None, run_id: str = None, thread_id: str = None,
    background_task: bool = False,
    graph=None, checkpointer=None,
) -> dict:
    """Derive one chapter's academic contract end to end. Returns the final state.

    The contract itself is `result["contract"]` — that is Node 1's output and the
    thing Node 2 is handed. Everything else in the returned state is the working
    surface it was built from.

    `thread_id` is what makes a run resumable: invoking again with the same
    thread_id continues from the last checkpoint rather than starting over.

    `background_task`: pass True when the caller is FastAPI's `BackgroundTasks`
    (prep_flow/routes.py::_run_in_background) rather than a foreground caller
    awaiting this coroutine directly. Purely descriptive — it changes nothing
    about how the graph runs — but it is the one fact persist_node cannot
    otherwise know, and it is exactly the fact "was this derived inside the
    request that asked for it" that a provenance trace needs to answer.
    """
    merged = {**DEFAULT_CONFIG, **(config or {})}
    run_id = run_id or str(uuid.uuid4())
    thread_id = thread_id or f"chapter:{run_id}"

    initial: ChapterState = {
        "run_id": run_id,
        "thread_id": thread_id,
        "background_task": background_task,
        "school_id": school_id,
        "class_id": class_id,
        "grade": str(grade),
        "subject": subject,
        "chapter_title": chapter_title,
        "chapter_number": chapter_number,
        "chapter_markdown": chapter_markdown,
        "page_start": page_start,
        "page_end": page_end,
        "chapter_figures": chapter_figures or [],
        "teacher_settings": {
            "duration": 45, "classSize": 40, "resourceLevel": 0,
            "language": "English", "learningObjective": "new lesson",
            "teachingStyle": "interactive", **(teacher_settings or {}),
        },
        "teacher_preferences": teacher_preferences,
        "config": merged,
        "status": "running",
        "plans": {},
        "selections": {},
        "metrics": {},
        "errors": [],
    }

    await db.create_run({
        "id": run_id, "thread_id": thread_id, "school_id": school_id,
        "class_id": class_id, "grade": str(grade), "subject": subject,
        "chapter_number": chapter_number, "chapter_title": chapter_title,
        "status": "running", "config": merged,
    })

    invoke_config = {
        "configurable": {"thread_id": thread_id},
        "recursion_limit": recursion_limit_for(int(merged.get("max_topics", 40)), merged),
    }

    if graph is not None:
        return await graph.ainvoke(initial, invoke_config)

    if checkpointer is not None:
        return await build_chapter_graph(checkpointer).ainvoke(initial, invoke_config)

    async with db.checkpointer() as saver:
        return await build_chapter_graph(saver).ainvoke(initial, invoke_config)


async def run_feedback_cycle(
    *, grade: str, subject: str, school_id: str = None, window_days: int = 40,
    graph=None, checkpointer=None,
) -> dict:
    """One turn of the feedback loop for a cohort. Safe to run on a schedule:
    thin data and an unchanged picture both resolve to "no new version".

    Still a separate graph on its own cadence, and still fed by telemetry rather
    than by this run — which is why the node split did not touch it. What DID
    change is upstream of it: the material teachers give feedback ON is now
    written after Node 2, so the loop's satisfaction rows describe sheets this
    package no longer produces. It keeps working; what it is measuring moved.
    """
    key = db.scope_key(school_id, str(grade), subject)
    initial: FeedbackState = {
        "scope_key": key, "school_id": school_id, "grade": str(grade),
        "subject": subject, "window_days": window_days, "errors": [],
    }
    invoke_config = {"configurable": {"thread_id": f"feedback:{key}"},
                     "recursion_limit": 12}

    if graph is not None:
        return await graph.ainvoke(initial, invoke_config)
    if checkpointer is not None:
        return await build_feedback_graph(checkpointer).ainvoke(initial, invoke_config)
    async with db.checkpointer() as saver:
        return await build_feedback_graph(saver).ainvoke(initial, invoke_config)
