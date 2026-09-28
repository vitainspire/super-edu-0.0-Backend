"""Contract in, the shape `compose.py` reads.

The same job `validation_flow/adapter.py` does for Node 3, and a SEPARATE FILE on
purpose. The two look alike today and diverge the moment either stage needs a
field the other does not — generation wants the excerpt on every topic because
the Concept section is written from it, where Node 3 wants it only to check what
was written. A shared adapter would make one stage's needs the other's
constraint, and the duplication here is a few dozen lines of renaming.

`materials` is absent from what this builds, unlike Node 3's: generation
PRODUCES it. `graph.py` threads it back in as each window completes, because the
next window's Refresher is written from the last one's Explore.
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


def to_state(contract: dict, *, sources: Optional[dict] = None,
             chapter_text: str = "", config: Optional[dict] = None) -> dict:
    """The dict `checks.validation_node` and `learner.simulation_node` read.

    `sources` is index -> that topic's textbook excerpt. Optional, but far more
    load-bearing here than in Node 3: the Concept section is WRITTEN from the
    excerpt, so a generation run without it produces a sheet whose Concept is
    limited to what the topic title implies. `compose.py` says so in the prompt
    rather than inventing facts.

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

    topics, plans, selections, experience, chain = [], {}, {}, {}, {}
    for row in rows:
        index = int(row["index"])
        spec = _spec(row, chapter_anchors, sources.get(index, ""))
        # On the spec, because `compose._topic_block` reads `spec["experience"]`
        # rather than a parallel map. Node 3's adapter puts it in `experience`
        # because its checks read it there; the two stages genuinely differ here.
        spec["experience"] = _experience(row)
        topics.append(spec)
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
            "duration": delivery.get("durationMinutes") or 45,
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
    from, carried in the contract for exactly this. This is not a proportional
    guess: it is the literal same span the topic was originally derived from, so
    it carries none of the neighbouring-topic-contamination risk a guess would.
    Only a topic with neither pages nor offsets is skipped.

    Real case this exists for: published-book chapters fetched with no
    `<!-- page N -->` markers at all (confirmed against the live textbook
    catalog API) leave every topic's pageStart/pageEnd null. Before this
    fallback, that meant EVERY topic in such a chapter got no excerpt at all —
    Concept had nothing to cite, and Node 3 flagged every sheet for a
    non-existent page.
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


def apply_anchor_substitutions(state: dict, context_plan) -> int:
    """Rewrite each topic's anchor where the room cannot supply the original.

    Returns how many were swapped, for the run metrics. Edits the SPEC, not the
    contract: the contract is the closed document Node 1 signed, and a stage
    that quietly rewrote it would make the fingerprint a lie. What generation
    writes from is the spec, so that is what changes.

    `anchorReason` is rewritten too. Left alone it still argues for the object
    that is no longer there — "match-sticks are straight and identical, so a
    child can see the shape's boundary" printed beside paper strips reads as an
    editing mistake, and a teacher would be right to distrust the rest.
    """
    swaps = ((context_plan or {}).get("constraints") or {}).get("anchorSubstitutions") or {}
    if not swaps:
        return 0

    done = 0
    for spec in state.get("topics") or []:
        entry = swaps.get(str(spec.get("index")))
        if not entry or not entry.get("to"):
            continue
        experience = spec.get("experience") or {}
        if not experience:
            continue
        experience["anchor"] = entry["to"]
        experience["anchorReason"] = (
            f"{entry['why']}. Keep the thinking the original object carried; "
            f"only the object changed.")
        experience["anchorSubstitutedFrom"] = entry["from"]
        spec["experience"] = experience
        done += 1
    return done
