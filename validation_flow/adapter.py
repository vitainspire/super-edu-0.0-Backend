"""Contract in, the shape the checks already speak.

`checks.py` and `learner.py` came back from `_deferred/` unchanged in substance,
and unchanged is the point: the anchor pools grouped per strand, the two-tier
number check, the learner gate's verdict words chosen so they cannot be copied
out of a JSON example — all of it is hard-won, and a rewrite to fit a new input
shape would have put every line of it at risk to save one adapter.

So the adapter exists instead. It reads Node 1's contract and rebuilds the
`ChapterState`-shaped dict those checks were written against. Nothing is
recomputed and nothing is inferred: every field below is a rename.

TWO FIELDS CANNOT BE RENAMED INTO EXISTENCE, and both are the same omission.

    excerpt            the topic's own textbook text
    chapter_markdown   the whole chapter's

The contract deliberately carries neither — see `prep_flow/contract.py`, which
explains why at length: the excerpt is the largest field in the state by an order
of magnitude, Node 2 has no use for it, and carrying it would make every logged
contract several times larger than the thing it describes.

Node 3 DOES have a use for it. `check_concept_grounding` and
`check_book_fidelity` are the two checks that ask whether the sheet stayed inside
the book, and a grounding check with no book passes anything fluent.

So the source is an OPTIONAL, SEPARATE input (`sources=`), fetched from the pages
the contract names. When it is supplied those checks run in full. When it is not,
`check_concept_grounding` already says so — it emits an advisory naming the gap
rather than returning clean, which is the behaviour that makes this safe to leave
optional. `verdict.py` reports `groundingChecked` either way, so a run judged
without the book can never be mistaken for one judged with it.
"""
from typing import Optional


def _knowledge(row: dict) -> dict:
    """`academic` back into the `knowledge` shape the checks read."""
    academic = row.get("academic") or {}
    return {
        "concepts": list(academic.get("requiredConcepts") or []),
        "competencies": list(academic.get("competencies") or []),
        "vocabulary": list(academic.get("vocabulary") or []),
        "learning_outcomes": list(academic.get("learningOutcomes") or []),
        "contexts": list(academic.get("contexts") or []),
        "bloom_level": academic.get("bloomLevel"),
        "difficulty": academic.get("difficulty"),
    }


def _reasoning_slice(row: dict, chapter_anchors: dict) -> dict:
    """The per-topic reasoning slice, reassembled from three contract blocks.

    `agents/reasoning.py::topic_reasoning` built this originally and the contract
    split it across `knowledgeChain`, `masteryAudit` and `misconceptions` because
    those are three different questions to a reader. Putting it back is a rename,
    not a re-derivation — the values are identical.
    """
    chain = row.get("knowledgeChain") or {}
    audit = row.get("masteryAudit") or {}
    strand = chain.get("strand") or "main"
    return {
        "strand": strand,
        # From the topic's own `anchorPool` where it has one, and from the
        # chapter's per-strand pool otherwise. Both are the same list — the
        # contract carries it in both places because Node 2 needs it per topic —
        # and preferring the topic's own keeps a re-derived topic self-consistent.
        "anchors": list((row.get("experiencePlan") or {}).get("anchorPool")
                        or chapter_anchors.get(strand) or []),
        "gained": chain.get("gained") or "",
        "bridgesTo": chain.get("bridgesTo") or "",
        "assumes": chain.get("assumes") or None,
        "misconceptions": list(row.get("misconceptions") or []),
        "masteryTarget": audit.get("masteryTarget") or (row.get("academic") or {}).get("masteryTarget") or "",
        "supplied": list(audit.get("supplied") or []),
        "missing": list(audit.get("missing") or []),
    }


def _experience(row: dict) -> dict:
    """`experiencePlan` back into the experience plan the checks read.

    `derived` is the one field that is computed rather than renamed, and it is
    computed the same way `contract.readiness` computes it: a plan with a
    trajectory is a real plan, and one without is the fallback
    `agents/experience.py::_fallback` produces. `validation_node` branches on it
    — a real plan is checked against its own chosen object, a fallback against
    the strand's whole pool — and getting it wrong would check a sheet against
    the wrong standard rather than fail loudly.
    """
    experience = row.get("experiencePlan") or {}
    closes = experience.get("closesGap") or {}
    return {
        "index": row.get("index"),
        "trajectory": list(experience.get("trajectory") or []),
        "conceptualJump": experience.get("conceptualJump") or "",
        "anchor": experience.get("anchor") or "",
        "anchorReason": experience.get("anchorReason") or "",
        "studentAction": experience.get("studentAction") or "",
        "inference": experience.get("inference") or "",
        "gap": closes.get("gap") or "",
        "gapKind": closes.get("kind") or "",
        "gapCloser": closes.get("closer") or "",
        "derived": bool(experience.get("trajectory")),
        "misconception": next(iter(row.get("misconceptions") or []), ""),
        "forwardBridge": (row.get("knowledgeChain") or {}).get("bridgesTo") or "",
        "aimedAtMastery": bool((row.get("masteryAudit") or {}).get("masteryTarget")),
    }


def _spec(row: dict, chapter_anchors: dict, excerpt: str) -> dict:
    grounding = row.get("grounding") or {}
    return {
        "index": row.get("index"),
        "topic": row.get("topic") or "",
        "subtopic": row.get("subtopic") or "",
        "page_start": grounding.get("pageStart"),
        "page_end": grounding.get("pageEnd"),
        "anchor_kind": grounding.get("anchorKind"),
        "excerpt": excerpt or "",
        "figures": list(grounding.get("figures") or []),
        # Carries `fidelity` and `verbatim`, which is why the contract was
        # changed to emit them: `check_book_fidelity` compares the sheet against
        # exactly those quotes, and without them it has nothing to compare.
        "moves": list(grounding.get("bookOrder") or []),
        "moves_source": grounding.get("movesSource") or "none",
        "knowledge": _knowledge(row),
        "reasoning": _reasoning_slice(row, chapter_anchors),
    }


def to_state(contract: dict, *, materials: dict,
             sources: Optional[dict] = None,
             chapter_text: str = "",
             config: Optional[dict] = None) -> dict:
    """The dict `checks.validation_node` and `learner.simulation_node` read.

    `materials` is index -> the generated sheet. `sources` is index -> that
    topic's textbook excerpt, and both it and `chapter_text` are optional; see
    the module docstring for what is lost without them.

    Index keys are normalised to `int` throughout. The contract survives a JSON
    round trip on its way here — through a file, through a database column,
    through an HTTP response — and JSON has no integer keys, so a caller handing
    back what it was given would otherwise index every map with a string and find
    nothing, silently, on every topic.
    """
    rows = [r for r in (contract.get("topics") or []) if r.get("index") is not None]
    chapter_wide = contract.get("chapterWide") or {}
    chapter_anchors = {a.get("strand"): list(a.get("objects") or [])
                       for a in (chapter_wide.get("anchors") or []) if a.get("strand")}

    sources = {int(k): v for k, v in (sources or {}).items()}
    materials = {int(k): v for k, v in (materials or {}).items()}

    topics, plans, selections, experience, chain = [], {}, {}, {}, {}
    for row in rows:
        index = int(row["index"])
        topics.append(_spec(row, chapter_anchors, sources.get(index, "")))
        lesson = row.get("lessonPlan") or {}
        plans[index] = {
            "index": index,
            "minutes": dict(lesson.get("minutes") or {}),
            "emphasis": dict(lesson.get("emphasis") or {}),
            "newVocabulary": list(lesson.get("newVocabulary") or []),
            "exploreHook": dict(lesson.get("exploreHook") or {}),
        }
        activity = row.get("selectedActivity") or {}
        selections[index] = {
            "activity": {"id": activity.get("id"), "name": activity.get("name"),
                         "category": activity.get("category"),
                         "source": activity.get("source")},
            "context": {"name": activity.get("context")},
            "formats": dict(activity.get("formats") or {}),
        }
        experience[index] = _experience(row)
        chain[index] = {"strand": (row.get("knowledgeChain") or {}).get("strand"),
                        "gained": (row.get("knowledgeChain") or {}).get("gained"),
                        "bridgesTo": (row.get("knowledgeChain") or {}).get("bridgesTo"),
                        "assumes": (row.get("knowledgeChain") or {}).get("assumes")}

    delivery = contract.get("deliveryAssumptions") or {}
    chapter = contract.get("chapter") or {}
    return {
        "grade": contract.get("grade"),
        "subject": contract.get("subject"),
        "chapter_title": chapter.get("title"),
        "chapter_number": chapter.get("number"),
        "chapter_arc": chapter.get("arc"),
        "chapter_markdown": chapter_text or "",
        "topics": topics,
        "materials": materials,
        "plans": plans,
        "selections": selections,
        "experience": experience,
        "reasoning": {
            "prerequisites": list(chapter_wide.get("prerequisites") or []),
            "misconceptions": [{"belief": m.get("belief"),
                                "topics": list(m.get("topics") or [])}
                               for m in (chapter_wide.get("misconceptions") or [])],
            "anchors": [{"strand": s, "objects": o} for s, o in chapter_anchors.items()],
            "chain": chain,
        },
        "teacher_settings": {
            "duration": delivery.get("durationMinutes") or 30,
            "classSize": delivery.get("classSize") or 40,
            "language": delivery.get("language") or "English",
            "resourceLevel": delivery.get("resourceLevel") or 0,
        },
        "config": dict(config or {}),
    }


def sources_from_chapter(contract: dict, chapter_text: str) -> dict:
    """Cut each topic's excerpt back out of a chapter, by the pages it names.

    The convenience path for the common case: a caller that still has the whole
    chapter should not have to slice it per topic to get the grounding checks.
    Uses the same page index `prep_flow/tools.py` built the excerpts with, so the
    text a check sees is the text the topic was actually derived from.

    PER-TOPIC, NOT ALL-OR-NOTHING. A topic with no page numbers falls back to
    `excerptStart`/`excerptEnd` — the exact char offsets `sequencing.py` cut it
    from, carried in the contract for exactly this. Not a proportional guess:
    the literal same span the topic was originally derived from. Kept in sync
    with generation/adapt.py::sources_from_chapter, the same fix for the same
    bug — see that docstring for the real case this exists for.
    """
    if not (chapter_text or "").strip():
        return {}

    spans = []
    try:
        from prep_flow.tools import page_index
        spans = page_index(chapter_text) or []
    except ImportError:
        pass

    out: dict[int, str] = {}
    for row in contract.get("topics") or []:
        grounding = row.get("grounding") or {}
        start, end = grounding.get("pageStart"), grounding.get("pageEnd")
        text = ""
        if start is not None and spans:
            end = end if end is not None else start
            chunks = [chapter_text[s:e] for page, s, e in spans if start <= page <= end]
            text = "".join(chunks)
        if not text:
            c_start, c_end = grounding.get("excerptStart"), grounding.get("excerptEnd")
            if c_start is not None and c_end is not None:
                text = chapter_text[c_start:c_end]
        if text:
            out[int(row["index"])] = text
    return out
