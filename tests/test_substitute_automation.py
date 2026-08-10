"""End-to-end behaviour of the absence automation, over a fake postgrest.

These assert the things the feature exists for and that no unit test of
find_substitute can catch: that recording an absence actually notifies the
people affected, that re-recording it doesn't notify them twice, and that
withdrawing it stands the substitute down.
"""
import pytest

from app.lib import events
from app.lib.substitute_automation import apply_teacher_absence, clear_teacher_absence
from fake_supabase import FakeClient

SCHOOL = "school-1"
# 2026-03-03 is a Tuesday -> day_of_week 2 under the JS getDay() convention the
# timetable tables use.
DATE = "2026-03-03"
TUESDAY = 2


def _teacher(tid, name, **extra):
    return {
        "id": tid, "user_id": f"user-{tid}", "name": name, "school_id": SCHOOL,
        "subject": "", "grade": "", "phone": "",
        "max_periods_per_day": None, "max_periods_per_week": None, **extra,
    }


def _period(pid, teacher_id, class_id, period_number, label, day=TUESDAY):
    return {
        "id": pid, "teacher_id": teacher_id, "class_id": class_id,
        "day_of_week": day, "period_number": period_number,
        "start_time": "09:20", "end_time": "10:00", "label": label,
    }


@pytest.fixture
def school():
    """Anita teaches 7B Science period 4 on Tuesday. Bhaskar also teaches
    Science and is free then. Chandra is the class teacher for 7B and teaches
    something else at that time, so is not a candidate."""
    return FakeClient({
        "teachers": [
            _teacher("t-anita", "Anita"),
            _teacher("t-bhaskar", "Bhaskar"),
            _teacher("t-chandra", "Chandra"),
        ],
        "classes": [{"id": "c-7b", "name": "7B", "teacher_id": "t-chandra", "school_id": SCHOOL}],
        "timetable": [
            _period("tt-1", "t-anita", "c-7b", 4, "Science"),
            _period("tt-2", "t-bhaskar", "c-8a", 2, "Science"),
            _period("tt-3", "t-chandra", "c-7b", 4, "Maths"),
        ],
        "teacher_class_assignments": [
            {"teacher_id": "t-anita", "subject": "Science"},
            {"teacher_id": "t-bhaskar", "subject": "Science"},
            {"teacher_id": "t-chandra", "subject": "Maths"},
        ],
        "teacher_availability": [],
        "timetable_substitutions": [],
        "teacher_notifications": [],
        "admin_notifications": [],
        "automation_events": [],
    })


def notifications_for(client, teacher_id):
    return [n for n in client.rows("teacher_notifications") if n["teacher_id"] == teacher_id]


def types_for(client, teacher_id):
    return sorted(n["type"] for n in notifications_for(client, teacher_id))


def open_admin_alerts(client):
    """Unacknowledged only — an auto-resolved alert is stamped read, not deleted."""
    return [n for n in client.rows("admin_notifications") if not n.get("read_at")]


# ── Assignment ────────────────────────────────────────────────────────────────

def test_absence_assigns_a_qualified_substitute(school):
    subs = apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert len(subs) == 1
    assert subs[0]["substituteTeacherId"] == "t-bhaskar"
    assert subs[0]["status"] == "assigned"


def test_substitute_is_notified_on_a_same_day_check_in(school):
    """The gap this whole module closes: before, the teacher's own check-in
    assigned a substitute and told nobody."""
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    notes = notifications_for(school, "t-bhaskar")
    assert len(notes) == 1
    assert notes[0]["type"] == "substitute_assigned"
    assert "Period 4" in notes[0]["message"]
    assert "7B" in notes[0]["message"]
    assert "Anita" in notes[0]["message"]
    assert notes[0]["date"] == DATE
    assert notes[0]["class_id"] == "c-7b"


def test_absent_teacher_learns_who_has_their_class(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    notes = notifications_for(school, "t-anita")
    assert [n["type"] for n in notes] == ["coverage_arranged"]
    assert "Bhaskar" in notes[0]["message"]


def test_the_absent_teacher_gets_one_digest_not_one_message_per_period(school):
    """Four periods a day over a week's leave is 20 near-identical rows in the
    notification bell if this is emitted per period."""
    school.rows("timetable").extend([
        _period("tt-4", "t-anita", "c-8a", 5, "Science"),
        _period("tt-5", "t-anita", "c-9c", 6, "Science"),
    ])
    school.rows("classes").extend([
        {"id": "c-8a", "name": "8A", "teacher_id": None, "school_id": SCHOOL},
        {"id": "c-9c", "name": "9C", "teacher_id": None, "school_id": SCHOOL},
    ])

    subs = apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    assert len(subs) == 3

    digests = notifications_for(school, "t-anita")
    assert len(digests) == 1
    assert "3 of 3 periods arranged" in digests[0]["message"]
    # Bhaskar, in contrast, gets told about each period he's picked up.
    assert len(notifications_for(school, "t-bhaskar")) == 3


def test_the_digest_names_periods_nobody_could_take(school):
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    digest = next(n for n in notifications_for(school, "t-anita") if n["type"] == "coverage_arranged")
    assert "0 of 1 periods arranged" in digest["message"]
    assert "NOT COVERED" in digest["message"]


def test_notification_carries_the_period_start_time(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    assert "9:20 AM" in notifications_for(school, "t-bhaskar")[0]["message"]


# ── Idempotence ───────────────────────────────────────────────────────────────

def test_recording_the_same_absence_twice_does_not_re_notify(school):
    """mark_teacher_unavailable recomputes from scratch every call, so without
    the diff an admin correcting the reason would re-announce every cover."""
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "sick", "admin", "called in", school)

    assert len(notifications_for(school, "t-bhaskar")) == 1


def test_reassignment_stands_down_the_previous_substitute(school):
    """Bhaskar is assigned, then becomes unavailable himself. The recompute
    moves the cover, and Bhaskar must be told he's off it."""
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    # Chandra frees up so there's somewhere for the cover to move to.
    school.store["timetable"] = [r for r in school.rows("timetable") if r["id"] != "tt-3"]
    school.rows("teacher_class_assignments").append({"teacher_id": "t-chandra", "subject": "Science"})

    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert "cover_cancelled" in types_for(school, "t-bhaskar")
    sub = next(s for s in school.rows("timetable_substitutions") if s["original_teacher_id"] == "t-anita")
    assert sub["substitute_teacher_id"] == "t-chandra"


# ── Nobody available ──────────────────────────────────────────────────────────

def test_uncovered_period_alerts_the_class_teacher(school):
    """Bhaskar is the only candidate; with him away too, period 4 has no cover
    and 7B's own class teacher needs to know."""
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    subs = apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert subs[0]["substituteTeacherId"] is None
    assert subs[0]["status"] == "unresolved"
    assert "class_uncovered" in types_for(school, "t-chandra")


def test_cover_stays_within_subject_even_when_someone_is_free(school):
    """Deepa is free all day but teaches Art. A free body is not a substitute —
    the period goes unresolved rather than to someone who can't teach it."""
    school.rows("teachers").append(_teacher("t-deepa", "Deepa"))
    school.rows("teacher_class_assignments").append({"teacher_id": "t-deepa", "subject": "Art"})
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)

    subs = apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert subs[0]["substituteTeacherId"] is None
    assert subs[0]["status"] == "unresolved"
    assert notifications_for(school, "t-deepa") == []


def test_no_assignment_is_ever_recorded_as_a_fallback(school):
    """The automation has one assigned status now. Anything else on a fresh
    row would mean the subject rule was bypassed somewhere."""
    school.rows("teachers").append(_teacher("t-deepa", "Deepa"))
    school.rows("teacher_class_assignments").append({"teacher_id": "t-deepa", "subject": "Art"})

    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert {s["status"] for s in school.rows("timetable_substitutions")} <= {"assigned", "unresolved"}


def test_an_unfillable_period_escalates_instead_of_being_booked(school):
    """The tradeoff for the subject rule: more unresolved periods. They have to
    reach someone who can act, or the rule just loses classes quietly."""
    school.rows("teachers").append(_teacher("t-deepa", "Deepa"))
    school.rows("teacher_class_assignments").append({"teacher_id": "t-deepa", "subject": "Art"})
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)

    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert [a["type"] for a in open_admin_alerts(school)] == ["period_uncovered"]
    assert "class_uncovered" in types_for(school, "t-chandra")


def test_uncovered_period_raises_an_admin_alert(school):
    """Only an admin can actually fix this — reassign across the staff,
    override a cap, move the class."""
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    alerts = open_admin_alerts(school)
    assert len(alerts) == 1
    assert alerts[0]["type"] == "period_uncovered"
    assert alerts[0]["school_id"] == SCHOOL
    assert alerts[0]["class_id"] == "c-7b"
    assert alerts[0]["period_number"] == 4
    assert alerts[0]["date"] == DATE
    assert "Anita" in alerts[0]["message"]
    assert "7B" in alerts[0]["message"]


def test_a_covered_period_raises_no_admin_alert(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    assert open_admin_alerts(school) == []


def test_the_alert_clears_itself_once_the_period_gets_covered(school):
    """Bhaskar comes back, so Anita's period 4 can be covered after all. The
    admin shouldn't still be looking at an alert for a solved problem."""
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    assert len(open_admin_alerts(school)) == 1

    clear_teacher_absence(SCHOOL, "t-bhaskar", DATE, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)

    assert open_admin_alerts(school) == []


def test_the_alert_clears_when_the_absent_teacher_returns(school):
    """An unresolved period never had a substitute, so no cancellation event
    covers it — the alert has to be cleared directly."""
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    assert len(open_admin_alerts(school)) == 1

    clear_teacher_absence(SCHOOL, "t-anita", DATE, school)

    assert open_admin_alerts(school) == []


def test_class_teacher_is_not_alerted_about_their_own_absence(school):
    """Chandra teaches 7B period 4 and is 7B's class teacher. Telling them
    their own class is uncovered is noise — they know."""
    apply_teacher_absence(SCHOOL, "t-bhaskar", DATE, "sick", "teacher", None, school)
    apply_teacher_absence(SCHOOL, "t-chandra", DATE, "on_leave", "teacher", None, school)

    assert "class_uncovered" not in types_for(school, "t-chandra")


# ── Withdrawal ────────────────────────────────────────────────────────────────

def test_returning_to_duty_cancels_the_cover_and_tells_the_substitute(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    clear_teacher_absence(SCHOOL, "t-anita", DATE, school)

    assert school.rows("timetable_substitutions") == []
    assert school.rows("teacher_availability") == []
    assert "cover_cancelled" in types_for(school, "t-bhaskar")
    cancel = next(n for n in notifications_for(school, "t-bhaskar") if n["type"] == "cover_cancelled")
    assert "Anita is back in" in cancel["message"]


def test_clearing_an_absence_that_was_never_recorded_is_silent(school):
    clear_teacher_absence(SCHOOL, "t-anita", DATE, school)
    assert school.rows("teacher_notifications") == []


# ── Audit trail ───────────────────────────────────────────────────────────────

def test_every_decision_is_recorded(school):
    apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    logged = [e["type"] for e in school.rows("automation_events")]

    assert events.TEACHER_UNAVAILABLE in logged
    assert events.SUBSTITUTION_ASSIGNED in logged
    assert all(e["school_id"] == SCHOOL and e["date"] == DATE for e in school.rows("automation_events"))


# ── Failure isolation ─────────────────────────────────────────────────────────

def test_a_failing_handler_does_not_fail_the_absence(school, monkeypatch):
    """The absence is already committed by the time handlers run. A broken
    notification must not surface as a 500 on a check-in that worked."""
    import app.lib.substitute_automation as automation

    def boom(*_args, **_kwargs):
        raise RuntimeError("notification backend down")

    monkeypatch.setattr(automation, "create_notification", boom)

    subs = apply_teacher_absence(SCHOOL, "t-anita", DATE, "on_leave", "teacher", None, school)
    assert subs[0]["substituteTeacherId"] == "t-bhaskar"
