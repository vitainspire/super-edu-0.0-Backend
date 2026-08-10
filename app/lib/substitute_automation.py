"""Absence → recomputed coverage → published to everyone affected.

The single entry point for recording that a teacher is (or is no longer) away.
`admin_queries.mark_teacher_unavailable` still does the matching; this wraps
it with the two things that make the feature actually automatic:

  1. Diffing. mark_teacher_unavailable recomputes every affected period from
     scratch on each call, so its return value is "the current assignment",
     not "what changed". Re-running it — an admin correcting a reason, a
     second teacher going off sick and forcing a reshuffle — would re-announce
     covers that were already announced. Events are emitted from a diff
     against the previous state instead, so a teacher hears about their cover
     once, and hears again only if it genuinely moved to someone else.

  2. Enrichment. Handlers get class names, teacher names and start times
     resolved once here, rather than each of them querying for the same rows.

Everything downstream of the diff goes through the event bus (see events.py),
which is what lets all three absence entry points behave identically.
"""
from typing import Optional

from . import events
from .admin_queries import (
    fetch_substitutions_for_date, mark_teacher_unavailable, revert_teacher_availability,
)
from .notifications import (
    create_notification, create_admin_notification, resolve_admin_notifications_for_slot,
)

# The teacher-facing labels for teacher_availability.reason. Notifications say
# "on leave", not "on_leave".
REASON_LABEL = {
    "on_leave": "on leave",
    "late_arrival": "arriving late",
    "official_duty": "on official duty",
    "sick": "off sick",
    "other": "unavailable",
}


def _reason_text(reason: Optional[str]) -> str:
    return REASON_LABEL.get(reason or "other", "unavailable")


def _slot(s: dict) -> str:
    return f"{s['classId']}|{s['periodNumber']}"


def _period_label(payload: dict) -> str:
    """"Period 4 (9:20 AM) — 7B, Science" — as much as is known, no gaps."""
    bits = [f"Period {payload['periodNumber']}"]
    if payload.get("startTime"):
        bits.append(f"({_fmt_time(payload['startTime'])})")
    tail = payload.get("className") or "a class"
    if payload.get("subject"):
        tail += f", {payload['subject']}"
    return f"{' '.join(bits)} — {tail}"


def _fmt_time(t: str) -> str:
    try:
        h, m = (int(x) for x in t.split(":")[:2])
        return f"{h % 12 or 12}:{m:02d} {'PM' if h >= 12 else 'AM'}"
    except (ValueError, AttributeError):
        return t


# ── Orchestration ─────────────────────────────────────────────────────────────

def apply_teacher_absence(
    school_id: str, teacher_id: str, date: str, reason: str, source: str,
    note: Optional[str], ac, status: str = "approved",
) -> list[dict]:
    """Record the absence, recompute coverage, publish what changed.

    Returns the freshly computed substitution rows (same shape as
    fetch_substitutions_for_date) so callers can build a response from them.
    Callers do NOT need to notify anyone — that already happened by the time
    this returns.
    """
    before = {
        _slot(s): s for s in fetch_substitutions_for_date(school_id, date, ac)
        if s["originalTeacherId"] == teacher_id
    }

    after = mark_teacher_unavailable(school_id, teacher_id, date, reason, source, note, ac, status=status)

    events.emit(events.TEACHER_UNAVAILABLE, {
        "schoolId": school_id, "teacherId": teacher_id, "date": date,
        "reason": reason, "source": source, "note": note, "periodCount": len(after),
    }, ac)

    # Enriched over both states: a reassignment needs the outgoing substitute's
    # name as much as the incoming one's.
    enrich = _enricher(school_id, teacher_id, date, [*after, *before.values()], ac)

    changed_assigned, changed_unresolved = [], []

    for s in after:
        prior = before.get(_slot(s))
        prior_sub = prior.get("substituteTeacherId") if prior else None
        current_sub = s.get("substituteTeacherId")

        if current_sub == prior_sub and prior is not None:
            continue  # unchanged — already published, stay quiet

        # Reassignment: whoever was on it before is no longer covering and
        # needs telling, or they'll turn up to a class that isn't theirs.
        if prior_sub and prior_sub != current_sub:
            events.emit(events.SUBSTITUTION_CANCELLED, enrich({**prior, "reason": reason}), ac)

        if current_sub:
            payload = enrich({**s, "reason": reason})
            changed_assigned.append(payload)
            events.emit(events.SUBSTITUTION_ASSIGNED, payload, ac)
        else:
            payload = enrich({**s, "reason": reason})
            changed_unresolved.append(payload)
            events.emit(events.SUBSTITUTION_UNRESOLVED, payload, ac)

    if changed_assigned or changed_unresolved:
        events.emit(events.COVERAGE_PUBLISHED, {
            "schoolId": school_id, "teacherId": teacher_id, "date": date, "reason": reason,
            "assigned": changed_assigned, "unresolved": changed_unresolved,
        }, ac)

    return after


def clear_teacher_absence(school_id: str, teacher_id: str, date: str, ac) -> None:
    """Teacher is back on duty: drop the absence, drop their substitutions,
    and stand down anyone who'd been assigned to cover."""
    affected = [
        s for s in fetch_substitutions_for_date(school_id, date, ac)
        if s["originalTeacherId"] == teacher_id
    ]

    revert_teacher_availability(teacher_id, date, ac)

    events.emit(events.TEACHER_AVAILABLE, {
        "schoolId": school_id, "teacherId": teacher_id, "date": date,
    }, ac)

    enrich = _enricher(school_id, teacher_id, date, affected, ac)
    for s in affected:
        if s.get("substituteTeacherId"):
            events.emit(events.SUBSTITUTION_CANCELLED, enrich({**s, "returned": True}), ac)
        else:
            # An unresolved period raised an admin alert but never had a
            # substitute, so no cancellation covers it. The teacher being back
            # is exactly what fixes it — clear it directly.
            resolve_admin_notifications_for_slot(
                school_id, date, s["classId"], s["periodNumber"], ac, notif_type="period_uncovered",
            )


def _enricher(school_id: str, teacher_id: str, date: str, rows: list[dict], ac):
    """Resolve names and period times once, return a per-row decorator.

    Times come from the absent teacher's published timetable row for the day —
    a substitution row records which period, not when it runs.
    """
    from .timetable_resolution import day_of_week_for

    class_ids = list({r["classId"] for r in rows})
    substitute_ids = list({r["substituteTeacherId"] for r in rows if r.get("substituteTeacherId")})

    class_rows = (
        ac.table("classes").select("id, name, teacher_id").in_("id", class_ids).execute().data or []
    ) if class_ids else []
    teacher_rows = (
        ac.table("teachers").select("id, name").in_("id", [teacher_id, *substitute_ids]).execute().data or []
    )
    time_rows = (
        ac.table("timetable").select("class_id, period_number, start_time, end_time")
        .eq("teacher_id", teacher_id).eq("day_of_week", day_of_week_for(date)).execute().data or []
    )

    class_name = {r["id"]: r["name"] for r in class_rows}
    class_teacher = {r["id"]: r.get("teacher_id") for r in class_rows}
    teacher_name = {r["id"]: r["name"] for r in teacher_rows}
    times = {f"{r['class_id']}|{r['period_number']}": r for r in time_rows}

    def enrich(s: dict) -> dict:
        t = times.get(_slot(s), {})
        return {
            **s,
            "schoolId": school_id,
            "className": class_name.get(s["classId"]),
            "classTeacherId": class_teacher.get(s["classId"]),
            "originalTeacherName": teacher_name.get(s["originalTeacherId"], "a colleague"),
            "substituteTeacherName": teacher_name.get(s.get("substituteTeacherId")),
            "startTime": t.get("start_time"),
            "endTime": t.get("end_time"),
        }

    return enrich


# ── Handlers ──────────────────────────────────────────────────────────────────
# Registered at import time, which is safe because they live in the same module
# as apply_teacher_absence: anything able to record an absence has necessarily
# imported this file and therefore run the registrations. Move them to their own
# module and that guarantee is gone — the bus would dispatch to nothing and
# absences would go out silently again.

@events.on(events.SUBSTITUTION_ASSIGNED)
def _notify_substitute(payload: dict, ac) -> None:
    """The autopublish: the covering teacher learns of the period the moment
    it's assigned, and it's already on their timetable when they look."""
    # Always empty for an automatic assignment, which is subject-matched by
    # construction. Kept for the admin's manual override, where the substitute
    # may be outside their subjects and gets no class materials.
    access_note = (
        "" if payload.get("status") == "assigned"
        else " (cover only — class materials stay with the regular teacher)"
    )
    create_notification(
        payload["substituteTeacherId"], "substitute_assigned",
        f"You're covering {_period_label(payload)} on {payload['date']} for "
        f"{payload['originalTeacherName']}, who is {_reason_text(payload.get('reason'))}"
        f".{access_note}",
        ac, class_id=payload["classId"], date=payload["date"],
    )


@events.on(events.COVERAGE_PUBLISHED)
def _notify_absent_teacher(payload: dict, ac) -> None:
    """The absent teacher gets told who has their classes — they're the one
    fielding "what did you cover?" on their return.

    One digest per date rather than one message per period, which is what the
    COVERAGE_PUBLISHED event exists for: a five-day leave would otherwise fill
    their notification bell with near-identical rows.
    """
    assigned, unresolved = payload["assigned"], payload["unresolved"]
    if not assigned and not unresolved:
        return

    lines = [
        f"{a['substituteTeacherName'] or 'A colleague'} — {_period_label(a)}"
        for a in assigned
    ] + [
        f"NOT COVERED — {_period_label(u)}" for u in unresolved
    ]
    headline = (
        f"Cover for {payload['date']}: {len(assigned)} of {len(assigned) + len(unresolved)} periods arranged."
    )
    create_notification(
        payload["teacherId"], "coverage_arranged",
        f"{headline} " + "; ".join(lines) + ".",
        ac, date=payload["date"],
    )


@events.on(events.SUBSTITUTION_UNRESOLVED)
def _alert_admins_uncovered(payload: dict, ac) -> None:
    """The school's own inbox. Only an admin can actually fix an uncovered
    period — they're the ones who can reassign across the whole staff, override
    a workload cap or move the class — so this is the alert that matters.

    School-scoped, not per-admin: whichever admin deals with it clears it for
    everyone (see migration 0006).
    """
    create_admin_notification(
        payload["schoolId"], "period_uncovered",
        f"No substitute available for {_period_label(payload)} on {payload['date']} — "
        f"{payload['originalTeacherName']} is {_reason_text(payload.get('reason'))}.",
        ac, class_id=payload["classId"], date=payload["date"], period_number=payload["periodNumber"],
    )


@events.on(events.SUBSTITUTION_ASSIGNED)
def _clear_stale_uncovered_alert(payload: dict, ac) -> None:
    """A period that was uncovered and now has someone is no longer a problem.

    Happens routinely: a teacher returns and frees up, or an admin reassigns by
    hand. Without this the inbox keeps alerts for problems that already fixed
    themselves, and admins learn to ignore the bell.
    """
    resolve_admin_notifications_for_slot(
        payload["schoolId"], payload["date"], payload["classId"], payload["periodNumber"], ac,
        notif_type="period_uncovered",
    )


@events.on(events.SUBSTITUTION_UNRESOLVED)
def _notify_uncovered(payload: dict, ac) -> None:
    """Nobody was free. The class teacher is told, because an uncovered
    period is a class left unattended and the admin dashboard is a pull
    surface nobody watches at 7am.

    Silent when the class teacher IS the absent teacher — they already know
    they're away, and their own coveredBy view shows the gap.
    """
    class_teacher_id = payload.get("classTeacherId")
    if not class_teacher_id or class_teacher_id == payload["originalTeacherId"]:
        return
    create_notification(
        class_teacher_id, "class_uncovered",
        f"No substitute could be found for {_period_label(payload)} on {payload['date']} "
        f"({payload['originalTeacherName']} is {_reason_text(payload.get('reason'))}). "
        f"Your class needs cover arranging.",
        ac, class_id=payload["classId"], date=payload["date"],
    )


@events.on(events.SUBSTITUTION_CANCELLED)
def _notify_cover_cancelled(payload: dict, ac) -> None:
    """Stand-down. Without this, a teacher told on Monday that they're
    covering Tuesday period 4 has no way to learn it was called off."""
    substitute_id = payload.get("substituteTeacherId")
    if not substitute_id:
        return
    why = (
        f"{payload['originalTeacherName']} is back in"
        if payload.get("returned") else "it's been reassigned"
    )
    create_notification(
        substitute_id, "cover_cancelled",
        f"You're no longer covering {_period_label(payload)} on {payload['date']} — {why}.",
        ac, class_id=payload["classId"], date=payload["date"],
    )
