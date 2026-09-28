"""Node 3 — validation.

    prepare ─> structure ─> learner ─> realism ─> integrity ─> verdict
                  │            │          │           │
       structure, │  could a   │  could   │  did the  │
       grounding, │  child get │  this    │  pipeline │
       continuity,│  there     │  run in  │  do what  │
       grade fit  │  from this │  THE     │  it said  │
                  │            │  room    │

SIX NODES, THREE OF THEM FREE. `structure` makes one chapter-level consistency
call, `learner` and `realism` make roughly one per sheet each, and the other
three make none.

`learner` and `realism` ask different questions and a sheet can fail either
alone: the first asks whether the sheet TEACHES, the second whether the period
could HAPPEN — in a rural Indian government school, with one teacher, 30-60
children, a blackboard and the textbook, and nothing else guaranteed.

WHY THE ORDER IS FIXED. `structure` runs before `learner` for the reason the old
in-package graph gave and which did not change with the split: judging whether a
sheet TEACHES is only worth paying for once the sheet is actually a sheet. A
batch still missing sections would be evaluated on its holes, diagnosed for its
holes, and repaired for reasons the structural pass had already stated more
cheaply.

`integrity` runs after both because it is the only pass that needs to know what
the others concluded — and last-but-one rather than last because `verdict` has to
fold its findings in.

WHAT IS NOT HERE, AND WHY THAT IS THE POINT. There is no repair node and no loop.
The old pipeline ran validation → repair → validation → simulation → repair,
with three separate round counters and a revision-selection pass to decide which
attempt shipped. All of that belonged to generation, and generation is a
different node now. This one names the sections that need rewriting and stops.

The consequence is worth stating plainly: THIS NODE IS STATELESS AND
IDEMPOTENT. Call it, act on the verdict, regenerate, call it again. The old
loop's verdict depended on how many rounds had already been spent; this one
depends only on the material in front of it, which is what makes a verdict
reproducible months later from a logged contract and a logged sheet.
"""
import uuid
from typing import Optional

from langgraph.graph import END, START, StateGraph

from . import adapter as adapter_module
from . import checks as checks_module
from . import diagnostic as diagnostic_module
from . import integrity as integrity_module
from . import learner as learner_module
from . import realism as realism_module
from . import verdict as verdict_module
from .state import DEFAULT_CONFIG, ValidationState


# ── 1. Prepare ───────────────────────────────────────────────────────────────

async def prepare_node(state: ValidationState) -> dict:
    """Rebuild the shape the revived checks read. No model, no I/O.

    Built ONCE and threaded through. `checks.py` and `learner.py` must judge the
    same material against the same contract, and adapting the inputs twice is how
    two passes come to disagree about which topic T7 is.
    """
    contract = state.get("contract") or {}
    materials = {int(k): v for k, v in (state.get("materials") or {}).items()}
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}

    sources = state.get("sources") or {}
    chapter_text = state.get("chapter_text") or ""
    if not sources and chapter_text:
        # The convenience path: a caller holding the whole chapter should not
        # have to slice it per topic to get the grounding checks.
        sources = adapter_module.sources_from_chapter(contract, chapter_text)

    adapted = adapter_module.to_state(
        contract, materials=materials, sources=sources,
        chapter_text=chapter_text, config=config)

    grounded = sum(1 for spec in adapted["topics"] if (spec.get("excerpt") or "").strip())
    return {
        "adapted": adapted,
        "sources": sources,
        "metrics": {
            "topics": len(adapted["topics"]),
            "materials": len(materials),
            "topics_with_source": grounded,
            # The number that decides `coverage.groundingChecked` on the verdict.
            # Reported even when it is zero — especially when it is zero.
            "grounding_checked": bool(grounded),
        },
    }


# ── 2. Structure, grounding, continuity, grade fit ───────────────────────────

async def structure_node(state: ValidationState) -> dict:
    """The cheap gate, run first. `checks.py`, unchanged from `_deferred/`."""
    adapted = state.get("adapted") or {}
    if not adapted.get("materials"):
        return {"issues": {}, "batch_issues": [], "repair_targets": [],
                "metrics": {"blocking_findings": 0, "advisory_findings": 0}}

    out = await checks_module.validation_node(adapted)
    issues = out.get("issues") or {}
    blocking = sum(1 for findings in issues.values()
                   for f in findings if f.get("severity") == "blocking")
    advisory = sum(len(f) for f in issues.values()) - blocking

    # TARGETS ARE DERIVED HERE, from the findings, rather than taken from what
    # `validation_node` returned — and that is a deliberate divergence from the
    # module this node revived.
    #
    # In the old in-package pipeline, `validation_node` emptied its own target
    # list once the failure ratio passed `repair_abort_ratio`, and that was
    # right: it was about to spend a bounded repair budget, and re-rolling topic
    # by topic on a batch where most sheets fail burns the budget without
    # changing the outcome. The list was a SPENDING DECISION.
    #
    # Here it is ADVICE, and this node spends nothing. Suppressing the advice
    # exactly when the most work is needed is backwards: a chapter where every
    # sheet failed is a chapter where a person most needs the list of what
    # failed. Caught by a test — a one-topic chapter with one blocking finding
    # trips a 0.6 ratio at 1.0, and the repair brief came back empty on the only
    # topic there was.
    #
    # The ratio itself is still computed and still reported, because "most of
    # this batch failed, look at the prompt rather than the sheets" is a true and
    # useful thing to say. It just no longer deletes the evidence for it.
    targets = sorted(index for index, findings in issues.items()
                     if any(f.get("severity") == "blocking" for f in findings))
    sections = {index: [] for index in targets}

    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    topics = len(adapted.get("topics") or []) or 1
    ratio = len(targets) / topics
    wholesale = ratio >= float(config.get("failure_alarm_ratio", 0.6))
    if wholesale:
        print(f"[validation_flow] {len(targets)}/{topics} topics have blocking "
              f"findings ({ratio:.0%}) — at this rate the problem is the prompt "
              f"or the inputs rather than the individual sheets. The per-topic "
              f"list below is still complete.")

    return {
        "issues": issues,
        "batch_issues": out.get("batch_issues") or [],
        "repair_targets": targets,
        # Empty list per target means "the whole sheet", which is what a
        # structural failure gets: a sheet missing sections has no narrower
        # surface to aim at. The learner gate narrows it where it can.
        "repair_sections": sections,
        "errors": out.get("errors") or [],
        "metrics": {**(out.get("metrics") or {}),
                    "blocking_findings": blocking,
                    "advisory_findings": advisory,
                    "structural_failure_ratio": round(ratio, 3),
                    # Renamed from the old `repair_aborted_wholesale`, because
                    # nothing is aborted any more. It is an alarm, not an action.
                    "structural_failure_wholesale": wholesale},
    }


# ── 3. The learner gate ──────────────────────────────────────────────────────

async def learner_node(state: ValidationState) -> dict:
    """Could a child get there from this sheet? `learner.py`, unchanged.

    Skipped when `simulate_learner` is off. Skipping is legitimate and cheap —
    roughly one call per sheet — but a skipped gate is recorded rather than
    absorbed: `coverage.learnerGateRan` on the verdict says which happened, and
    `verdict._topic_verdict` will not say `ship` for a sheet nothing judged.
    """
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    if not config.get("simulate_learner", True):
        return {"learner": {}, "metrics": {"learner_gate_ran": False}}

    adapted = {**(state.get("adapted") or {}), "config": config}
    if not adapted.get("materials"):
        return {"learner": {}, "metrics": {"learner_gate_ran": False}}

    out = await learner_module.simulation_node(adapted)
    # The learner gate names sections too, and its names are narrower than a
    # structural failure's — a diagnosis says "the Concept and the Challenge",
    # where a missing section says "the whole sheet". MERGED rather than
    # replaced: a sheet that failed both needs the structural rewrite, and the
    # union is the honest surface.
    sections = dict(state.get("repair_sections") or {})
    for index, named in (out.get("repair_sections") or {}).items():
        if index in sections and not sections[index]:
            continue                    # already "the whole sheet"
        sections[index] = sorted(set(sections.get(index) or []) | set(named or []))

    targets = sorted(set(state.get("repair_targets") or []) |
                     set(out.get("repair_targets") or []))
    return {
        "learner": out.get("learner") or {},
        "repair_targets": targets,
        "repair_sections": sections,
        "errors": out.get("errors") or [],
        "metrics": {**(out.get("metrics") or {}), "learner_gate_ran": True},
    }


# ── 3b. The classroom-reality gate ───────────────────────────────────────────

async def realism_node(state: ValidationState) -> dict:
    """Could this period actually run in a rural Indian government school?
    `realism.py`. Roughly one call per sheet, like the learner gate.

    Runs AFTER the learner gate for the same reason the learner gate runs after
    `structure`: asking whether a sheet could run in the room is only worth
    paying for once something has said the sheet teaches at all.

    Skipped when `verify_realism` is off, and a skip is recorded rather than
    absorbed — `metrics.realism_gate_judged` counts sheets actually judged, so a
    quiet gate never reads as a passing one.
    """
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    if not config.get("verify_realism", True):
        return {"metrics": {"realism_gate_judged": 0, "realism_gate_ran": False}}

    adapted = {**(state.get("adapted") or {}), "config": config}
    if not adapted.get("materials"):
        return {"metrics": {"realism_gate_judged": 0, "realism_gate_ran": False}}

    out = await realism_module.run(adapted)

    # MERGED BY HAND, not by the state reducer. `issues` and `repair_sections`
    # are annotated with `merge_by_index`, which does dict.update() — so
    # returning {3: [...]} for a topic `structure` already filed findings against
    # would REPLACE the structural findings rather than add to them. The verdict
    # would then be built from a sheet's realism problems alone.
    issues = {index: list(findings) for index, findings in (state.get("issues") or {}).items()}
    for index, findings in (out.get("issues") or {}).items():
        issues[index] = (issues.get(index) or []) + list(findings)

    sections = dict(state.get("repair_sections") or {})
    for index, named in (out.get("repair_sections") or {}).items():
        if index in sections and not sections[index]:
            continue                    # already "the whole sheet"
        sections[index] = sorted(set(sections.get(index) or []) | set(named or []))

    targets = sorted(set(state.get("repair_targets") or []) |
                     set(out.get("repair_targets") or []))

    return {
        "issues": issues,
        "repair_targets": targets,
        "repair_sections": sections,
        "errors": out.get("errors") or [],
        "metrics": {**(out.get("metrics") or {}), "realism_gate_ran": True},
    }


# ── 3c. The diagnostic-thinking gate ─────────────────────────────────────────

async def diagnostic_node(state: ValidationState) -> dict:
    """Where a real gap was named for this topic, did the sheet actually
    deliver an if-then response for it? `diagnostic.py`. Only judges topics
    that named a gap -- most run zero calls here.

    Runs after realism for the same reason realism runs after learner: each
    gate is only worth paying for once the ones before it have had their say.
    Skipped when `verify_diagnostic` is off (default on).
    """
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    if not config.get("verify_diagnostic", True):
        return {"metrics": {"diagnostic_gate_ran": False}}

    adapted = {**(state.get("adapted") or {}), "config": config}
    if not adapted.get("materials"):
        return {"metrics": {"diagnostic_gate_ran": False}}

    out = await diagnostic_module.run(adapted)

    issues = {index: list(findings) for index, findings in (state.get("issues") or {}).items()}
    for index, findings in (out.get("issues") or {}).items():
        issues[index] = (issues.get(index) or []) + list(findings)

    sections = dict(state.get("repair_sections") or {})
    for index, named in (out.get("repair_sections") or {}).items():
        if index in sections and not sections[index]:
            continue
        sections[index] = sorted(set(sections.get(index) or []) | set(named or []))

    targets = sorted(set(state.get("repair_targets") or []) |
                     set(out.get("repair_targets") or []))

    return {
        "issues": issues,
        "repair_targets": targets,
        "repair_sections": sections,
        "errors": out.get("errors") or [],
        "metrics": {**(out.get("metrics") or {}), "diagnostic_gate_ran": True},
    }


# ── 4. Did the pipeline do what it said? ─────────────────────────────────────

async def integrity_node(state: ValidationState) -> dict:
    """Target preservation and context adoption — the two hooks the earlier
    nodes left for this one. No model.

    It also closes a third hook, which is the only I/O this whole node does.
    `check_context_adoption` decides, per adaptation, whether the sheet shows any
    trace of it; the reinforcement loop cannot attribute a teacher's rating
    without that answer, and until now it existed only as a rate and a sentence.
    Writing it back to the stored adaptation row is what turns this pass from a
    report into the loop's evidence.

    THE WRITE IS BEST-EFFORT AND DELIBERATELY LAST. A verdict is a judgement
    about material and stands whether or not a database accepted it — so a failed
    write costs the loop one run's evidence and costs this node nothing. It never
    changes the verdict, and it never raises.
    """
    findings, metrics = integrity_module.run(
        state.get("contract") or {},
        materials={int(k): v for k, v in (state.get("materials") or {}).items()},
        plan=state.get("plan"))

    written = await _record_adoption(state.get("plan"), metrics.pop("verdicts", None))
    if written:
        metrics["adoptionVerdictsStored"] = written

    return {"integrity_findings": findings, "metrics": metrics}


async def _record_adoption(plan: Optional[dict], verdicts: Optional[dict]) -> int:
    """Write each adaptation's adoption verdict back to its stored row.

    Silent in all three ways it can legitimately do nothing: no plan (Node 2
    ran in shadow mode or not at all), no `contextRunId` (Node 2's persistence
    was off or its insert failed), or no verdicts (nothing accepted, or nothing
    with a stable ordinal to address). None of these is an error, and none is
    worth a log line on a node that runs on every chapter.

    context_flow is imported here rather than at module scope. Node 3 does not
    otherwise depend on Node 2's package at all — it reads a plan, which is a
    document — and a module-level import would make that true only by accident,
    turning a missing sibling package into a validation pass that cannot start.
    """
    context_run_id = (plan or {}).get("contextRunId")
    if not context_run_id or not verdicts:
        return 0
    try:
        from context_flow import store as context_store
    except Exception as exc:                # noqa: BLE001 - reported, never raised
        print(f"[validation_flow:integrity] adoption verdicts could not be "
              f"stored — context_flow.store is unavailable ({str(exc)[:120]}). "
              f"The verdict stands; the reinforcement loop loses this run.")
        return 0
    return await context_store.record_adoption(context_run_id, verdicts)


# ── 5. The verdict ───────────────────────────────────────────────────────────

async def verdict_node(state: ValidationState) -> dict:
    config = {**DEFAULT_CONFIG, **(state.get("config") or {})}
    metrics = state.get("metrics") or {}
    document = verdict_module.build(
        contract=state.get("contract") or {},
        materials={int(k): v for k, v in (state.get("materials") or {}).items()},
        issues=state.get("issues") or {},
        batch_issues=state.get("batch_issues") or [],
        integrity_findings=state.get("integrity_findings") or [],
        learner=state.get("learner") or {},
        metrics=metrics,
        repair_targets=state.get("repair_targets") or [],
        repair_sections=state.get("repair_sections") or {},
        grounding_checked=bool(metrics.get("grounding_checked")),
        learner_ran=bool(metrics.get("learner_gate_ran")),
        plan=state.get("plan"),
        run_id=state.get("run_id"),
    )
    return {"verdict": document, "status": document["verdict"]}


# ── Graph construction ───────────────────────────────────────────────────────

def build_validation_graph(checkpointer=None):
    graph = StateGraph(ValidationState)

    graph.add_node("prepare", prepare_node)
    graph.add_node("structure", structure_node)
    graph.add_node("learner", learner_node)
    graph.add_node("realism", realism_node)
    graph.add_node("diagnostic", diagnostic_node)
    graph.add_node("integrity", integrity_node)
    graph.add_node("verdict", verdict_node)

    graph.add_edge(START, "prepare")
    graph.add_edge("prepare", "structure")
    # Structural first, always: judging whether a sheet teaches is only worth
    # paying for once the sheet is actually a sheet. Realism follows the learner
    # gate for the same reason one step further out — whether the period could
    # run in the room matters once the sheet is one that teaches. Diagnostic
    # runs last of the judged gates — it only fires on topics that named a
    # gap, so it is cheapest to leave for last.
    graph.add_edge("structure", "learner")
    graph.add_edge("learner", "realism")
    graph.add_edge("realism", "diagnostic")
    graph.add_edge("diagnostic", "integrity")
    graph.add_edge("integrity", "verdict")
    graph.add_edge("verdict", END)

    return graph.compile(checkpointer=checkpointer)


# ── Entry point ──────────────────────────────────────────────────────────────

async def run_validation(
    *, contract: dict, materials: dict,
    plan: Optional[dict] = None, sources: Optional[dict] = None,
    chapter_text: str = "", config: Optional[dict] = None,
    run_id: str = None, thread_id: str = None,
    graph=None, checkpointer=None,
) -> dict:
    """Judge one chapter's generated material. Returns the final state.

    The verdict is `result["verdict"]`; `shippable(result)` is the shorter
    question. Everything else in the returned state is the working surface it
    was built from.

    `sources` (index -> textbook excerpt) or `chapter_text` turn on the two
    grounding checks. Without either they report an advisory rather than passing
    quietly, and `verdict["coverage"]["groundingChecked"]` records which happened.
    """
    merged = {**DEFAULT_CONFIG, **(config or {})}
    run_id = run_id or str(uuid.uuid4())

    initial: ValidationState = {
        "run_id": run_id,
        "thread_id": thread_id or f"validation:{run_id}",
        "contract": contract or {},
        "materials": materials or {},
        "plan": plan,
        "sources": sources or {},
        "chapter_text": chapter_text,
        "config": merged,
        "status": "running",
        "issues": {},
        "batch_issues": [],
        "repair_targets": [],
        "repair_sections": {},
        "learner": {},
        "integrity_findings": [],
        "metrics": {},
        "errors": [],
    }
    invoke_config = {"configurable": {"thread_id": initial["thread_id"]},
                     "recursion_limit": 12}

    if graph is not None:
        return await graph.ainvoke(initial, invoke_config)
    return await build_validation_graph(checkpointer).ainvoke(initial, invoke_config)


def shippable(state: dict) -> list[int]:
    """The topics that may be published, by index.

    A LIST, NOT A BOOLEAN, because the chapter-level verdict is almost never the
    useful answer: thirty-four good sheets and six flagged ones is a usable
    chapter, and a caller that asked "may I ship this?" and got False would
    withhold all forty.
    """
    document = state.get("verdict") or {}
    return [row["index"] for row in (document.get("topics") or [])
            if row.get("verdict") == verdict_module.SHIP]


def repair_brief(state: dict) -> dict:
    """What to hand back to Generation: which topics, which sections, and why.

    The whole of this node's advice to the stage that has to act on it. Empty
    `sections` means the whole sheet — what a structural failure gets, since a
    sheet missing sections has no narrower surface to aim at.

    The findings travel with it. A repair prompt told only WHICH sections to
    rewrite rewrites them to the same standard that failed; the finding text is
    the only thing in this brief that says what was actually wrong.
    """
    document = state.get("verdict") or {}
    rows = {r["index"]: r for r in (document.get("topics") or [])}
    return {
        "runId": document.get("runId"),
        "contractFingerprint": document.get("contractFingerprint"),
        "topics": [
            {
                "index": index,
                "sections": rows.get(index, {}).get("repairSections") or [],
                "why": [f.get("message") for f in (rows.get(index, {}).get("findings") or [])
                        if f.get("severity") == "blocking"],
                "learnerDiagnosis": ((rows.get(index, {}).get("learner") or {})
                                     or {}).get("diagnosis") or [],
            }
            for index in (document.get("repairTargets") or [])
        ],
    }
