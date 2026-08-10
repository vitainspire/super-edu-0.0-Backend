"""Mirrors lib/substituteFinder.ts.

find_substitute finds the best-fit substitute for one period. By default only
considers teachers who actually teach the period's subject (derived elsewhere
from their real assignments/timetable, since Teacher.subject/grade aren't
reliable). Hard-excludes anyone who'd breach their own daily/weekly workload
cap by taking this period. Ranks survivors by fewest periods already on that
weekday, then name, for a deterministic pick.

require_subject_match=False drops the subject requirement. Nothing in the
absence automation passes it any more — mark_teacher_unavailable used to, as a
last-resort fallback, and that is exactly the behaviour that was removed:
covering a class with a teacher who can't teach the subject booked a
supervision slot that read as solved everywhere. The parameter stays because
it's part of the shared parity spec this file is pinned to (see
shared/parity/substitute-finder.json and lib/substituteFinder.ts).

A SubstituteCandidate dict has: teacherId, name, subjectsTaught (set),
busySlots (set of "day|period" strings), maxPeriodsPerDay (optional),
maxPeriodsPerWeek (optional), weeklyLoad (int)."""


def find_substitute(
    need: dict, candidates: list[dict], exclude_teacher_ids: set, already_used_this_slot: set,
    require_subject_match: bool = True,
) -> str | None:
    slot_key = f"{need['dayOfWeek']}|{need['periodNumber']}"
    day_prefix = f"{need['dayOfWeek']}|"

    def periods_on_day(c: dict) -> int:
        return sum(1 for k in c["busySlots"] if k.startswith(day_prefix))

    pool = [
        c for c in candidates
        if c["teacherId"] not in exclude_teacher_ids
        and c["teacherId"] not in already_used_this_slot
        and slot_key not in c["busySlots"]
        and (not require_subject_match or need["subject"] in c["subjectsTaught"])
        and (c.get("maxPeriodsPerDay") is None or periods_on_day(c) < c["maxPeriodsPerDay"])
        and (c.get("maxPeriodsPerWeek") is None or c["weeklyLoad"] < c["maxPeriodsPerWeek"])
    ]
    if not pool:
        return None

    pool.sort(key=lambda c: (periods_on_day(c), c["name"]))
    return pool[0]["teacherId"]


def suggest_swap(
    need: dict,
    class_periods_that_day: list[dict],  # {periodNumber, subject, teacherId}
    candidates: list[dict],
    exclude_teacher_ids: set,
    already_used_this_slot,  # Callable[[int], set]
) -> dict | None:
    """When no substitute is free at the exact period, checks whether swapping
    this class's schedule for the day would resolve it: is there another
    period Q (same class, same day) where (a) a qualified substitute for the
    needed subject is free, and (b) Q's regular teacher is free at the
    original period P? If so, the class could do Q's subject at P (taught by
    Q's regular teacher) and the needed subject at Q (taught by the found
    substitute) — nobody's own schedule is disrupted, just this class's order
    for the day. Suggestion only — never auto-applied."""
    candidate_by_id = {c["teacherId"]: c for c in candidates}
    need_slot_key = f"{need['dayOfWeek']}|{need['periodNumber']}"

    for period in class_periods_that_day:
        if period["periodNumber"] == need["periodNumber"]:
            continue

        freeing_teacher_id = find_substitute(
            {"dayOfWeek": need["dayOfWeek"], "periodNumber": period["periodNumber"], "subject": need["subject"]},
            candidates, exclude_teacher_ids, already_used_this_slot(period["periodNumber"]),
        )
        if not freeing_teacher_id:
            continue

        moving_teacher = candidate_by_id.get(period["teacherId"])
        if not moving_teacher or need_slot_key in moving_teacher["busySlots"]:
            continue

        return {
            "swapPeriodNumber": period["periodNumber"], "swapSubject": period["subject"],
            "movingTeacherId": period["teacherId"], "freeingTeacherId": freeing_teacher_id,
        }
    return None
