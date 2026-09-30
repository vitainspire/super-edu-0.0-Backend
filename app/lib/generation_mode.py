"""Per-(grade, subject) Prep Material generation mode.

"opt_in": the cheap shared-batch default (today's only behavior) — one lesson
per topic, shared across every section, with a teacher able to personalize a
single topic on demand ("make this mine").

"full_personalization": every teacher assigned to this grade+subject always
gets their own freshly generated lesson, built from their own teaching
profile — no shared pool at all.

Which one applies is suggested automatically by comparing how similar the
assigned teachers' personalization profiles actually are (see
compute_similarity below), but an admin's own explicit choice always wins —
recompute_grade_subject_mode refuses to touch a row once mode_set_by='admin'.
"""
import itertools
from typing import Optional

# The 9 fixed categories on TeachingProfilePersonalization (lib/types.ts).
# Deliberately just these — the only part of the profile that's a clean,
# comparable fingerprint of teaching STYLE, as opposed to classroom logistics
# (language, resources) which don't belong in a "how differently do they
# teach" comparison.
PERSONALIZATION_KEYS = [
    "stories", "games", "handsOn", "criticalThinking", "creativity",
    "realLife", "localCulture", "reflection", "exploration",
]

SIMILARITY_THRESHOLD = 0.60


def _pair_similarity(a: dict, b: dict) -> float:
    matches = sum(1 for k in PERSONALIZATION_KEYS if a.get(k) is not None and a.get(k) == b.get(k))
    return matches / len(PERSONALIZATION_KEYS)


def compute_similarity(personalizations: list[dict]) -> float:
    """Average pairwise agreement across every pair of teachers, not a strict
    unanimous-agreement requirement — with 3+ teachers, unanimity gets harder
    to hit just by adding more people even if they're all broadly alike, so a
    pairwise average stays fair regardless of how many are involved.

    0 or 1 real profile has nothing to disagree with — trivially 1.0 (100%),
    which is also the practically correct answer: with only one teacher,
    full personalization and shared generation produce identical coverage,
    so there is no reason to pay the extra cost.
    """
    valid = [p for p in personalizations if p]
    if len(valid) <= 1:
        return 1.0
    pairs = list(itertools.combinations(valid, 2))
    return sum(_pair_similarity(a, b) for a, b in pairs) / len(pairs)


def _teachers_of_grade_subject(ac, school_id: str, grade: str, subject: str) -> list[str]:
    """The real, current set of teachers teaching this grade+subject — read
    from the published timetable (class_id + label=subject), the same source
    _find_class_teacher already trusts, rather than the coarser (and
    subject-blind) teacher_class_assignments."""
    class_ids = [
        c["id"] for c in
        (ac.table("classes").select("id").eq("school_id", school_id).eq("grade", grade).execute().data or [])
    ]
    if not class_ids:
        return []
    periods = (
        ac.table("school_timetable_periods").select("teacher_id")
        .in_("class_id", class_ids).eq("label", subject).execute().data or []
    )
    return list({p["teacher_id"] for p in periods if p.get("teacher_id")})


def recompute_grade_subject_mode(ac, school_id: str, grade: str, subject: str) -> Optional[str]:
    """Re-derive the suggested mode from the CURRENT set of assigned teachers'
    profiles, and persist it — unless an admin already made an explicit choice
    for this grade+subject, in which case this only reads, never writes.

    Returns the mode now in effect (whatever ends up in the row), or None if
    there is nothing to compute against yet (no classes for this grade).
    """
    teacher_ids = _teachers_of_grade_subject(ac, school_id, grade, subject)
    if not teacher_ids:
        return None

    rows = ac.table("teachers").select("id, teaching_profile").in_("id", teacher_ids).execute().data or []
    personalizations = [
        (r.get("teaching_profile") or {}).get("personalization") or {}
        for r in rows
    ]
    similarity = compute_similarity(personalizations)
    suggested = "opt_in" if similarity >= SIMILARITY_THRESHOLD else "full_personalization"

    existing = (
        ac.table("grade_subject_feedback_profiles").select("generation_mode, mode_set_by")
        .eq("school_id", school_id).eq("grade", grade).eq("subject", subject).maybe_single().execute()
    )
    existing_data = existing.data if existing else None

    if existing_data and existing_data.get("mode_set_by") == "admin":
        # An admin's explicit choice is authoritative — recomputation is
        # advisory only from here on, never a silent override.
        return existing_data.get("generation_mode") or "opt_in"

    ac.table("grade_subject_feedback_profiles").upsert({
        "school_id": school_id, "grade": grade, "subject": subject,
        "generation_mode": suggested, "mode_set_by": "auto",
        "mode_updated_at": _now_iso(),
    }, on_conflict="school_id,grade,subject").execute()
    return suggested


def get_grade_subject_mode(ac, school_id: str, grade: str, subject: str) -> str:
    """Fast read path for the generation pipeline — never recomputes, just
    reads whatever is currently in effect. Defaults to 'opt_in' (the cheap,
    safe default) when nothing has been computed yet at all."""
    row = (
        ac.table("grade_subject_feedback_profiles").select("generation_mode")
        .eq("school_id", school_id).eq("grade", grade).eq("subject", subject).maybe_single().execute()
    )
    data = row.data if row else None
    return (data or {}).get("generation_mode") or "opt_in"


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()
