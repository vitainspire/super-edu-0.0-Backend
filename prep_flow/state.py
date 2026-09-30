"""Graph state, and the reducers that let nodes write to it concurrently.

Two graphs share this module: Node 1's chapter flow (ChapterState) and the
feedback loop beside it (FeedbackState). They are separate graphs on purpose —
the feedback loop runs on its own cadence over ~40 days of telemetry and must
not be a step inside a chapter run that happens to be waiting on it. They meet
through one row: the active adaptive state, which context assembly reads and
feedback writes.

WHAT THIS STATE STOPPED CARRYING. `materials`, `issues`, `learner`, the three
histories, the repair counters and the batch findings were all here until the
master orchestration was split into nodes. Every one of them described a
generated sheet or a verdict on one, and Node 1 produces neither — it produces
the academic contract that generation is later written FROM (`contract.py`).
The fields are removed rather than left unused: a TypedDict key that nothing
writes is a key the next reader assumes is merely empty. `_deferred/README.md`
says which node claimed each of the agents that used them.
"""
import operator
from typing import Annotated, Literal, Optional, TypedDict


def merge_by_index(left: dict, right: dict) -> dict:
    """Reducer for the index-keyed maps (materials, issues, selections).

    A plain dict field in LangGraph is REPLACED by whatever a node returns, so
    a node that derives three topics out of forty would wipe the other
    thirty-seven. Keying by topic index and merging means a node can return only
    what it changed — which is what lets a resumed run top up the topics whose
    work is missing without re-deriving the ones already in state.
    """
    merged = dict(left or {})
    merged.update(right or {})
    return merged


class TopicSpec(TypedDict, total=False):
    """One topic in the chapter's ordered sequence — the unit T1…T40."""
    index: int                      # 1-based position in the chapter
    topic: str
    subtopic: str
    # Derived from the stretch of chapter this topic was actually given. No
    # model is consulted: sequencing cuts on boundaries the book printed and
    # reads these off the result — see sequencing.derive_topics. Every
    # (Page N) on the sheet and every grounding check is measured against
    # these, so they describe `excerpt` or they are wrong.
    page_start: Optional[int]
    page_end: Optional[int]
    anchor_kind: str                # 'heading' | 'pages' | 'proportional'
    excerpt: str                    # verbatim textbook for THIS topic
    images: list[dict]
    figures: list[dict]             # the figures printed on THIS topic's pages
    # Filled by move extraction — the order the BOOK explains this topic in.
    # [{ord, type, gist, page, section, fidelity, verbatim?}, ...] in printed
    # order. moves.teaching_order() turns it into this topic's section order,
    # which is why it must exist before planning allocates minutes.
    moves: list[dict]
    moves_source: str               # 'extracted' | 'headings' | 'none'
    # Filled by context assembly
    knowledge: dict                 # concepts/competencies/vocabulary/contexts/bloom/difficulty
    canonical: dict                 # kind -> {raw name: canonical id}
    canonical_names: dict           # kind -> [raw name, ...] resolved (for display/provenance)
    # kind -> {"created": [...], "matchedExisting": [...], "matchedViaLLM": [...]} —
    # which of THIS topic's names actually grew the shared library. See
    # provenance.py; competencies are absent (read-only matched, never created).
    canonical_seeded: dict
    activities: list[dict]          # Pedagogy Library candidates
    # Filled by reasoning — this topic's slice of the chapter's knowledge chain,
    # and the mastery audit that runs beside it:
    #   {gained, bridgesTo, assumes, misconceptions,
    #    masteryTarget, supplied, missing}
    # `gained` is what THIS PERIOD leaves; `masteryTarget` is what the idea
    # requires of a child who has to decide a case nobody showed them. They are
    # different fields because a sheet written to the first alone teaches the
    # page's own example — see the mastery pass in agents/reasoning.py.
    # `supplied` is what the printed page already gives, and `missing` is the
    # audited shortfall list, each entry {kind, missing} with kind drawn from
    # reasoning.MASTERY_KINDS. All three are empty on a chapter whose reasoning
    # was cached before the pass existed and whose top-up failed, which every
    # reader treats as "not derived".
    reasoning: dict
    # Filled by the experience agent — the cognitive path and the object that
    # makes it happen. Generation may not run without one.
    #
    # Also where the audited shortfall this period actually closes is recorded:
    # `gap` is the one chosen from the reasoning slice's `missing`, `gapKind` is
    # that entry's kind, and `gapCloser` is what stages it in a room of forty. The
    # audit finds them, this chooses one — reasoning has the excerpt and this has
    # the trajectory, and choosing needs the trajectory.
    experience: dict
    # Filled by planning
    plan: dict
    # Filled by activity selection
    selection: dict                 # {activity, context, rationale}


class ChapterState(TypedDict, total=False):
    # ── Inputs ───────────────────────────────────────────────────────────────
    run_id: str
    thread_id: Optional[str]
    school_id: Optional[str]
    class_id: Optional[str]
    grade: str
    subject: str
    chapter_title: str
    chapter_number: Optional[int]
    chapter_markdown: str           # the whole chapter, verbatim
    page_start: Optional[int]
    page_end: Optional[int]
    chapter_images: list[dict]
    chapter_figures: list[dict]     # id/page/caption/path from a classified ingest
    teacher_settings: dict
    teacher_preferences: Optional[str]
    config: dict                    # window size, topic count bounds, retry budget
    # Whether THIS run is executing inside a FastAPI BackgroundTasks job
    # (prep_flow/routes.py::_run_in_background) or in the foreground (the CLI,
    # or a direct run_chapter() call). Read once, at persist time, by
    # provenance.build_run_provenance() — see provenance.py. Defaults to False
    # (foreground) when a caller does not set it, which is the CLI's real
    # behaviour: it awaits the graph directly, nothing schedules it afterwards.
    background_task: bool

    # ── Sequencing ───────────────────────────────────────────────────────────
    topics: list[TopicSpec]
    sequencing_note: str
    # When True with `topics` already populated, sequencing_node trusts them
    # rather than re-deriving — the viewer's topic picker already ran this LLM
    # call once, and a second one could disagree with what the teacher just
    # reordered by hand.
    skip_sequencing: bool

    # ── Context assembly ─────────────────────────────────────────────────────
    adaptive_state: dict            # what the feedback loop learned; {} on a cold start
    class_context: dict             # interests, weak topics, teaching profile
    engagement_guidance: str
    context_ready: bool
    unmapped_competencies: list[str]   # real library coverage gaps

    # ── Curriculum reasoning ─────────────────────────────────────────────────
    # What the textbook does not say. Chapter-wide, written once, cached across
    # runs: {prerequisites, anchors, misconceptions, chain: {index -> entry}}.
    # The per-topic slice is copied onto each TopicSpec; this is the whole.
    reasoning: dict

    # ── Experience plans ─────────────────────────────────────────────────────
    # index -> {trajectory, conceptualJump, anchor, studentAction, inference, …}
    # The boundary the design draws: no bullet is written without one of these.
    experience: Annotated[dict, merge_by_index]

    # ── Planning ─────────────────────────────────────────────────────────────
    chapter_arc: str
    plans: Annotated[dict, merge_by_index]        # index -> plan

    # ── Activity selection ───────────────────────────────────────────────────
    selections: Annotated[dict, merge_by_index]   # index -> {activity, context}

    # ── The contract ─────────────────────────────────────────────────────────
    # Node 1's output, built by persist_node from everything above — see
    # contract.py. Present in the returned state whether or not the run was
    # persisted, because a node that ends in an artefact should hand the caller
    # the artefact rather than a row id to go and fetch it with.
    contract: dict

    # WHAT USED TO BE HERE, between activity selection and bookkeeping: the
    # generated sheets (`materials`, `material_history`, `generated_order`,
    # `cursor`), the validator's findings and the repair loop (`issues`,
    # `validation_history`, `batch_issues`, `repair_targets`, `repair_sections`,
    # `repair_round`, `selection`), and the cognitive learner gate (`learner`,
    # `learner_history`, `sim_round`). Before those, a teacher-readiness gate
    # (`readiness`, `readiness_history`, `readiness_round`).
    #
    # The learner and readiness gates were removed for cost, one after the other,
    # and each removal said what stopped being measured. THIS removal is a
    # different kind: nothing stopped being measured, the measuring moved. Every
    # one of those fields describes a generated sheet, sheets are now written
    # downstream of Node 2, and Node 3 judges them there. `_deferred/README.md`
    # says which node claimed each agent.
    #
    # The consequence for a reader of this state: `status == "validated"` no
    # longer means "the sheets passed". It means every topic's contract came out
    # complete enough to generate from — see contract.readiness and
    # graph.py::persist_node.

    # ── Bookkeeping ──────────────────────────────────────────────────────────
    status: Literal["pending", "running", "validated", "needs_review", "failed"]
    errors: Annotated[list, operator.add]
    metrics: Annotated[dict, merge_by_index]
    # The run-wide trace persist_node builds from the final state — see
    # provenance.py. Present in the returned state whether or not the run was
    # actually persisted, so a `--no-persist` CLI run can still render it.
    run_provenance: dict


class FeedbackState(TypedDict, total=False):
    scope_key: str                  # "{school}:{grade}:{subject}"
    school_id: Optional[str]
    grade: str
    subject: str
    window_days: int

    responses: list[dict]           # raw YES/SOMEWHAT/NO rows from telemetry
    sample_size: int
    patterns: list[dict]            # what the matcher found
    aggregates: dict                # the numbers the patterns were drawn from

    previous_state: dict            # the adaptive state currently in force
    adaptive_state: dict            # the new one
    changelog: list[str]
    applied: bool
    liveness: dict                  # did the new state actually reach a prompt
    gate: dict                      # regression-gate verdict, when the gate ran
    errors: Annotated[list, operator.add]


DEFAULT_CONFIG = {
    # A chapter is split into this many teachable topics. The diagram's
    # "T1 -> T30/40" is the real target; the bounds keep a thin chapter from
    # being padded into nonsense and a fat one from becoming unteachable.
    "min_topics": 8,
    "max_topics": 40,
    "target_topics": 30,
    # Topics per move-extraction call. The excerpt is sent once per topic
    # whatever this is, so a bigger window buys progressively less while the
    # chance of a truncated response — which loses the LAST topics in the window
    # silently — grows. 1 restores the per-topic call the pipeline used before
    # the curriculum stage was merged. See agents/move_extraction.py.
    "moves_window": 4,
    "concurrency": 4,
    "no_repeat_window": 5,          # activity variety: no repeat within N topics

    # ── The Deep Agent layer (deep_agents/) ──────────────────────────────────
    #
    # OFF, and it stays off until a golden-set comparison says otherwise — the
    # same stance `context_flow` takes on shadow mode, for the same reason. With
    # this False the two seams that can use an agent (curriculum reasoning,
    # experience design) make the structured `call_json` they always made, and
    # this package behaves exactly as it did before `deep_agents/` existed.
    #
    # With it True those two calls are made by an agent that can read the pages
    # it decides it needs. Everything around them — the chain gate, the
    # re-derive, the mastery pass, the caching refusal, the anchor snap-back —
    # is unchanged, which is what makes an A/B of the two meaningful.
    "deep_agents": False,
    # Model steps per agent invocation before it is told to answer with what it
    # has. Replaces ai.py's circuit breaker, which the agent path cannot use —
    # see deep_agents/middleware.py::StepBudget.
    "deep_agents_max_steps": 14,        # curriculum reasoning
    "deep_agents_max_steps_design": 10,  # experience design
    # A directory to mount /workspace/ on, for a local run whose artifacts
    # somebody wants to open. None keeps them in graph state, which is what a
    # served request must use — deep_agents/workspace.py says why.
    "deep_agents_workspace": None,
    # Whether an agent may pause for human approval instead of being refused
    # outright when it tries to write the pedagogy library. A background job has
    # nobody to ask, so this must stay False there.
    "deep_agents_hitl": False,
    # Whether the curriculum agent is offered `write_canonical_knowledge` at all.
    # It grows a library every school reads; see deep_agents/tools/knowledge.py.
    "deep_agents_library_writes": False,

    # WHAT WENT WITH GENERATION AND VALIDATION. `window_size` (topics per
    # generation call), `include_visuals`, `max_repair_rounds`,
    # `repair_abort_ratio`, `simulate_learner`, `max_sim_rounds`,
    # `learner_pass_threshold`, `zero_dimension_fails` and `gate_transfer` were
    # all here. Every one of them tuned a stage that now runs after Node 2 or
    # inside Node 3, and a knob left in a config that no longer reaches the code
    # it names is worse than an absent one — it reads as a setting somebody
    # already chose. They belong with their stages; see `_deferred/README.md`.
    #
    # Callers that still pass them are not broken: `run_chapter` merges whatever
    # config it is given over these defaults, so an unknown key is carried in
    # state and ignored rather than rejected.
}
