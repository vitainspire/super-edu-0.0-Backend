"""Which revision of a sheet actually ships.

Until this existed the answer was "the last one", unconditionally. That is the
right default when every rewrite is an improvement, and repair is not that: a
targeted rewrite is asked to fix a diagnosed failure while leaving five good
sections alone, and it can come back having fixed nothing and broken something.
When it does, the run stores it anyway, reports the chapter under that revision's
verdict, and the better sheet it replaced is gone.

The history that makes this answerable is already kept — `material_history` from
repair, `validation_history` from every validation pass, `learner_history` from
every simulation round. This joins them on the revision number and picks.

THE RULE, AND WHY IT IS NOT "HIGHEST SCORE".

    1. Structural soundness first. A revision with blocking findings loses to one
       without, whatever it scored. A sheet missing a section is not a better
       sheet that happens to be incomplete — the learner score of a sheet with
       holes was measured on the holes.
    2. Then the learner verdict, by weighted score.
    3. Then the LATEST revision. A tie means the rewrite changed nothing that is
       measured here, and the repair was attempted for a reason the measurements
       may not capture — so the newer sheet keeps the benefit of the doubt, and
       the previous behaviour is what a tie falls back to.

A revision nobody judged is not scored as zero. It is ranked on what IS known
about it, below any revision with a real verdict, because "we do not know" and
"we know it failed" are different and only one of them justifies discarding a
sheet. In practice this only arises when the run stopped between a repair and the
simulation that would have judged it.

WHAT THIS DOES NOT DO. It never promotes a failing sheet to `validated`. Choosing
the best of three bad attempts is still three bad attempts, and the chapter is
reported on the revision that shipped — see graph.persist_node, which recomputes
its status from the selection rather than from the last verdict. The spec's
phrase for it is the right one: pick the best candidate, and still show why it
failed.
"""
from typing import Optional


def _by_revision(entries: list[dict]) -> dict[int, dict]:
    """Latest entry wins per revision — a re-validation of unchanged material."""
    out: dict[int, dict] = {}
    for entry in entries or []:
        try:
            out[int(entry.get("revision", 0))] = entry
        except (TypeError, ValueError):
            continue
    return out


def candidates(index: int, *, material: dict, material_history: dict,
               validation_history: dict, learner_history: dict) -> list[dict]:
    """Every revision of one sheet that could ship, with what is known about it.

    The current material is always a candidate, and is added from `materials`
    rather than trusted to be in the history: a topic that was never repaired has
    no history at all, and one that was must still be judged against the sheet
    the run is actually holding.
    """
    revisions: dict[int, dict] = {}

    for entry in (material_history or {}).get(index) or []:
        body = entry.get("material")
        if not isinstance(body, dict):
            continue
        try:
            revisions[int(entry.get("revision", 0))] = body
        except (TypeError, ValueError):
            continue

    if isinstance(material, dict):
        current = int((material.get("_meta") or {}).get("revision", 0))
        revisions[current] = material

    validations = _by_revision((validation_history or {}).get(index))
    verdicts = _by_revision((learner_history or {}).get(index))

    out = []
    for revision in sorted(revisions):
        checked = validations.get(revision) or {}
        judged = verdicts.get(revision) or {}
        out.append({
            "revision": revision,
            "material": revisions[revision],
            "blocking": checked.get("blocking"),
            "advisory": checked.get("advisory"),
            "validated": bool(checked),
            "weighted": judged.get("weighted"),
            "passed": judged.get("passed"),
            "judged": bool(judged),
        })
    return out


def _rank(candidate: dict) -> tuple:
    """Sort key, best last. Every component is "higher is better"."""
    # Unvalidated is treated as "no blocking findings known", which is the same
    # position an unjudged revision gets on the learner axis: ranked on what is
    # known, never penalised for a measurement that did not run.
    blocking = candidate.get("blocking")
    clean = 1 if (blocking in (None, 0)) else 0
    return (
        clean,
        -(blocking or 0),
        1 if candidate.get("judged") else 0,
        candidate.get("weighted") if candidate.get("weighted") is not None else -1.0,
        # The teacher-readiness score used to sit here, below the learner's and
        # above the revision number, and it is gone with the gate that produced
        # it. Which means `revision` is now the tiebreaker between two revisions
        # that are structurally equal and scored the same by the learner — later
        # wins, because a repair was asked for and the newer sheet is the answer
        # to the question that asked for it.
        #
        # Two sheets that tie on the learner axis used to be separated by which
        # one prepared its teacher better, and that separation was real: a repair
        # that changed nothing a learner could see but gave the teacher a usable
        # explanation was KEPT because of this line. After the removal, that
        # repair looks identical to one that changed nothing at all.
        candidate["revision"],
    )


def select(index: int, *, material: dict, material_history: dict,
           validation_history: dict, learner_history: dict) -> Optional[dict]:
    """The revision that should ship, or None when there is nothing to choose.

    None means one candidate — the overwhelmingly common case, and worth
    returning explicitly so callers do not rewrite a material with itself and
    record a decision nobody made.
    """
    options = candidates(index, material=material, material_history=material_history,
                         validation_history=validation_history,
                         learner_history=learner_history)
    if len(options) < 2:
        return None

    ranked = sorted(options, key=_rank)
    best, current = ranked[-1], options[-1]
    return {
        "index": index,
        "revision": best["revision"],
        "material": best["material"],
        "changed": best["revision"] != current["revision"],
        "supersedes": current["revision"],
        "reason": _reason(best, current),
        "candidates": [{k: v for k, v in option.items() if k != "material"}
                       for option in options],
    }


def _reason(best: dict, current: dict) -> str:
    """Why this revision, in a sentence a teacher could read."""
    if best["revision"] == current["revision"]:
        return f"kept revision {best['revision']} — the latest is also the best"
    parts = []
    if (current.get("blocking") or 0) > (best.get("blocking") or 0):
        parts.append(f"revision {current['revision']} had "
                     f"{current['blocking']} blocking finding(s) against "
                     f"{best.get('blocking') or 0}")
    if best.get("weighted") is not None and current.get("weighted") is not None \
            and best["weighted"] > current["weighted"]:
        parts.append(f"the learner scored it {best['weighted']} against "
                     f"{current['weighted']}")
    if not parts:
        parts.append(f"revision {current['revision']} was not measurably better")
    return (f"reverted to revision {best['revision']} — " + "; ".join(parts))


def apply(topics: list, materials: dict, *, material_history: dict,
          validation_history: dict, learner_history: dict) -> tuple[dict, list[dict]]:
    """Run the selection across a chapter.

    Returns the materials that should ship and the decisions worth recording.
    Only sheets whose chosen revision differs from the current one appear in the
    returned materials — the caller merges, and rewriting forty unchanged sheets
    to change two would make the diff unreadable and the checkpoint larger.
    """
    chosen: dict[int, dict] = {}
    decisions: list[dict] = []
    for spec in topics or []:
        index = spec.get("index")
        material = (materials or {}).get(index)
        if material is None:
            continue
        decision = select(index, material=material,
                          material_history=material_history,
                          validation_history=validation_history,
                          learner_history=learner_history)
        if not decision:
            continue
        decisions.append({k: v for k, v in decision.items() if k != "material"})
        if decision["changed"]:
            body = dict(decision["material"])
            meta = dict(body.get("_meta") or {})
            # Stamped so the stored sheet says which revision it is and that the
            # choice was made rather than defaulted. `revision` itself is left
            # alone: it identifies the sheet, and rewriting it would make the
            # stored row disagree with the history that explains it.
            meta["selectedRevision"] = decision["revision"]
            meta["supersedes"] = decision["supersedes"]
            meta["selectionReason"] = decision["reason"]
            body["_meta"] = meta
            chosen[index] = body
    return chosen, decisions
