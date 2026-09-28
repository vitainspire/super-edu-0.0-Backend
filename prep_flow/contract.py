"""The academic contract — Node 1's output, and the only thing Node 2 is handed.

WHAT THIS REPLACED. Until the master orchestration was split into nodes, this
package ran to the end: it sequenced the chapter, derived what mastering it
requires, designed the period — and then wrote the six-section sheet and judged
it. Generation and validation have left (see `_deferred/README.md`). What is
left is Node 1, textbook representation and transformation, and a node has to
end in an artefact rather than in a graph edge.

This is that artefact. Everything Node 1 derived, in one structured document,
shaped for the thing that reads it next.

WHY IT IS BUILT HERE RATHER THAN READ OFF THE STATE. `ChapterState` is a working
surface: forty TopicSpecs each carrying their own excerpt, the raw model output
of six agents, cache flags, per-node metrics. Handing that to Node 2 would make
Node 2's prompt a function of Node 1's internals — rename `gapCloser` and a
different node breaks. A contract is the place to pay that cost once.

THREE PROPERTIES IT HAS THAT THE STATE DOES NOT:

  * **It is closed.** Every field Node 2 may read is here, and nothing here is a
    pointer into something else. Node 2 needs no database, no excerpt, no
    canonical library — which is what makes it possible to run Node 2 in shadow
    mode against a logged contract months later.
  * **It states what may not change.** `preserve` names the fields Node 2 adapts
    the route to rather than the fields it may edit, and `integrity` carries a
    fingerprint of exactly those fields. Node 3 recomputes the fingerprint over
    what came back; a mastery target that moved cannot be argued about.
    See the implementation framework, §10 (Equity & Safety Gate) — the
    deterministic half of that gate is a hash comparison, and this is the hash.
  * **It says whether it is finished.** `readiness` per topic, and a chapter-wide
    `generationReady`. A topic with no experience plan is not a topic Node 2 can
    usefully adapt or generation can write from, and the honest place to say so
    is the handoff, not a downstream failure.

WHAT IS DELIBERATELY NOT HERE. The excerpt. It is the largest field in the state
by an order of magnitude and Node 2 has no use for it: contextual reinforcement
adapts the route to a target, and the target is already stated in words. What is
carried instead is the grounding *reference* — pages, figure ids, the book's own
move order — so anything downstream that needs the text can fetch exactly the
pages this topic was cut from. `excerptChars` is kept because "the contract for
T7 was built from 40 characters of book" is a real diagnosis.

The one exception inside that reference is `bookOrder[].verbatim`: the book's own
words for the moves that print an instruction, capped and only on `activity` and
`practice`. It is here because Node 3's book-fidelity check compares the sheet
against exactly those quotes, and a check that cannot see them passes everything.
That is a few hundred characters per topic against an excerpt of several
thousand, which is the trade this exception is worth making and the general rule
is not.

NO MODEL IS CALLED IN THIS MODULE. It is a projection of work already done, and
if it ever needs a model to fill a field, that field belongs in the agent that
derived the rest of its neighbours.
"""
import hashlib
import json
from typing import Optional

from . import moves as moves_module
from .sections import SECTION_LABELS, SECTION_ORDER, SECTION_POLICY

CONTRACT_VERSION = "1.0"

# The fields Node 2 may not move. Named here rather than in Node 2, because the
# node that owns a constraint is the node that can still enforce it — Node 2
# asked to police its own boundary is being asked to mark its own work.
#
# The distinction the framework draws (§2, "Route, not rigor"): language,
# scaffolding, participation route, examples and delivery are all adaptable, and
# every one of them is downstream of these five. What a child must end up knowing
# is not.
PRESERVE = (
    "masteryTarget",      # what the idea requires of a child, per topic
    "requiredConcepts",   # the concepts the pages actually carry
    "competencies",       # the curriculum competencies mapped to them
    "knowledgeChain",     # what this period assumes and what it owes the next
    "prerequisites",      # what the chapter assumes before any of it
)


def _clean(value):
    """Drop the empties, keep the zeroes.

    A contract full of `null` and `[]` is a contract whose reader cannot tell an
    absent field from an unfilled one — and Node 2's prompt is assembled from
    whichever of these fields are present, so an empty list costs tokens to say
    nothing. `0` and `False` survive: a difficulty of 0 and a `movesReordered`
    of False are both answers.
    """
    if isinstance(value, dict):
        cleaned = {k: _clean(v) for k, v in value.items()}
        return {k: v for k, v in cleaned.items() if v not in (None, "", [], {})}
    if isinstance(value, list):
        cleaned = [_clean(v) for v in value]
        return [v for v in cleaned if v not in (None, "", [], {})]
    return value


def fingerprint(payload) -> str:
    """A stable hash of whatever is passed, for the integrity block.

    `sort_keys` and a separator with no spaces, so the same contract hashes the
    same after a JSON round trip through the database, through an HTTP response,
    and through whatever Node 2 hands back. Without that, `integrity` would fail
    on formatting and Node 3 would learn to ignore it — which is worse than not
    having it.
    """
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:32]


# ── Per-topic ────────────────────────────────────────────────────────────────

def _grounding(spec: dict) -> dict:
    """Where in the book this topic came from — a reference, not the text.

    `bookOrder` is the one part of the excerpt that survives into the contract,
    and it survives because it is not a copy of the book: it is the order the
    book explains the topic in, which move extraction derived and which every
    later stage allocates against. Node 2 needs it to know whether a proposed
    resequencing is a route change or a fight with the page.
    """
    topic_moves = spec.get("moves") or []
    return {
        "pageStart": spec.get("page_start"),
        "pageEnd": spec.get("page_end"),
        # Char offsets into the chapter markdown -- exact, and the only way to
        # re-cut this topic's excerpt when the source carries no page markers
        # (a real, observed case: published-book chapters with no <!-- page N
        # --> markers at all leave pageStart/pageEnd null, and re-slicing by
        # page then silently returns nothing). See sequencing.py::derive_topics
        # and generation/adapt.py::sources_from_chapter.
        "excerptStart": spec.get("excerpt_start"),
        "excerptEnd": spec.get("excerpt_end"),
        "anchorKind": spec.get("anchor_kind"),
        "excerptChars": len(spec.get("excerpt") or ""),
        "figures": [{"id": f.get("id"), "page": f.get("page"),
                     "caption": f.get("caption")}
                    for f in (spec.get("figures") or [])],
        # `fidelity` and `verbatim` are carried because NODE 3 READS THEM, and
        # for no other reason — they were absent from the first version of this
        # contract and the book-fidelity check degraded silently without them.
        #
        # `fidelity` says how much of the move is the book's own (verbatim /
        # faithful / staged) and `verbatim` is the book's actual words, present
        # only on `activity` and `practice` moves and capped by
        # moves.MAX_VERBATIM_CHARS. So this is a handful of short quotes per
        # topic, not a second copy of the chapter — the excerpt itself is still
        # deliberately absent, and the module docstring says why.
        "bookOrder": [_clean({"ord": m.get("ord"), "type": m.get("type"),
                              "gist": m.get("gist"), "page": m.get("page"),
                              "section": m.get("section"),
                              "fidelity": m.get("fidelity"),
                              "verbatim": m.get("verbatim")})
                      for m in topic_moves],
        "movesSource": spec.get("moves_source") or "none",
        # False rather than absent when the book's order matches the canonical
        # six: "we checked and it agreed" and "we never looked" are different
        # facts, and only one of them means the section order below is arbitrary.
        "movesReordered": (moves_module.reordered(topic_moves)
                           if topic_moves else None),
        "stagingNote": (moves_module.staging_note(topic_moves)
                        if topic_moves else ""),
    }


def _academic(spec: dict, reasoning_slice: dict) -> dict:
    """The fixed destination: what this topic requires a child to end up knowing.

    Everything in here is under `PRESERVE`. `masteryTarget` and `gained` are both
    carried and they are not the same claim — `gained` is what THIS period leaves,
    `masteryTarget` is what the idea requires of a child asked to decide a case
    nobody showed them. A route adapted to the first alone teaches the page's own
    example, which is the failure the mastery pass in `agents/reasoning.py`
    exists to catch.
    """
    knowledge = spec.get("knowledge") or {}
    return {
        "masteryTarget": reasoning_slice.get("masteryTarget") or "",
        "requiredConcepts": list(knowledge.get("concepts") or []),
        "competencies": list(knowledge.get("competencies") or []),
        "vocabulary": list(knowledge.get("vocabulary") or []),
        "learningOutcomes": list(knowledge.get("learning_outcomes") or []),
        "contexts": list(knowledge.get("contexts") or []),
        "bloomLevel": knowledge.get("bloom_level"),
        "difficulty": knowledge.get("difficulty"),
        # The shared-library ids, so Node 2 and anything after it can talk about
        # "concept 4f2a" rather than about a string that two topics spell
        # differently. Names, not the full rows: the rows are a join away.
        "canonical": {kind: sorted(names)
                      for kind, names in (spec.get("canonical_names") or {}).items()
                      if names},
    }


def _chain(reasoning_slice: dict) -> dict:
    """This topic's link in the chapter's knowledge chain."""
    return {
        "strand": reasoning_slice.get("strand"),
        "gained": reasoning_slice.get("gained") or "",
        "bridgesTo": reasoning_slice.get("bridgesTo") or "",
        "assumes": reasoning_slice.get("assumes") or "",
    }


def _mastery_audit(reasoning_slice: dict) -> dict:
    """What the printed page supplies towards the target, and what it does not.

    This is the field Node 2 has the most legitimate use for and the most room to
    misread, so both halves are carried. `missing` is an audited shortfall list
    with a fixed taxonomy (`reasoning.MASTERY_KINDS`) — an `example` shortfall is
    an invitation to supply a local example and a `transfer` shortfall is not,
    and a contextual layer that treats them alike is decorating.
    """
    return {
        "supplied": list(reasoning_slice.get("supplied") or []),
        "missing": [{"kind": item.get("kind"), "missing": item.get("missing")}
                    for item in (reasoning_slice.get("missing") or [])
                    if item.get("kind")],
    }


def _experience(experience: dict, reasoning_slice: dict) -> dict:
    """The cognitive path, and the object that makes it happen.

    `anchor` is where a contextual layer most obviously wants to intervene, and
    `anchorReason` is carried beside it for exactly that reason: the object was
    chosen because it makes the trajectory happen, not because it was familiar.
    A local substitution that keeps the familiarity and loses the mechanism is
    the "localization theater" failure mode in §18, and this is the field that
    lets Node 2 — or Node 3 checking it — tell the two apart.
    """
    return {
        "trajectory": list(experience.get("trajectory") or []),
        "conceptualJump": experience.get("conceptualJump") or "",
        "anchor": experience.get("anchor") or "",
        "anchorReason": experience.get("anchorReason") or "",
        "studentAction": experience.get("studentAction") or "",
        "inference": experience.get("inference") or "",
        # The pool the anchor was chosen FROM, not just the one chosen. Node 2's
        # local-relevance factor is the one most likely to want a different
        # object, and the alternatives that were already judged to fit this
        # topic's strand are cheaper — and safer — than an invented one.
        "anchorPool": list(reasoning_slice.get("anchors") or []),
        # Which audited shortfall THIS period closes, and what stages it. One of
        # `masteryAudit.missing`, already chosen — Node 2 is not being asked to
        # choose again.
        "closesGap": _clean({
            "kind": experience.get("gapKind"),
            "gap": experience.get("gap"),
            "closer": experience.get("gapCloser"),
        }),
    }


def _lesson_plan(plan: dict, spec: dict) -> dict:
    """The period around the path: minutes, emphasis, and the seam to the next.

    `sectionOrder` is the book's order where move extraction found one and the
    canonical six otherwise, resolved here rather than left for the reader to
    derive — two readers deriving it separately is how a sheet and its validator
    came to disagree about what order the chapter taught in.
    """
    topic_moves = spec.get("moves") or []
    order = (moves_module.teaching_order(topic_moves) if topic_moves
             else list(SECTION_ORDER))
    return {
        "sectionOrder": order,
        "minutes": dict(plan.get("minutes") or {}),
        # Time freed by Explore's hard 5-minute ceiling (see planning.py's
        # EXPLORE_CEILING) — never one of the six sections, always a sibling
        # field, so nothing downstream mistakes it for a section to write.
        "flexMinutes": plan.get("flexMinutes") or 0,
        "emphasis": dict(plan.get("emphasis") or {}),
        "newVocabulary": list(plan.get("newVocabulary") or []),
        "exploreHook": dict(plan.get("exploreHook") or {}),
    }


def _selected_activity(selection: dict) -> dict:
    """The one Challenge activity this topic was given, without its template.

    The template body is several kilobytes of setup, variants and materials, it
    is already a row in `activity_templates`, and Node 2's decision about it is
    binary and cheap: can this be run in the room as recorded. `materials` is
    carried because that decision cannot be made without it — a feasibility
    check against an activity whose materials are a join away is a feasibility
    check nobody runs.
    """
    activity = selection.get("activity") or {}
    context = selection.get("context") or {}
    return {
        "id": activity.get("id"),
        "name": activity.get("name"),
        "category": activity.get("category"),
        "source": activity.get("source") or ("library" if activity.get("id")
                                             else "invented"),
        "materials": list(activity.get("materials") or []),
        "grouping": activity.get("grouping"),
        "durationMinutes": activity.get("duration_minutes") or activity.get("durationMinutes"),
        "context": context.get("name") if isinstance(context, dict) else context,
        "formats": dict(selection.get("formats") or {}),
        "rationale": selection.get("rationale") or selection.get("reason") or "",
        "candidateCount": selection.get("candidateCount", 0),
    }


def _section_spec(plan: dict, spec: dict) -> list[dict]:
    """The generation-ready lesson specification: six sections, in order.

    §3.1 of the implementation framework asks Node 1 for a "generation-ready
    lesson specification" and §12 maps each of the six sections to the kind of
    contextual influence it can take. This is the join between them — one row per
    section, in the order this topic actually teaches, carrying the minutes it
    was allocated, how much of its substance is the book's (`mode`) and where
    that substance comes from (`source`).

    `mode` is the field that makes §12 actionable rather than advisory. A
    `grounded` section's substance is the book's, so contextual reinforcement
    there can change the route in and the words around it but not the claim; a
    `creative` section is where a verified local example legitimately belongs.
    Node 2 proposing a new Real Life scenario and Node 2 proposing a new Concept
    definition are different acts, and without this they look identical.
    """
    minutes = plan.get("minutes") or {}
    emphasis = plan.get("emphasis") or {}
    topic_moves = spec.get("moves") or []
    order = (moves_module.teaching_order(topic_moves) if topic_moves
             else list(SECTION_ORDER))
    rows = []
    for position, section in enumerate(order, start=1):
        policy = SECTION_POLICY.get(section) or {}
        rows.append(_clean({
            "section": section,
            "label": SECTION_LABELS.get(section, section),
            "position": position,
            "minutes": minutes.get(section, policy.get("minutes")),
            "mode": policy.get("mode"),
            "source": policy.get("source"),
            "audience": policy.get("audience"),
            "emphasis": ("heaviest" if emphasis.get("heaviest") == section else
                         "lightest" if emphasis.get("lightest") == section else None),
        }))
    return rows


# What a topic must carry before anything downstream can use it. Ordered by when
# it was derived, so the first missing entry names the stage that did not finish
# rather than the last one that noticed.
_REQUIRED = (
    ("masteryTarget", lambda t: bool((t.get("academic") or {}).get("masteryTarget"))),
    ("requiredConcepts", lambda t: bool((t.get("academic") or {}).get("requiredConcepts"))),
    ("knowledgeChain", lambda t: bool((t.get("knowledgeChain") or {}).get("gained"))),
    ("experiencePlan", lambda t: bool((t.get("experiencePlan") or {}).get("trajectory"))),
    ("lessonPlan", lambda t: bool((t.get("lessonPlan") or {}).get("minutes"))),
    ("selectedActivity", lambda t: bool((t.get("selectedActivity") or {}).get("name"))),
)


def _readiness(topic: dict) -> dict:
    missing = [name for name, present in _REQUIRED if not present(topic)]
    return {"ready": not missing, "missing": missing}


def build_topic_contract(spec: dict, *, reasoning_slice: dict, experience: dict,
                         plan: dict, selection: dict) -> dict:
    """One topic's slice of the contract. Pure — no state, no I/O, no model."""
    topic: dict = {
        "index": spec.get("index"),
        "topic": spec.get("topic") or "",
        "subtopic": spec.get("subtopic") or "",
        "grounding": _grounding(spec),
        "academic": _academic(spec, reasoning_slice or {}),
        "knowledgeChain": _chain(reasoning_slice or {}),
        "masteryAudit": _mastery_audit(reasoning_slice or {}),
        "misconceptions": list((reasoning_slice or {}).get("misconceptions") or []),
        "experiencePlan": _experience(experience or {}, reasoning_slice or {}),
        "lessonPlan": _lesson_plan(plan or {}, spec),
        "selectedActivity": _selected_activity(selection or {}),
        "sectionSpec": _section_spec(plan or {}, spec),
    }
    topic["readiness"] = _readiness(topic)
    # CLEANED FIRST, THEN FINGERPRINTED, and the order is load-bearing. `_clean`
    # drops empty fields, so a topic whose `assumes` is "" is transmitted without
    # it — and a fingerprint taken before the drop could never be reproduced from
    # the row anybody actually receives. Hashing the cleaned shape is what makes
    # `topic_fingerprint()` below able to recompute the same digest from a
    # contract that has been through a file, a database column and an HTTP
    # response.
    cleaned = _clean(topic)
    cleaned["fingerprint"] = topic_fingerprint(cleaned)
    return cleaned


def topic_fingerprint(topic: dict) -> str:
    """One topic's digest, over the preserved fields ALONE.

    Not the whole topic. Node 2 is expected to come back having changed the route
    — a different anchor, a substituted activity, an extra language step — and a
    fingerprint over everything would break on every successful adaptation and
    prove nothing about the target.

    THE ONE PLACE THIS FORMULA EXISTS. `build_topic_contract` stamps it and
    `verify` recomputes it; a second spelling anywhere would mean a contract that
    verifies against itself and nothing else.
    """
    academic = topic.get("academic") or {}
    return fingerprint({
        "masteryTarget": academic.get("masteryTarget") or "",
        "requiredConcepts": sorted(academic.get("requiredConcepts") or []),
        "competencies": sorted(academic.get("competencies") or []),
        "knowledgeChain": topic.get("knowledgeChain") or {},
    })


# ── Chapter-wide ─────────────────────────────────────────────────────────────

def build_chapter_contract(state: dict) -> dict:
    """Node 1's whole output for one chapter — what Node 2 is handed.

    Built from the final state at persist time, for the same reason
    `provenance.build_run_provenance` is: this is the one point at which every
    stage's contribution exists at once, so the contract is composed from what
    actually happened rather than from a running commentary a later stage might
    still revise.
    """
    topics = state.get("topics") or []
    reasoning = state.get("reasoning") or {}
    chain = reasoning.get("chain") or {}
    experience = state.get("experience") or {}
    plans = state.get("plans") or {}
    selections = state.get("selections") or {}

    rows = []
    for spec in topics:
        index = spec.get("index")
        # `spec["reasoning"]` is the per-topic slice the reasoning node copies
        # onto each TopicSpec; `chain[index]` is the same data on the chapter-wide
        # object. Preferring the spec means a topic repaired or re-derived in
        # isolation carries its own slice — and falling back to the chain means a
        # chapter whose reasoning came from cache still resolves.
        slice_ = spec.get("reasoning") or chain.get(index) or {}
        rows.append(build_topic_contract(
            spec,
            reasoning_slice=slice_,
            experience=experience.get(index) or spec.get("experience") or {},
            plan=plans.get(index) or spec.get("plan") or {},
            selection=selections.get(index) or spec.get("selection") or {},
        ))

    settings = state.get("teacher_settings") or {}
    contract = {
        "contractVersion": CONTRACT_VERSION,
        "producedBy": "node1.textbook_representation",
        "consumedBy": "node2.contextual_reinforcement",
        "runId": state.get("run_id"),
        "threadId": state.get("thread_id"),
        "grade": str(state.get("grade") or ""),
        "subject": state.get("subject") or "",
        "chapter": _clean({
            "number": state.get("chapter_number"),
            "title": state.get("chapter_title") or "",
            "pageStart": state.get("page_start"),
            "pageEnd": state.get("page_end"),
            "arc": state.get("chapter_arc") or "",
            "sequencingNote": state.get("sequencing_note") or "",
        }),
        # Chapter-scope, not topic-scope: a prerequisite the whole chapter assumes
        # and a misconception several topics share are stated once. A contextual
        # layer reading them per topic would propose the same bridge six times,
        # which is the "teacher overload" failure in §18.
        "chapterWide": _clean({
            "prerequisites": list(reasoning.get("prerequisites") or []),
            "misconceptions": [
                {"belief": m.get("belief"), "topics": list(m.get("topics") or [])}
                for m in (reasoning.get("misconceptions") or []) if m.get("belief")
            ],
            "anchors": [
                {"strand": a.get("strand"), "objects": list(a.get("objects") or [])}
                for a in (reasoning.get("anchors") or []) if a.get("objects")
            ],
        }),
        # The conditions Node 1 was told about — which is NOT the context profile.
        # Node 2 builds that itself from school/class data (§3.2) and is the node
        # that decides what any of it justifies. This is only what Node 1 already
        # planned against, carried so Node 2 can see what its own profile would
        # contradict rather than silently re-deciding it.
        "deliveryAssumptions": _clean({
            "durationMinutes": settings.get("duration"),
            "classSize": settings.get("classSize"),
            "resourceLevel": settings.get("resourceLevel"),
            "language": settings.get("language"),
            "learningObjective": settings.get("learningObjective"),
            "teachingStyle": settings.get("teachingStyle"),
            "plainLanguage": settings.get("plainLanguage"),
            "teacherPreferences": state.get("teacher_preferences"),
        }),
        "sectionContract": list(SECTION_ORDER),
        "topics": rows,
        # Stated rather than implied. §2's "Fixed academic destination" is a rule
        # about these five names, and a downstream node that has to infer which
        # fields were fixed will infer a different set than the one meant.
        "preserve": list(PRESERVE),
    }

    ready = [r for r in rows if (r.get("readiness") or {}).get("ready")]
    contract["readiness"] = {
        "topicsTotal": len(rows),
        "topicsReady": len(ready),
        # The chapter is generation-ready when EVERY topic is. Not a majority:
        # the Refresher of T(n) is written from the Explore of T(n-1), so one
        # unready topic breaks the seam on the one after it too.
        "generationReady": bool(rows) and len(ready) == len(rows),
        "notReady": [{"index": r.get("index"),
                      "missing": (r.get("readiness") or {}).get("missing") or []}
                     for r in rows
                     if not (r.get("readiness") or {}).get("ready")],
    }
    contract["integrity"] = {
        "preserveFields": list(PRESERVE),
        "topicFingerprints": {str(r.get("index")): r.get("fingerprint")
                              for r in rows if r.get("fingerprint")},
        "chapterFingerprint": fingerprint({
            "prerequisites": sorted(reasoning.get("prerequisites") or []),
            "topics": {str(r.get("index")): r.get("fingerprint") for r in rows},
        }),
    }
    return contract


def verify(contract: dict, returned: dict) -> dict:
    """Did anything downstream move the academic destination?

    The deterministic half of the equity/safety gate (§10), available to Node 3
    without Node 3 needing to know what any of these fields mean.

    IT RECOMPUTES. The first version of this read `returned["integrity"]` — the
    fingerprints the returned document asserts about itself — and that is not a
    check, it is a receipt. A node that edited a mastery target and left the
    integrity block untouched walked straight through, which is precisely the
    one thing this function exists to catch, and it took a Node 3 test tampering
    with a contract to notice.

    So the digests are computed HERE, from the returned rows' actual fields, with
    `topic_fingerprint()` — the same formula `build_topic_contract` stamped with,
    called rather than repeated so the two can never hash a different set of
    fields.

    `expected` still comes from the ORIGINAL contract's stored block, and that is
    correct: the original is the thing being trusted, and it is held by whoever
    is doing the verifying.

    `recomputed` in the result says which way the comparison was made. A returned
    document carrying no topics leaves nothing to recompute from — that falls
    back to comparing the stored blocks, which is weaker, and the flag is how a
    caller can tell it got the weaker answer.
    """
    expected = (contract.get("integrity") or {}).get("topicFingerprints") or {}
    rows = returned.get("topics") or []

    if rows:
        actual = {str(r.get("index")): topic_fingerprint(r)
                  for r in rows if r.get("index") is not None}
        recomputed = True
    else:
        actual = (returned.get("integrity") or {}).get("topicFingerprints") or {}
        recomputed = False

    changed, missing = [], []
    for index, digest in expected.items():
        if index not in actual:
            missing.append(index)
        elif actual[index] != digest:
            changed.append(index)

    def _order(values):
        return sorted(values, key=lambda i: int(i) if str(i).isdigit() else 0)

    return {
        "preserved": not changed and not missing,
        "changed": _order(changed),
        "missing": _order(missing),
        "checked": len(expected),
        "recomputed": recomputed,
    }


def summary(contract: dict) -> str:
    """One line, for a terminal or an SSE event."""
    readiness = contract.get("readiness") or {}
    chapter = contract.get("chapter") or {}
    return (f"contract v{contract.get('contractVersion')} — "
            f"{readiness.get('topicsReady', 0)}/{readiness.get('topicsTotal', 0)} "
            f"topic(s) generation-ready | "
            f"{chapter.get('title') or 'chapter'} | "
            f"fingerprint {(contract.get('integrity') or {}).get('chapterFingerprint', '')[:12]}")
