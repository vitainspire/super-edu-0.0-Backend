"""What a teacher is handed when the pipeline has stopped trying.

READS STORED ROWS, NOT LIVE STATE, which is what lets it outlive the stages it
describes. Generation, validation and the learner gate left this package with
the node split (`_deferred/README.md`); every run made before that is still in
the database with its attempts, findings and verdicts intact, and this module
still assembles them. A Node 1 run has no material and produces no packet —
`needs_review` there means a contract with topics missing fields, which
`prep_flow/contract.py::readiness` states directly.

`needs_review` was a status string. Everything a person would need to act on it
existed — the findings, the learner verdicts, the teacher-readiness verdicts, the
selection decision, and now the attempts themselves — and none of it was ever
assembled into one thing anyone could read. This assembles it.

The packet answers, per topic and for the chapter:

    what was asked for      the chapter, the topics, the run's own trace
    what was produced       every attempt, its scores, which one shipped
    why it is here          the gates it failed and the diagnoses behind them
    what keeps happening    the failures repair could not fix, named
    what was decided        the review trail, append-only

TWO THINGS IT DELIBERATELY DOES NOT DO.

**It does not publish the best attempt.** Selection has already chosen which
revision ships, and it chose the best of what exists — but "best of three failing
attempts" is still failing, and a chapter arrives here precisely because the
automatic loop could not fix it. The packet shows the winner and the reason it
still fails, and a person decides. The spec's phrasing is the right one: do not
automatically publish it merely because it has the highest score.

**It does not rank topics by score.** A chapter with two bad sheets out of forty
is a usable chapter with a to-do list, and the to-do list is what a teacher wants
first — so the failing topics come first and everything else follows in teaching
order. Ordering by score would bury the ones that are fine among the ones that
are not, which is exactly backwards for someone who has to teach all of them.

RECURRING PROBLEMS ARE COUNTED, NOT JUDGED. A failure that survived every attempt
is the most useful line in this packet, and it is arithmetic: the same dimension
failing in two or more attempts of the same topic. No model is asked to
characterise it, because the diagnoses are already in the packet for a human to
read, and a summary of them written by the same family of model that wrote them
adds a layer of confident paraphrase between the teacher and the evidence.
"""
from typing import Optional

# The learner gate's four dimensions, kept here as names and labels only — the
# same treatment `TEACHER_DIMENSIONS` below already gets, and for the same
# reason twice over.
#
# The gate itself left this package when the master orchestration was split into
# nodes: judging whether a sheet teaches is a question about generated material,
# and Node 1 generates none. `validation_flow/learner.py` is the agent, with the
# weights, gates and prompts that produced these scores; Node 3 runs it.
#
# What this module reads is STORED ROWS, and every run made before the split
# carries real learner verdicts under these four names in
# `prep_flow_learner_diagnoses` and in the `validation` column. A review of one
# of those runs should still be able to say which dimension kept failing, so the
# map outlives the agent — importing it back from `_deferred/` to get four
# labels would make this module depend on a package that is deliberately not on
# the import path.
#
# ORDER MATTERS and is the gate's own: it is the order a packet lists a topic's
# failures in, and `recurring()` counts against these keys.
LEARNER_DIMENSIONS = {
    "understanding": {"label": "Core understanding"},
    "application": {"label": "Application"},
    "misconception": {"label": "Misconception resistance"},
    "forward": {"label": "Forward readiness"},
}

# The teacher-readiness gate's five facets, kept here as names and labels only.
#
# The gate itself is gone — it cost a model call per sheet and was removed — but
# this module reads STORED ROWS, and every run recorded before the removal still
# carries real readiness verdicts in `prep_flow_material_attempts` and in the
# `validation` column. A review of one of those runs should still be able to say
# which facet kept failing, so the map outlives the agent that produced it.
#
# Nothing writes these any more, so on a run made after the removal every
# readiness field is absent and `recurring()` simply finds no teacher history to
# count — which is the correct answer rather than a gap.
TEACHER_DIMENSIONS = {
    "prerequisites": {"label": "Prerequisites"},
    "explanation": {"label": "Explanation"},
    "misconception": {"label": "Misconception readiness"},
    "checking": {"label": "Checking understanding"},
    "struggle": {"label": "Struggle support"},
}


def _failed_dimensions(entry: dict, dimensions: dict, floor: float = 0.7) -> set:
    """Which dimensions of one judged attempt were under the floor."""
    scores = (entry or {}).get("scores") or {}
    return {name for name in dimensions if scores.get(name, 1.0) < floor}


def recurring(learner_history: list, readiness_history: list) -> list[dict]:
    """Failures that survived a rewrite, which is what makes them worth naming.

    Counted across the attempts of ONE topic. A dimension that failed once and
    was fixed is the loop working; the same dimension failing in two or three
    consecutive attempts means the repair prompt cannot reach it, and that is a
    different problem with a different owner — it is about the generator, not
    about this chapter.
    """
    out = []
    for source, history, dimensions in (
            ("learner", learner_history or [], LEARNER_DIMENSIONS),
            ("teacher", readiness_history or [], TEACHER_DIMENSIONS)):
        if len(history) < 2:
            # One attempt cannot establish that anything recurs. Said here rather
            # than filtered silently, because "nothing recurred" and "there was
            # only ever one attempt" are different answers.
            continue
        counts: dict[str, int] = {}
        reasons: dict[str, list] = {}
        for entry in history:
            for name in _failed_dimensions(entry, dimensions):
                counts[name] = counts.get(name, 0) + 1
                for diagnosis in entry.get("diagnosis") or []:
                    key = diagnosis.get("dimension") or diagnosis.get("facet")
                    if key == name and diagnosis.get("rootCause"):
                        reasons.setdefault(name, []).append(diagnosis["rootCause"])
        for name, count in sorted(counts.items(), key=lambda kv: -kv[1]):
            if count < 2:
                continue
            out.append({
                "source": source,
                "dimension": name,
                "label": dimensions[name]["label"],
                "attemptsFailed": count,
                "ofAttempts": len(history),
                "survivedEveryAttempt": count == len(history),
                # Every stated cause, not a summary of them. They are usually
                # the same gap described twice, and reading both is how you tell
                # a real pattern from a model repeating itself.
                "statedCauses": reasons.get(name, [])[:3],
            })
    return out


def _attempt_view(row: dict) -> dict:
    """One attempt, without its material body — the packet lists, then fetches."""
    return {
        "revision": row.get("revision"),
        "shipped": bool(row.get("shipped")),
        "blocking": row.get("blocking"),
        "advisory": row.get("advisory"),
        "learner": {"weighted": row.get("learner_weighted"),
                    "passed": row.get("learner_passed")},
        "teacher": {"weighted": row.get("readiness_weighted"),
                    "passed": row.get("readiness_passed")},
    }


def build(run: dict, materials: list[dict], attempts: list[dict],
          reviews: list[dict], *, attempts_available: bool = True) -> dict:
    """Assemble the packet from stored rows alone.

    From the database rather than from graph state, because a review happens
    after the run — possibly days later, certainly in another process — and a
    packet that could only be built while the run was still in memory would be a
    packet nobody could ever open.
    """
    by_topic: dict[int, list[dict]] = {}
    for row in attempts or []:
        by_topic.setdefault(row.get("idx"), []).append(row)

    topics, needing_attention = [], []
    for material in materials or []:
        index = material.get("idx")
        validation = material.get("validation") or {}
        findings = validation.get("findings") or []
        learner = validation.get("learner") or {}
        teacher = validation.get("readiness") or {}
        blocking = [f for f in findings if f.get("severity") == "blocking"]

        stored = sorted(by_topic.get(index) or [],
                        key=lambda r: r.get("revision") or 0)
        meta = (material.get("material") or {}).get("_meta") or {}

        entry = {
            "index": index,
            "topic": (material.get("prep_flow_topics") or {}).get("topic")
                     if isinstance(material.get("prep_flow_topics"), dict) else None,
            "shippedRevision": material.get("revision"),
            # Present only when selection had a choice to make and made one.
            "selection": {
                "revision": meta.get("selectedRevision"),
                "supersedes": meta.get("supersedes"),
                "reason": meta.get("selectionReason"),
            } if meta.get("selectionReason") else None,
            "attempts": [_attempt_view(row) for row in stored],
            "attemptCount": len(stored) or 1,
            "blockingFindings": blocking,
            "advisoryFindings": [f for f in findings
                                 if f.get("severity") != "blocking"],
            "learner": {
                "passed": learner.get("passed"),
                "weighted": learner.get("weighted"),
                "reason": learner.get("reason"),
                "failedGates": learner.get("failedGates") or [],
                "diagnosis": learner.get("diagnosis") or [],
                "questions": learner.get("questions") or {},
            } if learner else None,
            "teacher": {
                "passed": teacher.get("passed"),
                "weighted": teacher.get("weighted"),
                "reason": teacher.get("reason"),
                "failedGates": teacher.get("failedGates") or [],
                "diagnosis": teacher.get("diagnosis") or [],
            } if teacher else None,
            "recurring": recurring(validation.get("learnerHistory"),
                                   validation.get("readinessHistory")),
        }
        entry["needsAttention"] = bool(
            blocking
            or (learner and learner.get("passed") is False)
            or (teacher and teacher.get("passed") is False))
        (needing_attention if entry["needsAttention"] else topics).append(entry)

    # Failing topics first, then the rest in teaching order — a chapter with two
    # bad sheets out of forty is a usable chapter with a to-do list, and the
    # to-do list is the part a teacher needs before anything else.
    needing_attention.sort(key=lambda t: t["index"] or 0)
    topics.sort(key=lambda t: t["index"] or 0)

    decided = [r for r in (reviews or []) if r.get("action") in ("approve", "reject")]
    return {
        "runId": run.get("id"),
        "status": run.get("status"),
        "chapter": {"number": run.get("chapter_number"),
                    "title": run.get("chapter_title")},
        "grade": run.get("grade"), "subject": run.get("subject"),
        "arc": run.get("chapter_arc"),
        "summary": {
            "topics": len(needing_attention) + len(topics),
            "needingAttention": len(needing_attention),
            "clean": len(topics),
            "topicsWithMultipleAttempts": sum(
                1 for t in needing_attention + topics if t["attemptCount"] > 1),
            "recurringProblems": sum(
                len(t["recurring"]) for t in needing_attention + topics),
            # Said explicitly rather than inferred from an empty list: without
            # migration 034 there are no attempt rows, and "this chapter never
            # needed a second attempt" would be the wrong thing to conclude.
            "attemptsRecorded": attempts_available,
        },
        "metrics": run.get("metrics") or {},
        "provenance": run.get("provenance") or {},
        "needsAttention": needing_attention,
        "topics": topics,
        "reviews": reviews or [],
        "decided": decided[0] if decided else None,
        "error": run.get("error"),
        "createdAt": run.get("created_at"),
        "completedAt": run.get("completed_at"),
    }


# Which run status each decision produces. In code and not in the route, because
# it is the one part of a review that changes what the rest of the system reads:
# `validated` is what publishing looks at, so approving a chapter and marking a
# chapter publishable have to be the same act or they will drift apart.
STATUS_FOR_ACTION = {
    "approve": "approved",
    "reject": "rejected",
    # A revision request leaves the run where it is. The chapter still needs
    # review; what has changed is that someone has said what they want, and the
    # note carries that to whoever picks it up.
    "revise": None,
    "comment": None,
}


def status_after(action: str, current: Optional[str]) -> Optional[str]:
    """The run's new status, or None to leave it alone."""
    target = STATUS_FOR_ACTION.get(action)
    return target if target and target != current else None
