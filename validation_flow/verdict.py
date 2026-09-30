"""Node 3's output: one document saying whether the material may ship, and why.

Node 1 ends in a contract, Node 2 in a plan, and this node ends in a verdict —
each node an artefact rather than a side effect on shared state. The reason is
the same all three times: a node whose output is "it wrote some keys into the
graph" cannot be replayed, logged, or handed to somebody a month later.

THE VERDICT IS PER SHEET AND PER CHAPTER, and both are needed. A chapter with
thirty-four clean sheets and six flagged ones is a usable chapter with a to-do
list, and the chapter-level word for that is `needs_review` — not `failed`, which
would hide the thirty-four.

    ship          every check passed, and the learner gate agrees it teaches
    needs_review  something failed; the sheet exists and a person should look
    refuse        the academic destination moved, or a refused context plan
                  reached the material. Not a to-do list — a sheet teaching
                  something nobody approved.

`refuse` IS SEPARATE FROM `needs_review` ON PURPOSE. Everything else this node
finds is a quality judgement about a sheet that is, at worst, a weaker version of
the right lesson. A moved mastery target is a different lesson. Collapsing the
two would put "the Refresher is thin" and "this teaches something else" in the
same bucket, and the bucket people learn to skim.

WHAT THE VERDICT DOES NOT DO. It does not repair, and it does not choose between
revisions. Both were part of the old in-package loop and both belong to whoever
owns generation: this node names the sections that need rewriting
(`repairTargets`) and stops. A validator that also rewrites is a validator
marking its own work.
"""
from datetime import datetime, timezone
from typing import Optional

try:
    from prep_flow.sections import SECTION_ORDER
except ImportError:  # pragma: no cover
    from ..prep_flow.sections import SECTION_ORDER

VERDICT_VERSION = "1.0"

SHIP, NEEDS_REVIEW, REFUSE = "ship", "needs_review", "refuse"

# The checks whose failure is a different lesson rather than a weaker one. See
# the module docstring — this list is the whole difference between the two
# severities of bad news, so it is named once and read everywhere.
_REFUSING_CHECKS = frozenset({"target_preservation", "plan_provenance"})


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _blocking(findings: list) -> list:
    return [f for f in findings if f.get("severity") == "blocking"]


def _topic_verdict(findings: list, learner: Optional[dict],
                   material: Optional[dict]) -> str:
    if material is None:
        return NEEDS_REVIEW
    if any(f.get("check") in _REFUSING_CHECKS and f.get("severity") == "blocking"
           for f in findings):
        return REFUSE
    if _blocking(findings):
        return NEEDS_REVIEW
    # A sheet that has not been judged by the learner gate is not a sheet that
    # passed it. `--no-simulate` is a real option and a legitimate one, and a
    # run that used it should say `needs_review` rather than `ship` — the
    # design's claim is that material should not be published until it has been
    # shown to teach, and `ship` is what publishing reads.
    if learner is None:
        return NEEDS_REVIEW
    return SHIP if learner.get("passed") else NEEDS_REVIEW


def build(*, contract: dict, materials: dict, issues: dict,
          batch_issues: list, integrity_findings: list,
          learner: Optional[dict], metrics: dict,
          repair_targets: list, repair_sections: dict,
          grounding_checked: bool, learner_ran: bool,
          plan: Optional[dict] = None, run_id: str = None) -> dict:
    """The whole verdict, built from what the three passes found."""
    rows = {int(r["index"]): r for r in (contract.get("topics") or [])
            if r.get("index") is not None}
    learner = learner or {}

    # Integrity findings are per topic but arrive as one list, because two of the
    # three are chapter-scale. Folded in here so a reader of one topic's row sees
    # everything about that topic in one place.
    per_topic_integrity: dict[int, list] = {}
    chapter_integrity: list = []
    for finding in integrity_findings:
        index = finding.get("index")
        if index is None:
            chapter_integrity.append(finding)
        else:
            per_topic_integrity.setdefault(int(index), []).append(finding)

    topics = []
    for index in sorted(rows):
        row = rows[index]
        material = materials.get(index)
        findings = list(issues.get(index) or []) + per_topic_integrity.get(index, [])
        judged = learner.get(index) if learner_ran else None
        verdict = _topic_verdict(findings, judged, material)

        topics.append({
            "index": index,
            "topic": row.get("topic"),
            "masteryTarget": (row.get("academic") or {}).get("masteryTarget"),
            "verdict": verdict,
            "hasMaterial": material is not None,
            "revision": int(((material or {}).get("_meta") or {}).get("revision", 0)),
            "blocking": len(_blocking(findings)),
            "advisory": len(findings) - len(_blocking(findings)),
            "findings": findings,
            "learner": ({"weighted": judged.get("weighted"),
                         "passed": judged.get("passed"),
                         "scores": judged.get("scores"),
                         "failedGates": judged.get("failedGates"),
                         "reason": judged.get("reason"),
                         "diagnosis": judged.get("diagnosis")}
                        if judged else None),
            # What a rewrite should touch, and nothing about how. Empty means
            # "the whole sheet", which is what a structural failure gets: a sheet
            # missing sections has no narrower surface to aim at.
            "repairSections": list(repair_sections.get(index) or []),
        })

    by_verdict: dict[str, int] = {}
    for row in topics:
        by_verdict[row["verdict"]] = by_verdict.get(row["verdict"], 0) + 1

    if any(r["verdict"] == REFUSE for r in topics) or any(
            f.get("severity") == "blocking" and f.get("check") in _REFUSING_CHECKS
            for f in chapter_integrity):
        chapter_verdict = REFUSE
    elif not topics or by_verdict.get(SHIP, 0) < len(topics):
        chapter_verdict = NEEDS_REVIEW
    else:
        chapter_verdict = SHIP

    return {
        "verdictVersion": VERDICT_VERSION,
        "producedBy": "node3.validation",
        "judgedAt": now_iso(),
        "runId": run_id,
        "grade": contract.get("grade"),
        "subject": contract.get("subject"),
        "chapter": contract.get("chapter") or {},
        "contractFingerprint": (contract.get("integrity") or {}).get("chapterFingerprint"),
        "verdict": chapter_verdict,
        "counts": {
            "topics": len(topics),
            "withMaterial": sum(1 for r in topics if r["hasMaterial"]),
            **{k: by_verdict.get(k, 0) for k in (SHIP, NEEDS_REVIEW, REFUSE)},
        },
        # WHAT WAS ACTUALLY ASKED. A run judged without the textbook and a run
        # judged with it produce the same shape, and only this says which — so a
        # `ship` reached without the grounding checks can never be mistaken for
        # one that survived them. Same for the learner gate, which is skippable
        # and expensive.
        "coverage": {
            "groundingChecked": grounding_checked,
            "learnerGateRan": learner_ran,
            "contextPlanChecked": plan is not None,
        },
        "chapterFindings": list(batch_issues or []) + chapter_integrity,
        "repairTargets": sorted(repair_targets or []),
        "metrics": dict(metrics or {}),
        "topics": topics,
        "sectionContract": list(SECTION_ORDER),
    }


def summary_line(document: dict) -> str:
    counts = document.get("counts") or {}
    coverage = document.get("coverage") or {}
    skipped = [name for name, ran in (
        ("grounding", coverage.get("groundingChecked")),
        ("learner gate", coverage.get("learnerGateRan"))) if not ran]
    return (f"[validation_flow] {document.get('verdict')} — "
            f"{counts.get('ship', 0)} ship, {counts.get('needs_review', 0)} need review, "
            f"{counts.get('refuse', 0)} refused, of {counts.get('topics', 0)} topic(s)"
            + (f" | NOT CHECKED: {', '.join(skipped)}" if skipped else ""))
