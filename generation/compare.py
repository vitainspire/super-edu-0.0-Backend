"""The A/B: generate the same chapter twice, with the context plan and without.

§16 asks for exactly this before Node 2 is allowed near production material —
"Run the existing Node 1 → Generation path. Run Node 2 independently against the
same lessons and log its proposed adaptations. Compare a 20–30 topic golden set
with and without contextual reinforcement." Shadow mode produces the plan; this
is the comparison that says whether the plan does anything.

THE ONE THING THAT MAKES THE COMPARISON MEAN ANYTHING is that the two arms
differ in exactly one input. Same contract, same topics, same plans, same
activities, same teacher settings, same model, same temperature — and one arm
gets `context_plan` while the other gets `None`. `generation/reinforcement.py`
returns an empty string when there is no plan, so the BASELINE arm's prompt is
byte-identical to what it would have been before Node 2 existed. That property is
asserted in the tests, because it is the whole experiment: if the two prompts
differed for any other reason, a difference in output would prove nothing.

WHAT IT CANNOT TELL YOU, said here because the app built on top of it will be
read as though it can. Generation runs at temperature 0.6. Two runs of the SAME
arm differ. So a difference between the arms is not automatically the plan's
doing, and this module reports a `noise` arm — a second baseline — for exactly
that reason. A change smaller than the gap between two baselines is not a
finding, and the viewer draws that line.

The comparison is DETERMINISTIC ABOUT ADOPTION and probabilistic about
everything else: whether a specific adaptation's words reached the sheet is a
fact (`validation_flow.integrity`), whether the sheet got better is a judgement
nobody here is qualified to make.
"""
import difflib
from typing import Optional

try:
    from prep_flow.llm import gather_bounded
    from prep_flow.sections import SECTION_LABELS, SECTION_ORDER, section_text
    from validation_flow.integrity import check_context_adoption
except ImportError:  # pragma: no cover
    from ..prep_flow.llm import gather_bounded
    from ..prep_flow.sections import SECTION_LABELS, SECTION_ORDER, section_text
    from ..validation_flow.integrity import check_context_adoption

from . import reinforcement
from .graph import run_generation


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in (text or "").replace("\n", " ").split(". ") if s.strip()]


def _landed(section: Optional[str], moved_above_noise: set, moved: set) -> str:
    """Where an adaptation asked for a change, did that change happen?

        yes         the named section is different in the adapted arm
        noise-only  it differs, but by no more than two baselines differ
        elsewhere   that section is untouched; some other section moved
        no          nothing on this sheet moved at all
        unplaced    the adaptation named no section, so there is nothing to check

    `elsewhere` is kept separate from `no` because the two mean opposite things
    about the generator: one used the adaptation and put it somewhere else, which
    is a placement problem; the other did not use it, which is the failure §18
    names.
    """
    if not section:
        return "unplaced"
    if section in moved_above_noise:
        return "yes"
    if section in moved:
        return "noise-only"
    return "elsewhere" if moved else "no"


def _section_diff(before: str, after: str) -> dict:
    """How far one section moved, as a ratio and as the actual added lines.

    `difflib.SequenceMatcher` on sentences rather than characters: a sheet that
    rephrases every sentence is a different sheet, and one that adds a sentence
    is a changed one — character-level similarity blurs the two into the same
    number.
    """
    a, b = _sentences(before), _sentences(after)
    ratio = difflib.SequenceMatcher(None, a, b).ratio() if (a or b) else 1.0
    added = [line for line in b if line not in a]
    removed = [line for line in a if line not in b]
    return {
        "similarity": round(ratio, 3),
        "changed": ratio < 0.999,
        "added": added,
        "removed": removed,
        "before": before,
        "after": after,
    }


def compare_materials(baseline: dict, adapted: dict, *,
                      plan: Optional[dict] = None,
                      noise: Optional[dict] = None) -> dict:
    """The per-topic, per-section comparison the viewer renders."""
    indexes = sorted(set(baseline) | set(adapted))
    topics = []
    for index in indexes:
        before_sheet = baseline.get(index) or {}
        after_sheet = adapted.get(index) or {}
        noise_sheet = (noise or {}).get(index) or {}

        sections = []
        for name in SECTION_ORDER:
            diff = _section_diff(section_text(before_sheet, name),
                                 section_text(after_sheet, name))
            # The same section across two BASELINE runs. This is the ruler: any
            # movement at or below it is the sampler, not the plan.
            if noise_sheet:
                diff["noiseSimilarity"] = _section_diff(
                    section_text(before_sheet, name),
                    section_text(noise_sheet, name))["similarity"]
                diff["aboveNoise"] = diff["similarity"] < diff["noiseSimilarity"]
            diff["section"] = name
            diff["label"] = SECTION_LABELS.get(name, name)
            sections.append(diff)

        mine = reinforcement.accepted_for(plan, index)
        moved = {s["section"] for s in sections if s["changed"]}
        # Moved by MORE than two baselines differ from each other, where a noise
        # arm was run. Without one, any movement counts — and `noiseArmRan` on
        # the result says which, so a reader is never left assuming a ruler that
        # was not there.
        moved_really = {s["section"] for s in sections
                        if s["changed"] and s.get("aboveNoise", True)}

        topics.append({
            "index": index,
            "adaptations": [{"factor": a.get("factor"), "type": a.get("type"),
                             "section": a.get("section"), "change": a.get("change"),
                             "reason": a.get("reason"), "purpose": a.get("purpose"),
                             "evidence": a.get("evidence") or [],
                             # DID IT LAND WHERE IT AIMED — the sharpest signal in
                             # the whole comparison, and the one the fuzzy adoption
                             # check cannot give. Adoption asks "do this
                             # adaptation's words appear anywhere on the sheet",
                             # which a paraphrase satisfies and a coincidence can
                             # too. This asks whether the SECTION it named is
                             # different from the section the baseline arm wrote —
                             # a byte comparison between two runs that differ by
                             # one input, so a `no` here is not a guess.
                             "landed": _landed(a.get("section"), moved_really, moved),
                             # The gate's ADVISORIES, carried through rather than
                             # dropped. An adaptation can land squarely in the
                             # section it named and still be worth a second look —
                             # `unechoed_evidence` fires exactly there, on a
                             # citation that reads like grounding and is not. Left
                             # out of this projection, the one flagged concern in a
                             # run is invisible in the only document anyone reads,
                             # which is the same as not having flagged it.
                             "warnings": list(a.get("warnings") or [])}
                            for a in mine],
            "sections": sections,
            "sectionsChanged": [s["section"] for s in sections if s["changed"]],
            # The sections an adaptation ASKED for, against the sections that
            # actually moved. The interesting cell in the whole app: an
            # adaptation aimed at Concept that moved only Explore did something,
            # but not the something it was asked to do.
            "sectionsTargeted": sorted({a.get("section") for a in mine if a.get("section")}),
        })

    adoption_findings, adoption_metrics = check_context_adoption(plan, adapted, None)
    changed_sections = sum(len(t["sectionsChanged"]) for t in topics)
    total_sections = len(topics) * len(SECTION_ORDER)

    return {
        "topics": topics,
        "handover": reinforcement.summary(plan),
        "summary": {
            "topics": len(topics),
            "sectionsCompared": total_sections,
            "sectionsChanged": changed_sections,
            "changeRate": round(changed_sections / total_sections, 3) if total_sections else None,
            "topicsUnchanged": sum(1 for t in topics if not t["sectionsChanged"]),
            # The headline finding, and the one worth being suspicious of: a plan
            # that was handed over and changed nothing is either a generator
            # ignoring it or a plan that asked for what was already there.
            "adaptationsHandedOver": reinforcement.summary(plan)["adaptations"],
            # The headline, counted from the section comparison rather than from
            # word overlap. `landedWhereAimed` is the number to read first: an
            # adaptation that asked for Level Set and did not move Level Set did
            # not do its job, however many of its words turn up elsewhere.
            "landedWhereAimed": sum(
                1 for t in topics for a in t["adaptations"] if a["landed"] == "yes"),
            "landedElsewhere": sum(
                1 for t in topics for a in t["adaptations"] if a["landed"] == "elsewhere"),
            "didNotLand": sum(
                1 for t in topics for a in t["adaptations"]
                if a["landed"] in ("no", "noise-only")),
            **adoption_metrics,
        },
        "adoptionFindings": adoption_findings,
        "noiseArmRan": bool(noise),
    }


async def run_ab(*, contract: dict, plan: dict, sources: Optional[dict] = None,
                 chapter_text: str = "", config: Optional[dict] = None,
                 with_noise_arm: bool = True) -> dict:
    """Both arms (and optionally a second baseline), then the comparison.

    The arms run CONCURRENTLY and share nothing but their inputs. Sequentially
    they would still be independent, but a chapter is minutes per arm and the
    whole point of this tool is to be run often enough to catch a regression.
    """
    jobs = [
        run_generation(contract=contract, context_plan=None, sources=sources,
                       chapter_text=chapter_text, config=config, label="baseline"),
        run_generation(contract=contract, context_plan=plan, sources=sources,
                       chapter_text=chapter_text, config=config, label="adapted"),
    ]
    if with_noise_arm:
        jobs.append(run_generation(contract=contract, context_plan=None,
                                   sources=sources, chapter_text=chapter_text,
                                   config=config, label="noise"))

    results = await gather_bounded(jobs, limit=3)
    arms, errors = {}, []
    for label, result in zip(("baseline", "adapted", "noise"), results):
        if isinstance(result, BaseException):
            errors.append(f"{label} arm: {result}")
            arms[label] = {}
        else:
            arms[label] = result.get("materials") or {}

    if not arms.get("baseline") or not arms.get("adapted"):
        return {"error": "an arm produced no material — nothing to compare",
                "errors": errors, "arms": {k: len(v) for k, v in arms.items()}}

    comparison = compare_materials(arms["baseline"], arms["adapted"],
                                   plan=plan, noise=arms.get("noise") or None)
    chapter = contract.get("chapter") or {}
    comparison["meta"] = {
        "grade": contract.get("grade"), "subject": contract.get("subject"),
        "chapter": chapter.get("title"), "chapterNumber": chapter.get("number"),
        "contractFingerprint": (contract.get("integrity") or {}).get("chapterFingerprint"),
        "errors": errors,
    }
    comparison["arms"] = arms
    return comparison
