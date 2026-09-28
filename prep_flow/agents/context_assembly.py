"""Context Assembly Agent — the join where the five inputs meet.

    Chapter Generation Batch  ─┐
    Topic sequence            ─┤
    Competencies              ─┼─> assembled context ─> Planning
    Activity data             ─┤
    Feedback Optimization State ┘

This is also the node the feedback loop feeds back into: the adaptive
generation state is fetched here, once for the whole run, and travels with the
batch from this point on. Fetching it per topic would let a mid-run state change
split a chapter across two policies, which is precisely the inconsistency the
batch exists to prevent.

Per topic it does the pilot's Stages 1–3 unchanged — extract knowledge from the
topic's own textbook slice, resolve it onto the canonical library, look up
matching activity templates — fanned out with a bounded gate. Reusing the pilot's
extraction here is deliberate: material from the two pipelines must remain
comparable, and a second extraction prompt would quietly make it not.
"""
from ..deps import engagement_level_guidance, extract_knowledge_from_text
from ..llm import CALL_LABEL, gather_bounded
from ..state import ChapterState
from ..tools import adaptive_state as fetch_adaptive_state
from ..tools import (
    class_context,
    map_to_library_competencies,
    pedagogy_activities,
    resolve_knowledge,
)


async def _extract_topic(spec: dict, grade: str, subject: str) -> dict:
    """Stage 1 for one topic, plus canonical resolution for everything EXCEPT
    competencies.

    Never raises: a topic whose extraction fails still generates — with an empty
    knowledge block and no matched activity, which the prompt and the validator
    both handle — and losing a whole chapter to one bad slice would be a far
    worse outcome.

    Competencies are deliberately excluded from `resolve_canonical` here. That
    function is biased toward creating a new entry over merging, which is right
    for concepts and wrong for a join key: it would file "Identify family
    members" as a brand-new competency that no activity template points at, once
    per topic, for every book. They are mapped onto the EXISTING library
    instead — see tools.map_to_library_competencies.
    """
    excerpt = spec.get("excerpt") or ""
    knowledge = {
        "concepts": [], "competencies": [], "vocabulary": [],
        "learning_outcomes": [], "contexts": [], "bloom_level": None, "difficulty": None,
    }
    if excerpt.strip():
        # Labelled around the await, not inside it: this is the pilot's own
        # prompt and its own call_ai call, so it is the only model call this
        # node makes that carries no label of its own. Without this the run
        # trace reports one "(unlabelled)" call per topic — attributed to this
        # agent by its stack, but with nothing saying WHICH topic it extracted.
        token = CALL_LABEL.set(f"knowledge-extraction[T{spec['index']}]")
        try:
            knowledge = await extract_knowledge_from_text(
                spec["topic"], spec.get("subtopic") or "", excerpt, grade, subject)
        except Exception as exc:
            print(f"[prep_flow:context] T{spec['index']} extraction failed: {exc}")
        finally:
            CALL_LABEL.reset(token)

    seeded: dict = {}
    canonical = await resolve_knowledge(
        knowledge, kinds=("concepts", "vocabulary", "contexts"), capture_seeded=seeded)
    return {
        **spec,
        "knowledge": knowledge,
        "canonical": {kind: sorted(mapping.values()) for kind, mapping in canonical.items()},
        "canonical_names": {kind: sorted(mapping) for kind, mapping in canonical.items()},
        # Which of THIS topic's names were newly written into the shared library
        # versus already there — the seeding half of this node's provenance.
        # Competencies are absent on purpose: they are read-only-matched below,
        # via map_to_library_competencies, never created.
        "canonical_seeded": seeded,
        "activities": [],
    }


async def context_assembly_node(state: ChapterState) -> dict:
    topics = state.get("topics") or []
    if not topics:
        return {"status": "failed", "errors": ["context assembly: no topics to assemble"]}

    config = state.get("config") or {}
    settings = state.get("teacher_settings") or {}
    grade, subject = state.get("grade", ""), state.get("subject", "")

    adaptive = await fetch_adaptive_state(state.get("school_id"), grade, subject)
    cohort = await class_context(
        state.get("class_id"), settings.get("teacherId"), state.get("chapter_title") or "")

    results = await gather_bounded(
        [_extract_topic(spec, grade, subject) for spec in topics],
        limit=int(config.get("concurrency", 4)),
    )

    assembled: list[dict] = []
    errors: list[str] = []
    for spec, result in zip(topics, results):
        if isinstance(result, BaseException):
            errors.append(f"context assembly: T{spec['index']} '{spec['topic']}': {result}")
            assembled.append({**spec, "knowledge": {}, "canonical": {},
                              "canonical_names": {}, "canonical_seeded": {}, "activities": []})
        else:
            assembled.append(result)

    # ONE mapping call for the whole chapter, after extraction rather than during
    # it. Per topic it would re-send the entire competency library once per
    # topic — forty times for a full chapter, for an answer that depends on the
    # same library every time.
    mapping = await map_to_library_competencies(
        {t["index"]: (t.get("knowledge") or {}).get("competencies") or []
         for t in assembled},
        grade=grade)

    resource_level = int(settings.get("resourceLevel", 0))
    lookups = await gather_bounded(
        [pedagogy_activities(mapping.get(t["index"], {}).get("ids") or [],
                             grade, resource_level)
         for t in assembled],
        limit=int(config.get("concurrency", 4)))

    for topic, activities in zip(assembled, lookups):
        entry = mapping.get(topic["index"], {})
        topic["activities"] = [] if isinstance(activities, BaseException) else activities
        topic["canonical"]["competencies"] = entry.get("ids") or []
        topic["canonical_names"]["competencies"] = sorted(
            {name for names in (entry.get("matched") or {}).values() for name in names})
        topic["competency_mapping"] = entry

    matched = sum(1 for t in assembled if t.get("activities"))
    grounded = sum(1 for t in assembled if (t.get("excerpt") or "").strip())
    unmapped = sorted({name for t in assembled
                       for name in (t.get("competency_mapping") or {}).get("unmatched", [])})
    # The provenance number: how many rows this run actually wrote into the
    # shared canonical library, across every topic and every kind resolved.
    seeded_total = sum(
        len(entry.get("created") or [])
        for t in assembled
        for entry in (t.get("canonical_seeded") or {}).values()
    )
    if unmapped:
        # Named, not just counted. These are the real coverage gaps — the skills
        # a curated library genuinely does not cover — and they are the shortlist
        # for whoever authors the next batch of templates.
        print(f"[prep_flow:context] {len(unmapped)} extracted competenc(ies) matched "
              f"nothing in the library: {', '.join(unmapped[:10])}"
              + ("…" if len(unmapped) > 10 else ""))

    return {
        "topics": assembled,
        "adaptive_state": adaptive,
        "class_context": cohort,
        "engagement_guidance": engagement_level_guidance(
            grade, plain=bool(settings.get("plainLanguage"))),
        "context_ready": True,
        "errors": errors,
        "unmapped_competencies": unmapped,
        "metrics": {
            "topics_with_textbook": grounded,
            "topics_with_activity_match": matched,
            "competencies_mapped": sum(len((t.get("competency_mapping") or {}).get("matched", {}))
                                       for t in assembled),
            "competencies_unmapped": len(unmapped),
            "adaptive_state_version": adaptive.get("_version"),
            "canonical_entries_created": seeded_total,
        },
    }
