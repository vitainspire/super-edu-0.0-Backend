"""The effective timetable for one teacher on one date.

`timetable` holds the recurring weekly plan; `timetable_substitutions` holds
the per-date deviations from it. Neither is the schedule a teacher should
actually be shown on a given morning — that's the two of them merged, and
this module is the only place that merge happens.

Merging produces three kinds of entry, and every consumer needs to tell them
apart:

  regular       a normal period, nothing has changed
  covered_away  the teacher's own period, but they're absent and someone else
                is taking it — it stays visible (they should see what happened
                to their class) but is not a period they turn up for
  covering      somebody else's period assigned to this teacher today; not in
                their weekly plan at all, and only exists for this one date

`covering` entries are synthesised, so they need class name, subject and
times pulled from the ORIGINAL teacher's published row — the substitution row
itself stores only class/period/subject. That lookup is why this is a query
module and not a pure function.

Access carried on covering entries mirrors school_data()'s existing rule and
must stay in step with it: only a subject-matched assignment ("assigned")
grants full class access. That is every cover the automation produces — it
leaves a period unresolved rather than assigning outside a teacher's subjects.
The other two statuses still turn up and are informational: "manual", from an
admin hand-picking a substitute, and legacy "assigned_fallback" rows. The
teacher sees the period so they know to show up, but no students, syllabus or
prep material sit behind it.
"""
from datetime import datetime, timedelta

# Substitution statuses that put a teacher in front of a class. "unresolved"
# is excluded: there's no substitute on the row to build an entry for.
_COVERING_STATUSES = ("assigned", "assigned_fallback", "manual")

# Only a subject match unlocks the class behind the period. Kept as its own
# constant so the rule reads the same here and in school_data().
_FULL_ACCESS_STATUS = "assigned"

# How far ahead the weekly overlay looks. Seven covers every weekday exactly
# once, which is what a weekly grid needs — and it matches how far the home
# page's schedule card will scan forward for the next day with periods.
_WEEK_AHEAD_DAYS = 7


def day_of_week_for(date: str) -> int:
    """0=Sun..6=Sat, matching JS getDay() — the convention `timetable`,
    `timetable_substitutions` and the frontend day tabs all already use."""
    return datetime.strptime(date, "%Y-%m-%d").isoweekday() % 7


def upcoming_dates_by_weekday(from_date: str, days: int = _WEEK_AHEAD_DAYS) -> dict[int, str]:
    """Each weekday mapped to its next occurrence, counting from_date as day 0.

    A weekly timetable has no dates on it, but substitutions do, so overlaying
    one onto the other needs a rule for which calendar Tuesday "Tuesday" means.
    The rule is the soonest one — matching what a teacher reads a weekly grid
    to mean, and matching how the home page scans forward for the next day
    that has periods.
    """
    start = datetime.strptime(from_date, "%Y-%m-%d")
    return {
        (start + timedelta(days=offset)).isoweekday() % 7:
            (start + timedelta(days=offset)).strftime("%Y-%m-%d")
        for offset in range(days)
    }


def _lookup_tables(teacher_id: str, covering_rows: list[dict], covered_away_rows: list[dict],
                   own_class_ids: list[str], ac) -> tuple[dict, dict, dict]:
    """Class names, teacher names and the absent teachers' period times.

    Batched into one query each: a covering entry needs all three, and doing
    them per-row would be an N+1 on the busiest read in the teacher portal.
    """
    original_teacher_ids = list({r["original_teacher_id"] for r in covering_rows})

    class_ids = list({*own_class_ids, *(r["class_id"] for r in covering_rows)})
    other_teacher_ids = list({
        *original_teacher_ids,
        *(r["substitute_teacher_id"] for r in covered_away_rows if r.get("substitute_teacher_id")),
    })

    class_name = {
        r["id"]: r["name"]
        for r in (ac.table("classes").select("id, name").in_("id", class_ids).execute().data or [] if class_ids else [])
    }
    teacher_name = {
        r["id"]: r["name"]
        for r in (ac.table("teachers").select("id, name").in_("id", other_teacher_ids).execute().data or [] if other_teacher_ids else [])
    }
    # Keyed teacher|class|day|period: the same class can sit at different times
    # on different days, so the day has to be part of the key.
    original_rows = (
        ac.table("timetable").select("teacher_id, class_id, day_of_week, period_number, start_time, end_time, label")
        .in_("teacher_id", original_teacher_ids).execute().data or []
    ) if original_teacher_ids else []
    times = {
        f"{r['teacher_id']}|{r['class_id']}|{r['day_of_week']}|{r['period_number']}": r
        for r in original_rows
    }
    return class_name, teacher_name, times


def _covering_entry(r: dict, date: str, day_of_week: int, class_name: dict, teacher_name: dict, times: dict) -> dict:
    original = times.get(
        f"{r['original_teacher_id']}|{r['class_id']}|{day_of_week}|{r['period_number']}"
    ) or {}
    return {
        # Synthetic and deterministic — this period has no row of its own in
        # `timetable`, and React keys need to be stable across refetches.
        "id": f"sub-{r['id']}",
        "classId": r["class_id"],
        "className": class_name.get(r["class_id"], "Class"),
        "dayOfWeek": day_of_week,
        "date": date,
        "periodNumber": r["period_number"],
        # Empty string, not null, when the absent teacher's row has been
        # deleted since the cover was assigned: every consumer types these as
        # strings, and a rare missing time shouldn't widen that to nullable
        # everywhere. Renders as "—".
        "startTime": original.get("start_time") or "",
        "endTime": original.get("end_time") or "",
        "label": r.get("subject") or original.get("label"),
        "coverage": "covering",
        "originalTeacherName": teacher_name.get(r["original_teacher_id"], "a colleague"),
        "substitutionStatus": r["status"],
        "fullAccess": r["status"] == _FULL_ACCESS_STATUS,
    }


def resolve_day_timetable(teacher_id: str, date: str, ac) -> dict:
    """This teacher's real schedule for `date`, base plan ⊕ substitutions.

    Returns {date, dayOfWeek, onLeave, reason, entries}, entries sorted by
    period number with a `coverage` field on each.
    """
    day_of_week = day_of_week_for(date)

    own_rows = (
        ac.table("timetable").select("*")
        .eq("teacher_id", teacher_id).eq("day_of_week", day_of_week).execute().data or []
    )
    covering_rows = (
        ac.table("timetable_substitutions").select("*")
        .eq("substitute_teacher_id", teacher_id).eq("date", date)
        .in_("status", list(_COVERING_STATUSES)).execute().data or []
    )
    covered_away_rows = (
        ac.table("timetable_substitutions").select("*")
        .eq("original_teacher_id", teacher_id).eq("date", date).execute().data or []
    )

    avail_res = (
        ac.table("teacher_availability").select("reason, status")
        .eq("teacher_id", teacher_id).eq("date", date).maybe_single().execute()
    )
    avail = avail_res.data if avail_res else None
    # A leave request still awaiting admin approval hasn't taken effect — the
    # teacher is on duty until it's approved, so it must not read as on-leave.
    on_leave = bool(avail) and (avail.get("status") or "approved") == "approved"

    class_name, teacher_name, times = _lookup_tables(
        teacher_id, covering_rows, covered_away_rows, [r["class_id"] for r in own_rows], ac,
    )

    # Keyed by class|period rather than row id so it matches whichever table
    # the caller's base entries came from (published `timetable` or the
    # admin's draft `school_timetable_periods`, which carry different ids for
    # the same slot).
    covered_away_by_slot = {f"{r['class_id']}|{r['period_number']}": r for r in covered_away_rows}

    entries = []

    for r in own_rows:
        sub = covered_away_by_slot.get(f"{r['class_id']}|{r['period_number']}")
        entries.append({
            "id": r["id"],
            "classId": r["class_id"],
            "className": class_name.get(r["class_id"], "Class"),
            "dayOfWeek": r["day_of_week"],
            "date": date,
            "periodNumber": r["period_number"],
            "startTime": r["start_time"],
            "endTime": r["end_time"],
            "label": r.get("label"),
            **(
                {
                    "coverage": "covered_away",
                    "substituteTeacherName": (
                        teacher_name.get(sub["substitute_teacher_id"])
                        if sub.get("substitute_teacher_id") else None
                    ),
                    # No substitute found. Surfaced rather than hidden: the
                    # class is genuinely uncovered and somebody has to act.
                    "unresolved": not sub.get("substitute_teacher_id"),
                }
                if sub else {"coverage": "regular"}
            ),
        })

    entries.extend(
        _covering_entry(r, date, day_of_week, class_name, teacher_name, times)
        for r in covering_rows
    )
    entries.sort(key=lambda e: (e["periodNumber"], e["classId"]))

    return {
        "date": date,
        "dayOfWeek": day_of_week,
        "onLeave": on_leave,
        "reason": (avail or {}).get("reason") if on_leave else None,
        "entries": entries,
    }


def overlay_substitutions_on_week(
    weekly_entries: list[dict], teacher_id: str, from_date: str, ac,
) -> list[dict]:
    """Apply the coming week's substitutions onto an already-built weekly timetable.

    Lets /school-data keep its existing weekly payload — and the draft-vs-
    published source choice it makes — while every weekday in the grid reflects
    what will actually happen on it.

    Covers the next seven days, not just today. Leave is usually approved in
    advance, so a cover the teacher picks up tomorrow is the normal case; a
    today-only overlay left tomorrow's schedule showing the period as free and
    the teacher with no reason to turn up.

    Matching is by class|period within the target weekday, which is why this
    can be handed draft-derived entries whose ids don't exist in `timetable`.
    """
    date_by_weekday = upcoming_dates_by_weekday(from_date)
    dates = list(date_by_weekday.values())

    covering_rows = (
        ac.table("timetable_substitutions").select("*")
        .eq("substitute_teacher_id", teacher_id).in_("date", dates)
        .in_("status", list(_COVERING_STATUSES)).execute().data or []
    )
    covered_away_rows = (
        ac.table("timetable_substitutions").select("*")
        .eq("original_teacher_id", teacher_id).in_("date", dates).execute().data or []
    )

    if not covering_rows and not covered_away_rows:
        # Overwhelmingly the common case. Tag and return without paying for the
        # name/time lookups. Still stamps `date` — a consumer shouldn't have to
        # cope with the field appearing only on days somebody happens to be off.
        return [
            {**e, "date": date_by_weekday.get(e["dayOfWeek"]), "coverage": "regular"}
            for e in weekly_entries
        ]

    class_name, teacher_name, times = _lookup_tables(
        teacher_id, covering_rows, covered_away_rows, [e["classId"] for e in weekly_entries], ac,
    )

    weekday_by_date = {date: weekday for weekday, date in date_by_weekday.items()}
    handed_over_by_slot = {
        f"{weekday_by_date[r['date']]}|{r['class_id']}|{r['period_number']}": r
        for r in covered_away_rows
    }

    # Only the coverage verdict is copied across. The caller's own id, times and
    # label stay theirs — /school-data may be serving draft rows whose times
    # differ from the published ones this module reads, and silently swapping
    # them would move periods on the teacher's grid as a side effect of someone
    # else being off sick.
    merged = []
    for e in weekly_entries:
        handed_over = handed_over_by_slot.get(f"{e['dayOfWeek']}|{e['classId']}|{e['periodNumber']}")
        merged.append({
            **e,
            "date": date_by_weekday.get(e["dayOfWeek"]),
            "coverage": "covered_away" if handed_over else "regular",
            **({
                "substituteTeacherName": teacher_name.get(handed_over["substitute_teacher_id"])
                    if handed_over.get("substitute_teacher_id") else None,
                "unresolved": not handed_over.get("substitute_teacher_id"),
            } if handed_over else {}),
        })

    # Covering periods have no weekly counterpart to merge onto — they exist
    # only for their own date — so they're appended whole.
    merged.extend(
        _covering_entry(r, r["date"], weekday_by_date[r["date"]], class_name, teacher_name, times)
        for r in covering_rows
    )
    return merged
