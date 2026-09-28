"""The agent-by-agent data-flow trace: which agent produced what, and which
agent reads it next.

`provenance.live_line()` says "this stage ran and did roughly this much".
`terminal.py` says "here is the actual content that stage produced". Neither
answers the question this module exists for: **where does an agent's output
GO** -- which downstream agent picks it up, under which state key, and what
that agent does with it. On a run with four nodes and seven different
LLM-calling agents that is the only view that makes a wrong contract traceable
back to the stage that caused it.

PIPELINE BELOW HOLDS BOTH NODES AND THE AGENTS INSIDE THEM, since two nodes are
merged (prep_flow/agents/stages.py). The merged nodes are what a stream of the
graph reports, so they are what `stage()` is called with and they need entries;
the agents they run are still separately worth describing, still name each other
in their `feeds`, and are still what `owner_from_stack()` attributes an LLM call
to. Nothing iterates this map expecting it to be exactly the graph: the timing
ledger skips any key with no recorded visits.

`feeds` ENTRIES POINTING PAST NODE 1 ARE KEPT AND MARKED. Generation, validation
and the learner gate left this package when the master orchestration was split
into nodes (see `_deferred/README.md`), but "the book's move order is what the
sheet's sections are emitted in" is still true and is still the reason move
extraction exists. A trace that stopped naming those consumers would make every
Node 1 stage look like it produced something nobody reads. They are labelled
NODE 2 / GENERATION / NODE 3 so a reader can tell which of them runs here.

Three things come out of here, and they nest:

  * `RunTrace.stage()` -- one block per graph node as it finishes: a banner,
    the INPUTS it consumed (each tagged with the agent that produced it), the
    OUTPUTS it actually wrote (read from the node's real delta, not guessed),
    the CONSUMERS waiting downstream, and every LLM call the stage spent.
  * `RunTrace.summary()` -- after the run: the stage timing/spend ledger, the
    full LLM call list grouped by agent, and a per-sheet lineage table saying
    which agent's output landed in which section of which sheet.
  * `note_llm_call()` -- called from `ai.py` (the single choke point every
    model call in this repo passes through), so a call made deep inside
    `prep_material_generator.extract_knowledge_from_text` is still attributed
    to the agent that asked for it, by walking the stack.

Deliberately ASCII-only and dependency-free: it is imported by the FastAPI
backend, by the CLI, and by anything else that streams the graph, and not
every one of those reconfigures its console away from cp1252.
"""
import time
from typing import Optional

WIDTH = 100
_HEAVY = "=" * WIDTH
_LIGHT = "-" * WIDTH


# -- The wiring map -----------------------------------------------------------
#
# For every node in `graph.py::build_chapter_graph`, in graph order: where its
# inputs came FROM, and where its outputs GO TO. `reads`/`feeds` name the other
# AGENT, not just the key, because "topics" appearing in six agents' inputs is
# the thing a reader is trying to follow, and a bare key name does not say who
# put it there.
#
# `writes` is only the EXPECTED set -- what actually prints per run is the
# node's real delta, so a node that returned less than advertised (a failure, a
# cache hit, an early return) shows as exactly that rather than as this table.

PIPELINE: dict[str, dict] = {
    "curriculum": {
        "label": "CURRICULUM + LEARNING MODEL",
        "module": "prep_flow/agents/stages.py::curriculum_node",
        "does": "reads the book's own teaching order, extracts what is on the pages, "
                "chains the topics, and audits each page against what mastering it needs",
        "reads": [
            ("topics[].excerpt", "SEQUENCING", "every sub-step here reads the same slice"),
            ("grade / subject", "INPUT", "how literally to read the book, and which band"),
            ("class_id / school_id", "INPUT", "the cohort the adaptive state is fetched for"),
        ],
        "writes": ["topics[].moves", "topics[].knowledge", "topics[].canonical",
                   "topics[].reasoning", "reasoning", "adaptive_state",
                   "class_context", "context_ready", "metrics"],
        "feeds": [
            ("lesson_design", "reasoning + topics[].knowledge",
             "the path is designed to the chain and the mastery target"),
            ("persist", "reasoning + topics[].knowledge",
             "the mastery target, the chain and the audit become the contract"),
            ("GENERATION (post-node-2)", "topics[].moves",
             "the order the sheet teaches in -- carried on contract.grounding.bookOrder"),
            ("NODE 3", "reasoning.chain", "what continuity means for this chapter"),
        ],
    },
    "lesson_design": {
        "label": "EXPERIENCE + LESSON PLANNING",
        "module": "prep_flow/agents/stages.py::lesson_design_node",
        "does": "designs the thinking path and its object, picks which audited shortfall "
                "each period closes, then plans the minutes and the seam around it",
        "reads": [
            ("reasoning", "CURRICULUM", "the chain, the anchors, and the mastery audit"),
            ("topics[].activities", "CURRICULUM", "the Pedagogy Library candidates"),
            ("adaptive_state / class_context", "CURRICULUM", "what this cohort needs"),
        ],
        "writes": ["experience", "plans", "chapter_arc", "selections", "metrics"],
        "feeds": [
            ("persist", "experience[i] + plans[i] + selections[i]",
             "the three that become the contract's experiencePlan / lessonPlan / "
             "selectedActivity"),
            ("NODE 2", "experience[i].anchor + selections[i]",
             "the object and the activity a contextual adaptation most often lands on"),
            ("GENERATION (post-node-2)", "experience[i] + plans[i] + selections[i]",
             "no bullet is written without all three"),
        ],
    },
    "sequencing": {
        "label": "SEQUENCING",
        "module": "prep_flow/agents/sequencing.py::sequencing_node",
        "does": "cuts the chapter into an ordered spine of teachable topics, on the\n                 boundaries the book itself prints — no model call",
        "reads": [
            ("chapter_markdown", "INPUT (textbook API / ingested folder)",
             "the whole chapter, verbatim"),
            ("chapter_figures", "INPUT", "figures per page, to anchor topics to real pages"),
            ("config.target_topics", "INPUT", "how many periods to cut it into"),
            ("topics + skip_sequencing", "VIEWER TOPIC PICKER (cache)",
             "reuse the teacher's own breakdown instead of re-deriving it"),
        ],
        "writes": ["topics", "sequencing_note", "status"],
        "feeds": [
            ("move_extraction", "topics[].excerpt", "reads each topic's slice for the book's order"),
            ("context_assembly", "topics[].excerpt", "extracts concepts/vocab from that same slice"),
            ("reasoning", "topics[]", "builds the knowledge chain BETWEEN these topics"),
            ("planning", "sequencing_note", "used as the chapter arc when planning derives none"),
            ("GENERATION (post-node-2)", "topics[].excerpt + pages", "the grounding every sheet is written from"),
            ("NODE 3", "topics[].excerpt", "grounding check: did the sheet stay in the book"),
        ],
    },
    "move_extraction": {
        "label": "MOVE EXTRACTION",
        "module": "prep_flow/agents/move_extraction.py::move_extraction_node",
        "does": "reads the order the BOOK explains each topic in, so the sheet can follow it",
        "reads": [
            ("topics[].excerpt", "SEQUENCING", "one topic's verbatim textbook slice"),
            ("grade / subject", "INPUT", "how literally to read the book's order"),
        ],
        "writes": ["topics"],
        "feeds": [
            ("planning", "topics[].moves", "moves.teaching_order() -> this sheet's section order"),
            ("GENERATION (post-node-2)", "topics[].moves", "sections are emitted in the book's own order"),
            ("NODE 3", "topics[].moves", "checks the sheet did not silently reorder the book"),
        ],
    },
    "context_assembly": {
        "label": "CONTEXT ASSEMBLY",
        "module": "prep_flow/agents/context_assembly.py::context_assembly_node",
        "does": ("extracts knowledge per topic, resolves it onto the shared canonical library, "
                 "matches Pedagogy Library activities, pulls the feedback loop's adaptive state"),
        "reads": [
            ("topics[].excerpt", "SEQUENCING", "the text knowledge is extracted from"),
            ("adaptive_state", "FEEDBACK LOOP (db row)",
             "what the last feedback cycle learned for this cohort"),
            ("class_context", "DB (class interests / weak topics)", "who this batch is for"),
            ("canonical library", "DB (shared curriculum library)",
             "concepts/vocabulary/contexts to resolve against"),
            ("Pedagogy Library", "DB", "activity templates keyed by competency"),
        ],
        "writes": ["topics", "adaptive_state", "class_context", "engagement_guidance",
                   "context_ready", "unmapped_competencies"],
        "feeds": [
            ("reasoning", "topics[].knowledge", "the chapter's concepts, as a whole, become the chain"),
            ("activity_selection", "topics[].activities", "the candidate set one activity is picked from"),
            ("planning", "adaptive_state", "adaptive_directive_block() goes into the planning prompt"),
            ("GENERATION (post-node-2)", "knowledge + adaptive_state + engagement_guidance",
             "concepts/vocabulary/contexts are written into the sheet prompt"),
            ("NODE 3", "topics[].knowledge.vocabulary", "grade-fit and vocabulary checks"),
        ],
        "seeds": ("THE ONLY STAGE THAT WRITES TO THE SHARED CANONICAL LIBRARY "
                  "(canonical_mapping.resolve_canonical) -- new rows here outlive this run"),
    },
    "reasoning": {
        "label": "REASONING",
        "module": "prep_flow/agents/reasoning.py::reasoning_node",
        "does": ("derives what the textbook does not say: prerequisites, misconceptions, and the "
                 "knowledge chain linking topic n to topic n+1"),
        "reads": [
            ("topics[].knowledge", "CONTEXT ASSEMBLY", "all topics' concepts at once"),
            ("reasoning cache", "DB", "same grade+subject+chapter -> reuse, no LLM call at all"),
        ],
        "writes": ["reasoning", "topics"],
        "feeds": [
            ("experience", "reasoning.chain", "the cognitive path is built on the chain"),
            ("planning", "reasoning", "reasoning_block() in the planning prompt"),
            ("GENERATION (post-node-2)", "reasoning", "chapter_block() in the sheet prompt; drives Level Set"),
            ("NODE 3", "reasoning.chain", "continuity: does T(n) actually assume T(n-1)"),
        ],
    },
    "experience": {
        "label": "EXPERIENCE PLAN",
        "module": "prep_flow/agents/experience.py::experience_node",
        "does": "the thinking path per topic, and the concrete object that carries it",
        "reads": [
            ("reasoning.chain", "REASONING", "what each topic assumes and gains"),
            ("topics[]", "SEQUENCING", "the topics to plan a path for"),
        ],
        "writes": ["experience", "topics"],
        "feeds": [
            ("planning", "experience[i]", "minutes are allocated along the path"),
            ("GENERATION (post-node-2)", "experience[i]",
             "NO BULLET IS WRITTEN WITHOUT ONE -- the design's hard boundary"),
        ],
    },
    "planning": {
        "label": "PLANNING",
        "module": "prep_flow/agents/planning.py::planning_node",
        "does": ("per-period minutes, emphasis, new vocabulary, and the refresher/explore seam "
                 "each topic owes the next"),
        "reads": [
            ("experience[i]", "EXPERIENCE PLAN", "the path minutes are spent on"),
            ("reasoning", "REASONING", "reasoning_block() in the prompt"),
            ("adaptive_state", "CONTEXT ASSEMBLY (from the feedback loop)", "adaptive_directive_block()"),
            ("class_context", "CONTEXT ASSEMBLY", "who is in the room"),
            ("topics[].moves", "MOVE EXTRACTION", "the section order minutes are spread over"),
            ("sequencing_note", "SEQUENCING", "fallback chapter arc"),
        ],
        "writes": ["plans", "chapter_arc", "topics"],
        "feeds": [
            ("GENERATION (post-node-2)", "plans[i]", "minutes, emphasis, exploreHook -> the sheet's shape"),
            ("NODE 3", "plans[i]", "structure and minute checks"),
            ("persist", "plans[i]", "becomes contract.lessonPlan -- minutes, emphasis, the Explore seam"),
        ],
    },
    "activity_selection": {
        "label": "ACTIVITY SELECTION",
        "module": "prep_flow/agents/activity_selection.py::activity_selection_node",
        "does": "picks ONE Challenge activity + context per topic, kept varied across the chapter",
        "reads": [
            ("topics[].activities", "CONTEXT ASSEMBLY", "the Pedagogy Library candidates"),
            ("adaptive_state", "CONTEXT ASSEMBLY", "format directives from the feedback loop"),
            ("config.no_repeat_window", "INPUT", "no activity repeats within N topics"),
        ],
        "writes": ["selections", "topics"],
        "feeds": [
            ("GENERATION (post-node-2)", "selections[i].activity/context/formats",
             "the Challenge section is written from this"),
            ("NODE 3", "selections[i]", "did the sheet use the activity it was given"),
            ("persist", "selections[i]", "becomes contract.selectedActivity"),
        ],
    },
    "persist": {
        "label": "PERSIST",
        "module": "prep_flow/graph.py::persist_node",
        "does": "composes Node 1's academic contract, builds the run trace, writes the rows",
        "reads": [
            ("everything", "ALL STAGES",
             "contract.build_chapter_contract() over the final state -- the one point "
             "at which every stage's contribution exists at once"),
        ],
        "writes": ["contract", "run_provenance", "status"],
        "feeds": [
            ("NODE 2", "contract",
             "the academic contract, closed: mastery targets, chain, experience plan, "
             "activity, and the fingerprints that prove none of it moved"),
            ("caller", "contract + run_provenance", "the SSE 'done' event / the CLI output"),
            ("DB", "prep_flow_runs.contract / _topics.contract", "when persistence is configured"),
        ],
    },
}

# Where an LLM call's `label` (prep_flow/llm.py::call_json) belongs. Longest
# matching prefix wins, so 'generation[T1-T3]' beats a bare 'generation'.
_LABEL_OWNER = (
    ("sequencing", "sequencing"),
    ("moves[", "move_extraction"),
    ("competency-mapping", "context_assembly"),
    ("knowledge-extraction", "context_assembly"),
    ("reasoning", "reasoning"),
    ("experience[", "experience"),
    ("planning[", "planning"),
    ("activity-invention", "activity_selection"),
    ("sequence-override", "sequence_override"),
    ("feedback-optimization", "feedback"),
)

# Which agent a stack frame belongs to, for a call that did NOT come through
# call_json and so carries no label of its own. `extract_knowledge_from_text`
# is the one that matters: context assembly calls it, but it lives in the
# six-stage pilot's module, so only the stack says whose call it is.
_FRAME_OWNER = {
    "sequencing.py": "sequencing",
    "move_extraction.py": "move_extraction",
    "context_assembly.py": "context_assembly",
    "reasoning.py": "reasoning",
    "experience.py": "experience",
    "planning.py": "planning",
    "activity_selection.py": "activity_selection",
    "sequence_override.py": "sequence_override",
    "feedback.py": "feedback",
    "tools.py": "context_assembly",
    "prep_material_generator.py": "context_assembly",
}


def owner_of_label(label: str) -> str:
    best, owner = "", "?"
    for prefix, node in _LABEL_OWNER:
        if label.startswith(prefix) and len(prefix) > len(best):
            best, owner = prefix, node
    return owner


def owner_from_stack() -> str:
    """The agent behind a call that carries no label -- the first stack frame
    that belongs to one. Keeps `extract_knowledge_from_text`'s call (made
    inside the pilot's module, on context assembly's behalf) attributed to
    context assembly rather than to nothing."""
    import traceback
    for frame in reversed(traceback.extract_stack()):
        name = frame.filename.replace("\\", "/").rsplit("/", 1)[-1]
        if name in _FRAME_OWNER:
            return _FRAME_OWNER[name]
    return "?"


# -- Value summaries ----------------------------------------------------------

def _n(value) -> str:
    return f"{value:,}" if isinstance(value, int) else str(value)


def _wrap(text: str, indent: str, width: int = WIDTH) -> list[str]:
    """Hard-wrap one sentence to the trace's column width. `textwrap` would do
    this, but this file stays import-light on purpose and the rule here is one
    line: never let a long `does:`/`seeds:` string push the block past the
    rules above and below it."""
    room = max(20, width - len(indent))
    lines, current = [], ""
    for word in text.split():
        if current and len(current) + 1 + len(word) > room:
            lines.append(indent + current)
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        lines.append(indent + current)
    return lines


def describe(key: str, value) -> str:
    """A one-line shape summary of one state value -- enough to see that a key
    was really written, and roughly how much came out of it."""
    if value is None:
        return "None"
    if isinstance(value, bool):
        return str(value)
    if isinstance(value, (int, float)):
        return _n(value)
    if isinstance(value, str):
        if len(value) > 70:
            return f'"{value[:67]}..." ({_n(len(value))} chars)'
        return f'"{value}"' if value else '"" (empty)'
    if isinstance(value, list):
        if key == "topics":
            return (f"{len(value)} topic spec(s): "
                    + ", ".join(f"T{t.get('index')}" for t in value[:12])
                    + (" ..." if len(value) > 12 else ""))
        if not value:
            return "[] (empty)"
        if all(isinstance(v, (str, int)) for v in value):
            joined = ", ".join(str(v) for v in value[:8])
            return f"[{len(value)}] {joined}" + (" ..." if len(value) > 8 else "")
        return f"[{len(value)} item(s)]"
    if isinstance(value, dict):
        if not value:
            return "{} (empty)"
        keys = list(value)
        if all(isinstance(k, int) for k in keys):
            return (f"{{{len(keys)}}} keyed by topic: "
                    + ", ".join(f"T{k}" for k in sorted(keys)[:12])
                    + (" ..." if len(keys) > 12 else ""))
        shown = ", ".join(str(k) for k in keys[:10])
        return f"{{{len(keys)} key(s)}} {shown}" + (" ..." if len(keys) > 10 else "")
    return type(value).__name__


def _delta_detail(node: str, key: str, value) -> list[str]:
    """The extra lines worth printing for the keys whose CONTENT is the point,
    beyond a shape summary. Kept small deliberately -- `terminal.py` prints the
    full content dump right after this block."""
    lines: list[str] = []
    if key == "topics" and isinstance(value, list):
        for spec in value[:40]:
            marks = []
            if spec.get("moves"):
                marks.append(f"moves={len(spec['moves'])}({spec.get('moves_source')})")
            knowledge = spec.get("knowledge") or {}
            if knowledge:
                marks.append(f"concepts={len(knowledge.get('concepts') or [])}"
                             f"/vocab={len(knowledge.get('vocabulary') or [])}"
                             f"/comp={len(knowledge.get('competencies') or [])}")
            if node == "context_assembly":
                marks.append(f"activityCandidates={len(spec.get('activities') or [])}")
            for slice_key, mark in (("reasoning", "reasoning-slice"),
                                    ("experience", "experience-slice"),
                                    ("plan", "plan-slice"),
                                    ("selection", "selection-slice")):
                if spec.get(slice_key):
                    marks.append(mark)
            lines.append(f"            T{spec.get('index'):<2} "
                         f"{str(spec.get('topic'))[:40]:<40} "
                         f"p{spec.get('page_start')}-{spec.get('page_end')} "
                         f"excerpt={_n(len(spec.get('excerpt') or ''))}ch"
                         + ("  " + " ".join(marks) if marks else ""))
    elif key == "contract" and isinstance(value, dict):
        # The one delta whose CONTENT is the run's output. Per topic: is it
        # complete, and if not, which of the six required fields is missing --
        # the answer a reader is looking for is almost never "how many bytes".
        readiness = value.get("readiness") or {}
        lines.append(f"            v{value.get('contractVersion')} | "
                     f"{readiness.get('topicsReady', 0)}/{readiness.get('topicsTotal', 0)} "
                     f"topic(s) ready | generationReady="
                     f"{readiness.get('generationReady')}")
        for entry in (readiness.get("notReady") or [])[:40]:
            lines.append(f"            T{entry.get('index'):<2} NOT READY -- missing "
                         f"{', '.join(entry.get('missing') or [])}")
        integrity = value.get("integrity") or {}
        if integrity.get("chapterFingerprint"):
            lines.append(f"            preserve={','.join(value.get('preserve') or [])} "
                         f"fingerprint={integrity['chapterFingerprint'][:16]}")
    elif key == "metrics" and isinstance(value, dict):
        for name in sorted(value):
            lines.append(f"            metrics.{name} = {value[name]}")
    return lines


class RunTrace:
    """One run's trace. Created by whoever streams the graph, fed each node
    update, and asked for a summary at the end."""

    def __init__(self, title: str, *, quiet: bool = False):
        self.title = title
        self.quiet = quiet
        self.started = time.time()
        self.stage_index = 0
        self.stage_started = self.started
        self.visits: dict[str, int] = {}
        self.timings: list[dict] = []
        self.calls: list[dict] = []          # every LLM call, in order
        self._call_cursor = 0                # how many are already attributed to a stage

    # -- output ---------------------------------------------------------------

    def _p(self, line: str = "") -> None:
        if not self.quiet:
            print(line)

    def banner(self, subtitle_lines: list) -> None:
        self._p()
        self._p(_HEAVY)
        self._p(f"  {self.title}")
        for line in subtitle_lines:
            self._p(f"  {line}")
        self._p(_LIGHT)
        self._p("  Each block below is ONE graph node. 'IN' names the agent every input came")
        self._p("  from; 'OUT' is what that node actually wrote into the shared graph state on")
        self._p("  this visit; 'GOES TO' is which agent picks that output up next. The wiring")
        self._p("  map behind those three lists lives in prep_flow/trace.py::PIPELINE.")
        self._p(_HEAVY)

    def stage(self, node: str, final: dict, delta: dict) -> None:
        """One node's block. `delta` is the node's REAL return value, so OUT is
        what the node actually wrote -- never what this module expected."""
        now = time.time()
        info = PIPELINE.get(node)
        self.stage_index += 1
        self.visits[node] = self.visits.get(node, 0) + 1
        visit = self.visits[node]
        elapsed = now - self.stage_started
        self.stage_started = now

        mine = self.calls[self._call_cursor:]
        self._call_cursor = len(self.calls)
        spent = sum(c["seconds"] for c in mine)

        label = (info or {}).get("label", node.upper())
        repeat = f"   (visit #{visit} -- this node runs more than once by design)" if visit > 1 else ""
        self._p()
        self._p(_HEAVY)
        self._p(f" [{self.stage_index:02d}] {label}{repeat}")
        if info:
            self._p(f"      {info['module']}")
            for line in _wrap(f"does: {info['does']}", "      "):
                self._p(line)
        self._p(f"      took {elapsed:6.1f}s  |  {now - self.started:6.1f}s into the run  |  "
                f"{len(mine)} LLM call(s) here, {spent:.1f}s of model time")
        self._p(_LIGHT)

        if info:
            self._p("  IN   -- what this agent consumed, and which agent produced it")
            for key, source, why in info["reads"]:
                self._p(f"         <- {key:<36} FROM {source}")
                for line in _wrap(why, " " * 51):
                    self._p(line)

        self._p("  OUT  -- what this agent actually wrote into the graph state")
        if not delta:
            self._p("         -> (nothing: the node returned an empty delta)")
        for key in sorted(delta or {}):
            self._p(f"         -> {key:<26} {describe(key, delta[key])}")
            for line in _delta_detail(node, key, delta[key]):
                self._p(line)

        if mine:
            self._p("  LLM  -- the model calls this stage spent, in order")
            for call in mine:
                self._p(f"         * {call['label']:<24} {call['model']:<32} "
                        f"{_n(call['prompt_chars']):>7}ch in -> {_n(call['response_chars']):>6}ch out"
                        f"  {call['seconds']:5.1f}s"
                        + ("  [FELL BACK]" if call.get("fallback") else "")
                        + (f"  [FAILED: {call['error']}]" if call.get("error") else ""))

        if info and info.get("seeds"):
            for line in _wrap(f"SEED -- {info['seeds']}", "  "):
                self._p(line)

        if info:
            self._p("  GOES TO -- which agent reads this output next")
            for target, key, why in info["feeds"]:
                target_label = PIPELINE.get(target, {}).get("label", target.upper())
                self._p(f"         -> {target_label:<20} takes {key}")
                for line in _wrap(why, " " * 38):
                    self._p(line)

        self.timings.append({"node": node, "visit": visit, "seconds": elapsed,
                             "calls": len(mine), "model_seconds": spent})

    # -- llm ledger -----------------------------------------------------------

    def note_call(self, call: dict) -> None:
        self.calls.append(call)

    # -- summary --------------------------------------------------------------

    def summary(self, final: dict) -> None:
        total = time.time() - self.started
        self._p()
        self._p(_HEAVY)
        self._p("  RUN LEDGER -- where the wall clock and the model calls went")
        self._p(_HEAVY)
        self._p(f"  {'agent':<24}{'visits':>7}{'wall s':>10}{'llm calls':>11}{'model s':>10}")
        rolled: dict[str, dict] = {}
        for row in self.timings:
            entry = rolled.setdefault(row["node"], {"visits": 0, "seconds": 0.0,
                                                    "calls": 0, "model_seconds": 0.0})
            entry["visits"] += 1
            entry["seconds"] += row["seconds"]
            entry["calls"] += row["calls"]
            entry["model_seconds"] += row["model_seconds"]
        for node in PIPELINE:
            entry = rolled.get(node)
            if not entry:
                continue
            self._p(f"  {PIPELINE[node]['label']:<24}{entry['visits']:>7}{entry['seconds']:>10.1f}"
                    f"{entry['calls']:>11}{entry['model_seconds']:>10.1f}")
        self._p(f"  {'TOTAL':<24}{'':>7}{total:>10.1f}{len(self.calls):>11}"
                f"{sum(c['seconds'] for c in self.calls):>10.1f}")

        if self.calls:
            self._p()
            self._p("  EVERY LLM CALL, GROUPED BY THE AGENT THAT MADE IT")
            self._p(_LIGHT)
            by_agent: dict[str, list] = {}
            for call in self.calls:
                by_agent.setdefault(call["agent"], []).append(call)
            for agent, calls in by_agent.items():
                agent_label = PIPELINE.get(agent, {}).get("label", agent.upper())
                self._p(f"  {agent_label}  --  {len(calls)} call(s), "
                        f"{sum(c['seconds'] for c in calls):.1f}s, "
                        f"{_n(sum(c['prompt_chars'] for c in calls))} chars sent, "
                        f"{_n(sum(c['response_chars'] for c in calls))} chars back")
                for call in calls:
                    self._p(f"      #{call['n']:<3} {call['label']:<24} {call['model']:<32} "
                            f"{call['seconds']:5.1f}s  "
                            f"{_n(call['prompt_chars']):>7} -> {_n(call['response_chars']):>6} chars"
                            + ("  [FELL BACK TO THE BACKUP MODEL]" if call.get("fallback") else "")
                            + (f"  [FAILED: {call['error']}]" if call.get("error") else ""))

        self._print_lineage(final)

    def _print_lineage(self, final: dict) -> None:
        """The end of the trail: for every topic in the contract, which agent's
        output each of its six sections will be written from.

        Built from `provenance.section_citations()`. It used to run over the
        SHEETS -- what actually landed in each section of each generated
        material -- and it now runs over the contract, because there are no
        sheets in Node 1. That is a real narrowing and worth naming: this is now
        a statement of what each section is CONTRACTED to be written from, not a
        record of what a model then did with it. The second half of that
        sentence is Node 3's to make.

        `previous_handoff` is always None for the same reason. The Refresher's
        source is the previous topic's Explore, and no Explore has been written
        yet -- what the contract carries instead is `lessonPlan.exploreHook`,
        which is the PLAN for that seam and is printed under Explore below.
        """
        rows = ((final.get("contract") or {}).get("topics") or [])
        if not rows:
            return
        try:
            from . import provenance
            from .sections import SECTION_LABELS
        except Exception as exc:                       # never take a run down for a trace
            self._p(f"  (lineage unavailable: {exc})")
            return

        specs = {t["index"]: t for t in (final.get("topics") or [])}
        plans = final.get("plans") or {}
        selections = final.get("selections") or {}

        self._p()
        self._p(_HEAVY)
        self._p("  WHAT EVERY SECTION IS CONTRACTED TO BE WRITTEN FROM -- topic by topic")
        self._p(_HEAVY)
        for row in rows:
            index = row.get("index")
            spec = specs.get(index) or {}
            try:
                citations = provenance.section_citations(
                    spec, plans.get(index) or {}, selections.get(index) or {}, None)
            except Exception as exc:
                self._p(f"  T{index}: (citations unavailable: {exc})")
                continue
            self._p("")
            ready = (row.get("readiness") or {}).get("ready")
            self._p(f"  T{index}. {row.get('topic') or spec.get('topic', '(untitled)')}"
                    + ("" if ready else "   [NOT READY]"))
            # The contract's own section order, which is the BOOK's order where
            # move extraction found one -- not the canonical six. Printing the
            # canonical order here would show a reader a sequence the sheet is
            # not going to be written in.
            for entry in row.get("sectionSpec") or []:
                section = entry.get("section")
                citation = citations.get(section)
                if not citation:
                    continue
                label = SECTION_LABELS.get(section, section)
                minutes = entry.get("minutes")
                head = f"{label} ({minutes}m)" if minutes else label
                self._p(f"     {head:<16} <- {citation[:72]}")
                for chunk in range(72, len(citation), 72):
                    self._p(f"     {'':<16}    {citation[chunk:chunk + 72]}")


# -- The live singleton every LLM call reports into ---------------------------
#
# `ai.py::call_ai` is the one function every model call in this repo passes
# through, and it has no idea which graph node it is inside. Rather than thread
# a tracer through eight agent modules, the active RunTrace registers itself
# here and ai.py hands its per-call facts back -- attribution is worked out
# from the call's own label, or from the stack when it has none.

CURRENT: Optional[RunTrace] = None


def activate(trace: RunTrace) -> RunTrace:
    global CURRENT
    CURRENT = trace
    return trace


def deactivate() -> None:
    global CURRENT
    CURRENT = None


def note_llm_call(*, label: str, model: str, prompt_chars: int, response_chars: int,
                  seconds: float, fallback: bool = False, error: str = "",
                  usage: dict = None) -> None:
    """Called from `ai.py` for every completed (or failed) model call."""
    if CURRENT is None:
        return
    agent = owner_of_label(label) if label and label != "(unlabelled)" else "?"
    if agent == "?":
        agent = owner_from_stack()
    CURRENT.note_call({
        "n": len(CURRENT.calls) + 1, "label": label or "(unlabelled)", "agent": agent,
        "model": model, "prompt_chars": prompt_chars, "response_chars": response_chars,
        "seconds": seconds, "fallback": fallback, "error": error, "usage": usage or {},
    })
