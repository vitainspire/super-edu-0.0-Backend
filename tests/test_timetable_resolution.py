"""The readjusted day: base weekly plan ⊕ that date's substitutions."""
import pytest

from app.lib.substitute_automation import apply_teacher_absence
from app.lib.timetable_resolution import (
    day_of_week_for, resolve_day_timetable, overlay_substitutions_on_week,
    upcoming_dates_by_weekday,
)
from fake_supabase import FakeClient

SCHOOL = "school-1"
DATE = "2026-03-03"      # Tuesday
TUESDAY = 2
WEDNESDAY = 3


def _teacher(tid, name):
    return {
        "id": tid, "user_id": f"user-{tid}", "name": name, "school_id": SCHOOL,
        "subject": "", "grade": "", "phone": "",
        "max_periods_per_day": None, "max_periods_per_week": None,
    }


def _period(pid, teacher_id, class_id, period_number, label, day=TUESDAY, start="09:20", end="10:00"):
    return {
        "id": pid, "teacher_id": teacher_id, "class_id": class_id,
        "day_of_week": day, "period_number": period_number,
        "start_time": start, "end_time": end, "label": label,
    }


@pytest.fixture
def school():
    return FakeClient({
        "teachers": [_teacher("t-anita", "Anita"), _teacher("t-bhaskar", "Bhaskar")],
        "classes": [
            {"id": "c-7b", "name": "7B", "teacher_id": None, "school_id": SCHOOL},
            {"id": "c-8a", "name": "8A", "teacher_id": None, "school_id": SCHOOL},
        ],
        "timetable": [
            _period("tt-1", "t-anita", "c-7b", 4, "Science"),
            _period("tt-2", "t-anita", "c-8a", 6, "Science", start="12:00", end="12:40"),
            _period("tt-3", "t-bhaskar", "c-8a", 2, "Science", start="08:00", end="08:40"),
            _period("tt-4", "t-bhaskar", "c-7b", 1, "Science", day=WEDNESDAY),
        ],
        "teacher_class_assignments": [
            {"teacher_id": "t-anita", "subject": "Science"},
            {"teacher_id": "t-bhaskar", "subject": "Science"},
        ],
        "teacher_availability": [],
        "timetable_substitutions": [],
        "teacher_notifications": [],
        "automation_events": [],
    })


def entries_by_period(resolved):
    return {e["periodNumber"]: e for e in resolved["entries"]}


def test_day_of_week_matches_the_js_getday_convention():
    assert day_of_week_for("2026-03-01") == 0   # Sunday
    assert day_of_week_for("2026-03-02") == 1   # Monday
    assert day_of_week_for("2026-03-07") == 6   # Saturday


def test_an_untouched_day_is_all_regular(school):
    resolved = resolve_day_timetable("t-anita", DATE, school)

    assert resolved["onLeave"] is False
    assert [e["coverage"] for e in resolved["entries"]] == ["regular", "regular"]


def test_entries_are_ordered_by_period(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    resolved = resolve_day_timetable("t-bhaskar", DATE, school)

    assert [e["periodNumber"] for e in resolved["entries"]] == sorted(e["periodNumber"] for e in resolved["entries"])


# ── The substitute's day ──────────────────────────────────────────────────────

def test_the_covered_period_appears_on_the_substitutes_day(school):
    """The whole point: Bhaskar's Tuesday now contains Anita's period 4, which
    is nowhere in his weekly plan."""
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    resolved = resolve_day_timetable("t-bhaskar", DATE, school)
    covering = [e for e in resolved["entries"] if e["coverage"] == "covering"]

    assert len(covering) == 2  # Anita's period 4 and period 6
    period4 = entries_by_period(resolved)[4]
    assert period4["className"] == "7B"
    assert period4["label"] == "Science"
    assert period4["originalTeacherName"] == "Anita"
    assert period4["fullAccess"] is True


def test_a_covering_entry_carries_the_original_periods_times(school):
    """Substitution rows store class and period, not times — they have to be
    read back off the absent teacher's own row or the period can't be placed
    on a timetable grid."""
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    period6 = entries_by_period(resolve_day_timetable("t-bhaskar", DATE, school))[6]
    assert period6["startTime"] == "12:00"
    assert period6["endTime"] == "12:40"


def test_an_unqualified_teacher_is_never_drafted_in(school):
    """No Science teacher is free and Deepa teaches Art. She must NOT be given
    the period — the class is left uncovered for an admin to resolve rather
    than booked to someone who can't teach it."""
    school.rows("teachers").append(_teacher("t-deepa", "Deepa"))
    school.rows("teacher_class_assignments").append({"teacher_id": "t-deepa", "subject": "Art"})
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)

    subs = apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert all(s["substituteTeacherId"] is None for s in subs)
    assert all(s["status"] == "unresolved" for s in subs)
    assert resolve_day_timetable("t-deepa", DATE, school)["entries"] == []


def test_a_restricted_cover_is_shown_but_grants_no_class_access(school):
    """Rows the automation no longer produces still exist: pre-existing
    "assigned_fallback" data, and "manual" from an admin hand-picking someone
    outside their subject. Both must render — the teacher has to know to turn
    up — with nothing behind them."""
    school.rows("teachers").append(_teacher("t-deepa", "Deepa"))
    school.rows("timetable_substitutions").append({
        "id": "sub-legacy", "school_id": SCHOOL, "date": DATE, "day_of_week": TUESDAY,
        "period_number": 4, "class_id": "c-7b", "subject": "Science",
        "original_teacher_id": "t-anita", "substitute_teacher_id": "t-deepa",
        "status": "manual",
    })

    period4 = entries_by_period(resolve_day_timetable("t-deepa", DATE, school))[4]
    assert period4["coverage"] == "covering"
    assert period4["substitutionStatus"] == "manual"
    assert period4["fullAccess"] is False
    assert period4["className"] == "7B"


def test_the_substitutes_own_periods_survive_the_merge(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    resolved = resolve_day_timetable("t-bhaskar", DATE, school)
    assert entries_by_period(resolved)[2]["coverage"] == "regular"


# ── The absent teacher's day ──────────────────────────────────────────────────

def test_the_absent_teachers_periods_are_marked_handed_over(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    resolved = resolve_day_timetable("t-anita", DATE, school)
    assert resolved["onLeave"] is True
    assert resolved["reason"] == "on_leave"

    period4 = entries_by_period(resolved)[4]
    assert period4["coverage"] == "covered_away"
    assert period4["substituteTeacherName"] == "Bhaskar"
    assert period4["unresolved"] is False


def test_a_period_nobody_could_take_is_flagged_unresolved(school):
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    period4 = entries_by_period(resolve_day_timetable("t-anita", DATE, school))[4]
    assert period4["coverage"] == "covered_away"
    assert period4["unresolved"] is True
    assert period4["substituteTeacherName"] is None


def test_a_pending_leave_request_has_not_taken_effect_yet(school):
    """Submitted but not approved: the teacher is still on duty and their day
    must not read as on-leave."""
    school.rows("teacher_availability").append({
        "id": "t-anita-" + DATE, "school_id": SCHOOL, "teacher_id": "t-anita",
        "date": DATE, "reason": "on_leave", "note": None, "source": "teacher", "status": "pending",
    })

    resolved = resolve_day_timetable("t-anita", DATE, school)
    assert resolved["onLeave"] is False
    assert [e["coverage"] for e in resolved["entries"]] == ["regular", "regular"]


# ── Weekly overlay (what /school-data serves) ─────────────────────────────────

def _week_dto(client, teacher_id):
    return [
        {
            "id": r["id"], "teacherId": r["teacher_id"], "classId": r["class_id"],
            "dayOfWeek": r["day_of_week"], "periodNumber": r["period_number"],
            "startTime": r["start_time"], "endTime": r["end_time"], "label": r.get("label"),
        }
        for r in client.rows("timetable") if r["teacher_id"] == teacher_id
    ]


def test_overlay_tags_other_weekdays_regular_and_leaves_them_alone(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    merged = overlay_substitutions_on_week(_week_dto(school, "t-bhaskar"), "t-bhaskar", DATE, school)
    wednesday = [e for e in merged if e["dayOfWeek"] == WEDNESDAY]

    assert len(wednesday) == 1
    assert wednesday[0]["coverage"] == "regular"
    assert wednesday[0]["id"] == "tt-4"


def test_overlay_appends_covering_periods_to_the_week(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    merged = overlay_substitutions_on_week(_week_dto(school, "t-bhaskar"), "t-bhaskar", DATE, school)
    covering = [e for e in merged if e["coverage"] == "covering"]

    assert {e["periodNumber"] for e in covering} == {4, 6}
    assert all(e["dayOfWeek"] == TUESDAY for e in covering)


def test_overlay_matches_slots_by_class_and_period_not_row_id(school):
    """/school-data may serve draft (school_timetable_periods) rows, whose ids
    differ from the published `timetable` ids the substitutions were computed
    against. Matching must survive that."""
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    draft_shaped = [{**e, "id": f"stp-{e['id']}"} for e in _week_dto(school, "t-anita")]
    merged = overlay_substitutions_on_week(draft_shaped, "t-anita", DATE, school)

    period4 = next(e for e in merged if e["periodNumber"] == 4)
    assert period4["coverage"] == "covered_away"
    assert period4["substituteTeacherName"] == "Bhaskar"
    assert period4["id"] == "stp-tt-1"  # the caller's id is preserved


def test_overlay_is_a_no_op_when_nobody_is_away(school):
    merged = overlay_substitutions_on_week(_week_dto(school, "t-anita"), "t-anita", DATE, school)
    assert all(e["coverage"] == "regular" for e in merged)
    assert len(merged) == 2


# ── The overlay window ────────────────────────────────────────────────────────

def test_upcoming_dates_cover_each_weekday_once():
    dates = upcoming_dates_by_weekday(DATE)          # Tuesday 2026-03-03
    assert len(dates) == 7
    assert dates[TUESDAY] == DATE                    # today is day 0
    assert dates[WEDNESDAY] == "2026-03-04"
    assert dates[1] == "2026-03-09"                  # Monday already gone; next one


def test_a_cover_assigned_for_tomorrow_lands_on_tomorrow(school):
    """The reported bug. Leave is normally approved in advance, so the cover a
    teacher picks up is usually not today's — and a today-only overlay left
    tomorrow's schedule showing the period as free."""
    tomorrow = "2026-03-04"                          # Wednesday
    school.rows("timetable").append(
        _period("tt-5", "t-anita", "c-7b", 3, "Science", day=WEDNESDAY)
    )

    apply_teacher_absence(SCHOOL, "t-anita", tomorrow, "on_leave", "teacher", None, school)

    merged = overlay_substitutions_on_week(_week_dto(school, "t-bhaskar"), "t-bhaskar", DATE, school)
    covering = [e for e in merged if e["coverage"] == "covering"]

    assert len(covering) == 1
    assert covering[0]["dayOfWeek"] == WEDNESDAY
    assert covering[0]["date"] == tomorrow
    assert covering[0]["periodNumber"] == 3
    assert covering[0]["className"] == "7B"


def test_covers_on_different_days_land_on_their_own_days(school):
    """Two absences on two dates must not bleed into each other's weekday."""
    school.rows("timetable").append(
        _period("tt-5", "t-anita", "c-7b", 3, "Science", day=WEDNESDAY)
    )
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", "2026-03-04", "on_leave", "teacher", None, school)

    merged = overlay_substitutions_on_week(_week_dto(school, "t-bhaskar"), "t-bhaskar", DATE, school)
    by_day = {}
    for e in (x for x in merged if x["coverage"] == "covering"):
        by_day.setdefault(e["dayOfWeek"], []).append(e["periodNumber"])

    assert sorted(by_day[TUESDAY]) == [4, 6]
    assert by_day[WEDNESDAY] == [3]


def test_a_cover_beyond_the_window_is_not_shown(school):
    """Eight days out is a different occurrence of that weekday than the one
    the grid is showing — claiming it would put a cover on the wrong date."""
    apply_teacher_absence(SCHOOL, "t-anita", "2026-03-11", "on_leave", "teacher", None, school)

    merged = overlay_substitutions_on_week(_week_dto(school, "t-bhaskar"), "t-bhaskar", DATE, school)
    assert [e for e in merged if e["coverage"] == "covering"] == []


def test_regular_entries_carry_the_date_their_weekday_resolves_to(school):
    merged = overlay_substitutions_on_week(_week_dto(school, "t-bhaskar"), "t-bhaskar", DATE, school)
    by_day = {e["dayOfWeek"]: e for e in merged}

    assert by_day[TUESDAY]["date"] == DATE
    assert by_day[WEDNESDAY]["date"] == "2026-03-04"
