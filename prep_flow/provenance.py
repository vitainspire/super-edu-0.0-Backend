"""Provenance: what ran, on what data, and where it landed.

Nothing else in this package answers "why does this topic's contract say what it
says" — the contract states the mastery target, the chain and the chosen
activity, but not which STAGE derived each of them, which DATA fed it, whether
the shared canonical library grew (was "seeded") to produce it, or whether the
run happened in the request that asked for it or in a FastAPI background task
afterwards.

THE STAGE LIST SHRANK when the master orchestration was split into nodes. Four
of the eight rows a run used to produce described generating a sheet and judging
it, and those stages now run after Node 2 and inside Node 3. What is left is
still the whole story of a Node 1 run, because a Node 1 run is what this package
now does.

This module is deliberately NOT another graph node. Every node already puts
what it derived into `ChapterState` (`state.py`) — `metrics` in particular is
a flat, ever-growing dict every node already contributes counts to — so a
stage's row can be built straight from `state` at any moment, without a
twelfth node whose only job is bookkeeping.

THREE things come out of this module, matching the three ways a person
actually wants to read a run:

  * `live_line()` — ONE stage's row, the moment that stage finishes, as a
    single terminal-friendly line. Called from inside the streaming loop in
    `prep_flow/cli.py` and `textbook_viewer.py`, so a run is legible bit by
    bit while it is still going, not only as a table after it ends.
  * `build_run_provenance()` — every stage's row, compiled once, after the
    whole run finishes (`persist_node`, which is the one place the complete
    final state exists). Stored on `prep_flow_runs.provenance`.
  * The per-topic counterpart, `topic_provenance()`, and the per-section
    `section_citations()` below it. Both are read by the generation stage, which
    now lives downstream of Node 2 (`_deferred/generation.py`) — they stay here
    because what they describe is where each section's substance CAME FROM, and
    all of that is Node 1 data.

`live_line()` and `build_run_provenance()` read the same per-stage builder
(`_stage_entry`), so a line printed mid-run and that same stage's row in the
final stored trace can never disagree with each other.
"""
from datetime import datetime, timezone

# Every RESPONSIBILITY this pipeline has, with what a reader who did not write it
# needs to know about each: what it reads, what it writes, and whether it is the
# one place the shared canonical library actually GROWS (creates new concept/
# competency/vocabulary/context rows — "seeds" them) rather than only ever
# matching against what is already there.
#
# MORE ENTRIES THAN THERE ARE GRAPH NODES, since the merge. Three nodes each run
# several of these in sequence — see prep_flow/agents/stages.py — and STAGE_ORDER
# below is the list of NODES, which is what a trace of a run should be a trace of.
# The sub-stage entries stay because their numbers are still worth having and
# their builders are what the merged builders are made of, and because
# `live_line()` is called with whatever node name the caller streamed: a viewer
# that has not been redeployed is still streaming `validation` and must not get a
# KeyError for it.
STAGE_INFO: dict[str, dict] = {
    "sequencing": {
        "label": "Sequencing",
        "reads": "the chapter's own transcribed text and its headings/page markers",
        "writes": "the ordered topic spine (T1...Tn), each anchored to real pages",
        "seeds": False,
    },
    "context_assembly": {
        "label": "Context assembly",
        "reads": ("each topic's own textbook excerpt, the canonical concept/competency/"
                  "vocabulary/context library, the Pedagogy Library's activity templates, "
                  "class context (interests/weak topics/teaching profile), and the adaptive "
                  "state the feedback loop last produced for this cohort"),
        "writes": "extracted knowledge, resolved canonical ids, and candidate activities, per topic",
        # The only stage that WRITES to the shared canonical library rather than only
        # reading it — see resolve_canonical() in canonical_mapping.py.
        "seeds": True,
    },
    "reasoning": {
        "label": "Reasoning",
        # The printed pages appear here and nowhere else before generation: the
        # mastery pass is the only stage that reads the excerpt to judge what it
        # supplies, which is why the audit is made here rather than where the gap
        # is later chosen.
        "reads": "the chapter's extracted knowledge as a whole, and each topic's "
                 "printed pages",
        "writes": "prerequisites, misconceptions, the knowledge chain between "
                  "topics, and per topic the mastery target with the shortfalls "
                  "its pages were audited for",
        "seeds": False,
    },
    "experience": {
        "label": "Experience plan",
        "reads": "the reasoning chain and mastery audit for this chapter",
        "writes": "the cognitive path, the object that carries it, and which "
                  "audited shortfall this period closes, per topic",
        "seeds": False,
    },
    "planning": {
        "label": "Planning",
        "reads": "the experience plan and the chapter's reasoning",
        "writes": "per-period minutes, emphasis, and the refresher/explore seam",
        "seeds": False,
    },
    "activity_selection": {
        "label": "Activity selection",
        "reads": "each topic's candidate activities (Pedagogy Library) and the chapter's recent picks",
        "writes": "the selected Challenge activity, context, and section formats, per topic",
        "seeds": False,
    },
    "persist": {
        "label": "Persist",
        "reads": "the run's entire final state",
        "writes": "Node 1's academic contract, and the stored run and topic rows",
        "seeds": False,
    },
}

# GENERATION, VALIDATION, SIMULATION and REPAIR had entries here until the master
# orchestration was split into nodes. They are not merely absent: a `live_line()`
# called with one of those names now returns "" rather than a row, which is the
# correct answer — a viewer still streaming `validation` is streaming a node that
# does not run, and inventing a row for it would report work nobody did. See
# `_deferred/README.md` for which node claimed each of them.

# ── The merged stages, as the graph actually runs them ──────────────────────
STAGE_INFO.update({
    "curriculum": {
        "label": "Curriculum + learning model",
        "reads": "each topic's printed pages — for the order the book explains "
                 "them in, the concepts on them, and what mastering those "
                 "concepts requires",
        "writes": "the book's teaching order per topic, extracted knowledge "
                  "resolved onto the canonical library, the knowledge chain "
                  "between topics, and each topic's mastery target with the "
                  "shortfalls its pages were audited for",
        # Inherited from the sub-step that does it. Knowledge extraction is still
        # the only place in the pipeline that writes new rows to the shared
        # library, and burying that inside a merged stage without saying so would
        # hide the one side effect this trace exists to surface.
        "seeds": True,
    },
    "lesson_design": {
        "label": "Experience + lesson planning",
        "reads": "the knowledge chain, the mastery audit, the class context, and "
                 "each topic's candidate activities",
        "writes": "the cognitive path and its object, which audited shortfall "
                  "each period closes, the per-period minutes and section "
                  "emphasis, the Explore/Refresher seam, and the chosen activity",
        "seeds": False,
    },
})


def _compose(state: dict, *stages: str) -> dict:
    """One merged stage's row, built from the rows of the sub-stages it ran.

    `dataOut` is unioned; anything else a sub-builder sets — `dataIn`, `seeded` —
    is taken from whichever sub-stage set it, because only one of them does. The
    numbers are therefore literally the same numbers the split pipeline reported,
    which is what makes a trace from before the merge comparable with one from
    after it.
    """
    entry: dict = {"dataOut": {}}
    for name in stages:
        builder = _STAGE_DATA_BUILDERS.get(name)
        if not builder:
            continue
        for key, value in builder(state).items():
            if key == "dataOut":
                entry["dataOut"].update(value or {})
            else:
                entry[key] = value
    return entry


# THE NODES, in the order the graph runs them. Explicit rather than
# `tuple(STAGE_INFO)`, which is what it used to be: derived, it would report the
# sub-stages and the merged stages that contain them as though they were eleven
# separate things that happened, and count every number twice.
STAGE_ORDER = (
    "sequencing",
    "curriculum",
    "lesson_design",
    "persist",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _metric(state: dict, key: str, default=None):
    return (state.get("metrics") or {}).get(key, default)


# ── Per-stage data builders ──────────────────────────────────────────────────
#
# One function per stage, each returning the {"dataIn": ..., "dataOut": ...,
# "seeded": ...} slice of that stage's row — nothing more. Kept separate from
# _stage_entry() below so a stage's numbers are computed in exactly one place,
# read by both the live, mid-run line and the after-the-fact stored trace.

def _sequencing_data(state: dict) -> dict:
    topics = state.get("topics") or []
    return {"dataOut": {
        "topicsSequenced": _metric(state, "topics_sequenced", len(topics)),
        "chapterPagesAvailable": _metric(state, "chapter_pages_available"),
        "figuresAvailable": _metric(state, "figures_available"),
    }}


def _context_assembly_data(state: dict) -> dict:
    adaptive = state.get("adaptive_state") or {}
    return {
        "dataIn": {
            "adaptiveStateVersion": adaptive.get("_version"),
            "adaptiveStateSampleSize": adaptive.get("_sampleSize"),
            "classContext": bool(state.get("class_context")),
        },
        "dataOut": {
            "topicsGroundedInTextbook": _metric(state, "topics_with_textbook"),
            "topicsWithActivityMatch": _metric(state, "topics_with_activity_match"),
            "competenciesMappedToLibrary": _metric(state, "competencies_mapped"),
            "competenciesUnmatched": _metric(state, "competencies_unmapped"),
            "unmappedCompetencyNames": (state.get("unmapped_competencies") or [])[:20],
        },
        # The only stage that grows the shared canonical library rather than only
        # reading it. 0 is a real, meaningful answer (every extracted name already
        # existed) — included whether or not anything was created, so "seeded 0"
        # reads as measured rather than as not reported.
        "seeded": {"canonicalEntriesCreated": _metric(state, "canonical_entries_created", 0)},
    }


def _reasoning_data(state: dict) -> dict:
    reasoning = state.get("reasoning") or {}
    return {"dataOut": {
        "cached": bool(_metric(state, "reasoning_cached")),
        "prerequisites": len(reasoning.get("prerequisites") or []),
        "misconceptionsNamed": len(reasoning.get("misconceptions") or []),
        "chainEntries": len(reasoning.get("chain") or {}),
        # Reported next to the chain because they are the two halves of the same
        # question and they can disagree: a chapter can have a chain entry for
        # every topic and a mastery target for none of them, which is what a run
        # looks like when the audit was served from a cache row written before the
        # pass existed and the top-up failed. Without this the trace would show a
        # complete Reasoning stage either way.
        "masteryTargets": _metric(state, "mastery_targeted", 0),
        "shortfallsAudited": _metric(state, "mastery_shortfalls", 0),
        # A page that supplies what its idea needs is a good page, not a stage
        # that did nothing — recorded so the two are distinguishable.
        "pagesNeedingNothing": _metric(state, "mastery_topics_complete", 0),
        "auditToppedUpFromCache": bool(_metric(state, "mastery_topped_up")),
    }}


def _experience_data(state: dict) -> dict:
    plans = [p for p in (state.get("experience") or {}).values()
             if isinstance(p, dict)]
    return {"dataOut": {
        "cached": bool(_metric(state, "experience_cached")),
        "plansDerived": sum(1 for p in plans if p.get("derived")),
        # What this chapter actually reinforces, and the kinds it reinforces it
        # with. `gapsClosed` below `plansDerived` rather than beside it because a
        # plan can be fully derived and close nothing, which is the correct answer
        # for a topic whose pages were audited as complete.
        "gapsClosed": sum(1 for p in plans if p.get("gap") and p.get("gapCloser")),
        "gapKinds": _metric(state, "experience_gap_kinds", {}) or {},
        "transferTasks": sum(1 for p in plans if p.get("transferTask")),
    }}


def _planning_data(state: dict) -> dict:
    return {"dataOut": {
        "chapterArc": bool(state.get("chapter_arc")),
        "periodsPlanned": len(state.get("plans") or {}),
    }}


def _activity_selection_data(state: dict) -> dict:
    return {"dataOut": {
        "activitiesFromLibrary": _metric(state, "activities_from_library"),
        "activitiesInvented": _metric(state, "activities_invented"),
        "activitiesMissing": _metric(state, "activities_missing"),
        "distinctActivities": _metric(state, "distinct_activities"),
    }}


def _persist_data(state: dict) -> dict:
    """What the contract came out as.

    This used to report which revision of each sheet shipped and which rewrites
    were reverted. Persist still decides something — it is the stage that
    composes the contract — but what it decides is now whether each topic is
    complete, so that is what it reports.

    `topicsNotReady` names the topics rather than counting them, and names what
    each is missing, because the count alone sends a reader back to the document
    to find out which four of forty and why.
    """
    document = state.get("contract") or {}
    readiness = document.get("readiness") or {}
    return {"dataOut": {
        "contractVersion": document.get("contractVersion"),
        "topicsTotal": readiness.get("topicsTotal"),
        "topicsReady": readiness.get("topicsReady"),
        # False with topics present is the honest failure: a contract was built
        # and it is not one Node 2 can act on whole. "not ready" and "never ran"
        # look identical from a missing number, so it is stated either way.
        "generationReady": readiness.get("generationReady"),
        "topicsNotReady": [f"T{entry.get('index')}: "
                           f"{', '.join(entry.get('missing') or [])}"
                           for entry in (readiness.get("notReady") or [])],
        "chapterFingerprint": (document.get("integrity") or {}).get("chapterFingerprint"),
    }}


_STAGE_DATA_BUILDERS = {
    "sequencing": _sequencing_data,
    "context_assembly": _context_assembly_data,
    "reasoning": _reasoning_data,
    "experience": _experience_data,
    "planning": _planning_data,
    "activity_selection": _activity_selection_data,
    "persist": _persist_data,
}

# Added after the table they are built from, because each one reads several of
# its entries. `move_extraction` has no builder of its own and never had one —
# its two numbers are read straight off `metrics` here, which is where they
# always were.
_STAGE_DATA_BUILDERS.update({
    "curriculum": lambda state: _merge_out(
        _compose(state, "context_assembly", "reasoning"),
        {"booksOwnOrderRead": _metric(state, "topics_with_moves", 0),
         "topicsTheCanonicalOrderWouldHaveGotWrong":
             _metric(state, "topics_reordered_by_book", 0)}),
    "lesson_design": lambda state: _compose(
        state, "experience", "planning", "activity_selection"),
})


def _merge_out(entry: dict, extra: dict) -> dict:
    entry["dataOut"] = {**extra, **(entry.get("dataOut") or {})}
    return entry


def _stage_entry(stage: str, state: dict) -> dict:
    """One stage's row — label/reads/writes plus real numbers pulled from
    `state` AT THE MOMENT THIS IS CALLED. The single place both `live_line()`
    (mid-run, one stage at a time) and `build_run_provenance()` (after the
    run, all stages at once) get their numbers from, so the two can never
    disagree about the same stage.
    """
    info = STAGE_INFO[stage]
    entry = {"stage": stage, "label": info["label"], "reads": info["reads"], "writes": info["writes"]}
    builder = _STAGE_DATA_BUILDERS.get(stage)
    if builder:
        entry.update(builder(state))
    return entry


def live_line(stage: str, state: dict) -> str:
    """A short, one-line summary for THIS stage alone, meant to be printed
    (to a terminal, or turned into an SSE event) the moment that stage
    finishes — while the run is still going, not after it.

    This is what makes a run legible bit by bit: `prep_flow/cli.py` and
    `textbook_viewer.py` both stream the graph node by node already
    (`graph.astream(..., stream_mode="updates")`); this is the one line each
    of them prints, right after merging that node's delta into the running
    state, so whoever is watching the terminal sees what that stage actually
    read, produced, and — for context_assembly — seeded, as it happens.

    Returns "" for anything this module has nothing to say about (e.g. the
    viewer's own synthetic "sequence_override" step) — callers should skip
    printing on an empty string rather than print a content-free line.
    """
    if stage == "persist":
        # By the time this fires, persist_node's own delta (status, contract,
        # run_provenance) is already merged into `state` — so this is the one
        # line that can report the run's FINAL outcome, not just what one
        # stage did.
        execution = (state.get("run_provenance") or {}).get("execution") or {}
        readiness = (state.get("contract") or {}).get("readiness") or {}
        return (f"[pipeline] Persist done -- run finished with status="
                f"{state.get('status', 'unknown')} | contract "
                f"{readiness.get('topicsReady', 0)}/{readiness.get('topicsTotal', 0)} "
                f"topic(s) ready, generationReady="
                f"{readiness.get('generationReady', False)} | backgroundTask="
                f"{execution.get('backgroundTask', False)}")
    if stage not in STAGE_INFO:
        return ""
    entry = _stage_entry(stage, state)
    bits = [f"{k}={v}" for k, v in (entry.get("dataOut") or {}).items()
            if v not in (None, "", [], {})]
    if entry.get("seeded"):
        created = entry["seeded"].get("canonicalEntriesCreated", 0)
        if created:
            bits.append(f"SEEDED {created} new canonical entr{'y' if created == 1 else 'ies'} "
                       f"in the shared library")
    detail = ", ".join(bits) if bits else "(nothing to report yet)"
    # ASCII only, deliberately: unlike prep_flow/cli.py / prep_material_cli.py,
    # textbook_viewer.py does not reconfigure stdout to UTF-8, so a checkmark or
    # em dash here would raise UnicodeEncodeError on a default-cp1252 Windows
    # console and crash the print instead of showing it.
    return f"[pipeline] {entry['label']} done -- {entry['writes']} | {detail}"


def build_run_provenance(state: dict, *, background_task: bool, run_id: str = None,
                         thread_id: str = None) -> dict:
    """The chapter-wide trace: every stage that ran, what it actually read and
    produced, and how the run itself was executed.

    Built AFTER the graph finishes rather than accumulated node by node:
    `persist_node` is the one place the complete state — every node's
    metrics, the final topic count, the adaptive state actually used — exists
    at once, so this is the one place the story can be told without a stage
    reporting on work later stages might still change.
    """
    # `persist` is included because it decides something: it composes the
    # contract, which is the run's actual output. It was excluded while its only
    # contribution was "wrote the rows".
    stages = [_stage_entry(stage, state) for stage in STAGE_ORDER]

    return {
        "runId": run_id,
        "threadId": thread_id,
        "generatedAt": now_iso(),
        "execution": {
            "backgroundTask": background_task,
            "note": (
                "This chapter ran inside a FastAPI BackgroundTasks job: the caller got "
                "a 202 and a runId immediately, and this pipeline kept running after "
                "that response was already sent (prep_flow/routes.py::_run_in_background)."
                if background_task else
                "This chapter ran in the foreground — the caller's own process (the CLI, "
                "or a direct run_chapter() call) waited for the whole run to finish."
            ),
        },
        "stages": stages,
        "errors": list(state.get("errors") or []),
    }


def _truncated_list(names: list, limit: int = 6) -> str:
    names = [n for n in (names or []) if n]
    if not names:
        return ""
    shown = ", ".join(names[:limit])
    return shown + (f" (+{len(names) - limit} more)" if len(names) > limit else "")


def section_citations(spec: dict, plan: dict, selection: dict,
                      previous_handoff: dict = None) -> dict[str, str]:
    """One short citation per SECTION of THIS topic's sheet — not a stage, a
    section (Concept, Real Life, ...), because that is what a reader is
    actually looking at. Mirrors prep_material_generator.build_section_
    citations() for the six-stage pilot, so a teacher who has seen a pilot
    sheet's citations recognises the same convention on a chapter sheet.

    Deliberately names the ACTUAL content behind a section wherever that
    content exists — the real concept/vocabulary names extracted, the real
    context named, the real scene the Refresher is echoing — rather than a
    category label with only a count attached. A citation that says "3
    concepts extracted" tells a reader nothing they could check; one that
    names them ("Sequencing, Farming Steps, Team Effort") does.

    `previous_handoff`: the PREVIOUS topic's explore.handoff dict
    (`sections.handoff_of(previous_material)`), when the caller has it. This
    is what makes the Refresher's citation quote the specific scene/object/
    question it is built from, instead of a generic "built from the previous
    Explore" sentence that is true of every Refresher and names nothing.

    render_topic() (render.py) numbers these in first-appearance order and
    prints a "## Sources" list at the end of the sheet — the citation sits
    next to the bullet it backs, the reference is defined once.
    """
    knowledge = spec.get("knowledge") or {}
    reasoning = spec.get("reasoning") or {}
    activity = (selection or {}).get("activity") or {}
    context = (selection or {}).get("context") or {}
    seeded = spec.get("canonical_seeded") or {}
    created = [n for entry in seeded.values() for n in (entry.get("created") or [])]
    contexts = [c for c in (knowledge.get("contexts") or []) if isinstance(c, str) and c.strip()]
    concepts = [c for c in (knowledge.get("concepts") or []) if isinstance(c, str) and c.strip()]
    vocabulary = [v for v in (knowledge.get("vocabulary") or []) if isinstance(v, str) and v.strip()]

    pages = [spec.get("page_start"), spec.get("page_end")]
    if pages[0]:
        page_str = (f"page {pages[0]}" if not pages[1] or pages[1] == pages[0]
                   else f"pages {pages[0]}-{pages[1]}")
    else:
        page_str = "no ingested textbook pages"

    # A plain string either way — never a raw dict repr — regardless of
    # whether exploreHook has a "scene", only a "question", or neither.
    hook = (plan or {}).get("exploreHook")
    hook_text = ""
    if isinstance(hook, dict):
        hook_text = (hook.get("scene") or hook.get("question") or "").strip()
    elif isinstance(hook, str):
        hook_text = hook.strip()

    citations: dict[str, str] = {
        "concept": (
            f"Textbook, {page_str} (context assembly) — concepts: "
            f"{_truncated_list(concepts) or '(none extracted)'}; vocabulary: "
            f"{_truncated_list(vocabulary) or '(none extracted)'}. Resolved against the "
            f"shared canonical library."
            + (f" This topic seeded {len(created)} new entr{'y' if len(created) == 1 else 'ies'} "
               f"into that library: {_truncated_list(created)}." if created else "")
        ),
        "realLife": (
            f"Textbook-named context(s) (context assembly): {_truncated_list(contexts)}."
            if contexts else
            f"No textbook-named context for these pages; grounded in the Pedagogy "
            f"Library's suggested context \"{context.get('name')}\" (activity selection)."
            if context.get("name") else
            "Neither the textbook nor the Pedagogy Library named a usable context — invented."
        ),
        "levelSet": (
            f"Free (generation) — the misconception this section is meant to surface: "
            f"\"{reasoning['misconceptions'][0]}\"." if reasoning.get("misconceptions") else
            "Free — the model's own design (generation); not grounded in an external source."
        ),
        "explore": (
            f"Free (generation) — planned to open on: \"{hook_text}\"."
            if hook_text else
            "Free — built to hand off to the next topic's Refresher (generation); "
            + ("opens on one of the textbook's own named things where possible"
               if contexts else "no textbook context was available to open on")
        ),
        "refresher": (
            (
                f"The previous topic's Explore — scene: \"{previous_handoff.get('scene')}\", "
                if previous_handoff.get("scene") else "The previous topic's Explore — "
            ) + (
                f"object: \"{previous_handoff.get('object')}\", " if previous_handoff.get("object") else ""
            ) + (
                f"left open: \"{previous_handoff.get('question')}\"." if previous_handoff.get("question")
                else "(no structured handoff was recorded for it)."
            )
            if previous_handoff else
            "Built entirely from the previous topic's Explore (planning + generation) — "
            "no new textbook or library lookup for this section."
        ),
    }

    if activity.get("name"):
        # Prefer the REAL reason activity_selection_node() already computed
        # (agents/activity_selection.py::_selection_reason()) — the same
        # sentence a developer sees in the terminal trace, now sitting next
        # to the Challenge activity it explains, in the material itself. A
        # citation that says "matched 2 competencies; best of 4 candidates
        # considered" lets a teacher (or a founder) see why THIS activity
        # over the others, not just that one was selected somehow.
        reason = (selection or {}).get("reason")
        if reason:
            citations["challenge"] = f"Activity \"{activity['name']}\" — {reason} (activity selection)."
        else:
            # Fallback for a selection built before this field existed (an
            # older persisted run, or one built outside the normal node).
            source = activity.get("source", "library" if activity.get("id") else "invented")
            citations["challenge"] = (
                f"Activity \"{activity['name']}\" — Pedagogy Library (activity selection), "
                f"matched on {len(activity.get('matchedCompetencyIds') or [])} competenc(ies)."
                if source == "library" else
                f"Activity \"{activity['name']}\" — invented for this topic; the Pedagogy "
                f"Library had no match (activity selection)."
            )

    return citations


def topic_provenance(spec: dict, plan: dict, selection: dict) -> dict:
    """The per-sheet counterpart: for THIS topic alone, what fed it.

    Built from exactly the arguments `_normalise_material` already has —
    nothing here is looked up again — and attached to `material["_meta"]
    ["provenance"]` so it travels with the material wherever it is read:
    the API response, the stored row, the rendered Markdown.
    """
    knowledge = spec.get("knowledge") or {}
    activity = (selection or {}).get("activity") or {}
    seeded = spec.get("canonical_seeded") or {}
    created = {kind: entry.get("created") for kind, entry in seeded.items() if entry.get("created")}

    return {
        "pages": [spec.get("page_start"), spec.get("page_end")],
        "textbookExcerptChars": len(spec.get("excerpt") or ""),
        "knowledgeExtracted": {
            "concepts": len(knowledge.get("concepts") or []),
            "competencies": len(knowledge.get("competencies") or []),
            "vocabulary": len(knowledge.get("vocabulary") or []),
            "contexts": len(knowledge.get("contexts") or []),
        },
        "canonicalLibraryIdsUsed": spec.get("canonical_names") or {},
        # Only the kinds that actually got a new row THIS run. An empty dict here
        # (as opposed to the field being absent) means every name this topic
        # extracted already existed in the shared library.
        "canonicalEntriesSeededHere": created,
        "activity": {
            "name": activity.get("name"),
            "source": activity.get("source", "library" if activity.get("id") else "invented"),
            "id": activity.get("id"),
        },
        "context": ((selection or {}).get("context") or {}).get("name"),
        "sectionFormats": (selection or {}).get("formats") or {},
        "plannedMinutes": (plan or {}).get("minutes") or {},
    }
