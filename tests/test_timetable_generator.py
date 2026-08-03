"""Whole-school timetable generation, driven by shared/parity/timetable-generator.json.

The generator shuffles candidate slots, so there is no single correct output to
compare against — and no way to compare byte-for-byte with the TypeScript port.
Both suites therefore assert the same INVARIANTS, and each scenario is run
repeatedly so a rule that only breaks on an unlucky shuffle still fails.

frontend/__tests__/lib/timetableGenerator.test.ts asserts the identical set
against frontend/lib/timetableGenerator.ts.
"""

import pytest

from app.lib.timetable_generator import generate_school_timetable
from conftest import load_parity_fixture

FIXTURE = load_parity_fixture("timetable-generator.json")
SLOT_TEMPLATE = FIXTURE["slotTemplate"]
SCENARIOS = FIXTURE["scenarios"]
SCENARIO_IDS = [s["name"] for s in SCENARIOS]

RUNS_PER_SCENARIO = 25


def run(scenario: dict) -> dict:
    return generate_school_timetable(
        SLOT_TEMPLATE,
        scenario["workingWeekdays"],
        scenario["classes"],
        scenario["lineup"],
        scenario["assignments"],
        scenario["teacherCaps"],
        scenario.get("existingPeriods", []),
    )


def valid_slot_keys(scenario: dict) -> set:
    periods = [s for s in SLOT_TEMPLATE if s.get("type") == "period" and s.get("periodNumber") is not None]
    return {
        f"{day}|{s['periodNumber']}"
        for day in scenario["workingWeekdays"]
        for s in periods
    }


def required_by_class(scenario: dict) -> dict:
    out = {}
    for cls in scenario["classes"]:
        out[cls["classId"]] = sum(
            max(0, item["periodsPerWeek"])
            for item in scenario["lineup"]
            if item["grade"] == cls["grade"]
        )
    return out


def test_fixture_has_scenarios():
    assert len(SCENARIOS) > 0


# ── Invariants that must hold for every scenario, on every shuffle ─────────────


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_only_emits_slots_from_the_bell_schedule(scenario):
    allowed = valid_slot_keys(scenario)
    for _ in range(RUNS_PER_SCENARIO):
        for p in run(scenario)["periods"]:
            assert f"{p['dayOfWeek']}|{p['periodNumber']}" in allowed


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_never_double_books_a_class(scenario):
    for _ in range(RUNS_PER_SCENARIO):
        seen = set()
        for p in run(scenario)["periods"]:
            key = (p["classId"], p["dayOfWeek"], p["periodNumber"])
            assert key not in seen
            seen.add(key)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_never_double_books_a_teacher(scenario):
    for _ in range(RUNS_PER_SCENARIO):
        seen = set()
        for p in run(scenario)["periods"]:
            if not p.get("teacherId"):
                continue
            key = (p["teacherId"], p["dayOfWeek"], p["periodNumber"])
            assert key not in seen
            seen.add(key)


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_never_breaches_a_daily_workload_cap(scenario):
    cap_of = {t["teacherId"]: t.get("maxPeriodsPerDay") for t in scenario["teacherCaps"]}
    for _ in range(RUNS_PER_SCENARIO):
        counts: dict = {}
        for p in run(scenario)["periods"]:
            if not p.get("teacherId"):
                continue
            key = (p["teacherId"], p["dayOfWeek"])
            counts[key] = counts.get(key, 0) + 1
        for (teacher_id, _day), count in counts.items():
            cap = cap_of.get(teacher_id)
            if cap is not None:
                assert count <= cap


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_never_exceeds_the_requested_periods_per_subject(scenario):
    grade_of = {c["classId"]: c["grade"] for c in scenario["classes"]}
    for _ in range(RUNS_PER_SCENARIO):
        counted: dict = {}
        for p in run(scenario)["periods"]:
            key = (p["classId"], p["label"])
            counted[key] = counted.get(key, 0) + 1
        for (class_id, subject), count in counted.items():
            wanted = next(
                (
                    item["periodsPerWeek"]
                    for item in scenario["lineup"]
                    if item["grade"] == grade_of.get(class_id) and item["subject"] == subject
                ),
                0,
            )
            assert count <= wanted


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_placed_plus_kept_reconciles_with_emitted_periods(scenario):
    for _ in range(RUNS_PER_SCENARIO):
        r = run(scenario)
        assert r["keptCount"] + r["placedCount"] == len(r["periods"])


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_every_required_period_is_placed_kept_or_skipped(scenario):
    required = required_by_class(scenario)
    for _ in range(RUNS_PER_SCENARIO):
        for stat in run(scenario)["classStats"]:
            assert stat["placed"] + stat["kept"] + stat["skipped"] == required[stat["classId"]]


@pytest.mark.parametrize("scenario", SCENARIOS, ids=SCENARIO_IDS)
def test_reports_one_unplaced_entry_per_skipped_period(scenario):
    for _ in range(RUNS_PER_SCENARIO):
        r = run(scenario)
        skipped = sum(c["skipped"] for c in r["classStats"])
        assert len(r["unplaced"]) == skipped
        for u in r["unplaced"]:
            assert u["reason"]


# ── Scenario-specific expectations ────────────────────────────────────────────

PINNED = [s for s in SCENARIOS if s.get("existingPeriods")]
FULLY_PLACED = [s for s in SCENARIOS if s.get("expectFullyPlaced")]
SOME_UNPLACED = [s for s in SCENARIOS if s.get("expectSomeUnplaced")]
WITH_WARNINGS = [s for s in SCENARIOS if s.get("expectTeacherWarnings")]


@pytest.mark.parametrize("scenario", PINNED, ids=[s["name"] for s in PINNED])
def test_preserves_pinned_periods_at_their_exact_slot(scenario):
    expected_kept = scenario["expectKeptCount"]
    for _ in range(RUNS_PER_SCENARIO):
        r = run(scenario)
        assert r["keptCount"] == expected_kept

        # Whatever was kept must appear identically, not merely re-placed
        kept = [
            p
            for p in r["periods"]
            if any(
                e["classId"] == p["classId"]
                and e["dayOfWeek"] == p["dayOfWeek"]
                and e["periodNumber"] == p["periodNumber"]
                and e["label"] == p["label"]
                and e.get("teacherId") == p.get("teacherId")
                for e in scenario["existingPeriods"]
            )
        ]
        assert len(kept) == expected_kept


@pytest.mark.parametrize("scenario", FULLY_PLACED, ids=[s["name"] for s in FULLY_PLACED])
def test_places_every_required_period(scenario):
    for _ in range(RUNS_PER_SCENARIO):
        r = run(scenario)
        assert r["unplaced"] == []
        for stat in r["classStats"]:
            assert stat["skipped"] == 0


@pytest.mark.parametrize("scenario", SOME_UNPLACED, ids=[s["name"] for s in SOME_UNPLACED])
def test_leaves_periods_unplaced_rather_than_breaching_a_cap(scenario):
    for _ in range(RUNS_PER_SCENARIO):
        assert len(run(scenario)["unplaced"]) > 0


@pytest.mark.parametrize("scenario", WITH_WARNINGS, ids=[s["name"] for s in WITH_WARNINGS])
def test_reports_expected_overload_warnings(scenario):
    r = run(scenario)
    for expected in scenario["expectTeacherWarnings"]:
        actual = next(
            (w for w in r["teacherWarnings"] if w["teacherId"] == expected["teacherId"]), None
        )
        assert actual is not None
        assert actual["requiredPeriods"] == expected["requiredPeriods"]
        assert actual["availableSlots"] == expected["availableSlots"]
        assert actual["overBy"] == expected["overBy"]


# ── Behaviour not expressible in the shared fixture ───────────────────────────


def test_returns_nothing_when_there_are_no_classes():
    r = generate_school_timetable(SLOT_TEMPLATE, [1, 2, 3, 4, 5], [], [], [], [])
    assert r["periods"] == []
    assert r["classStats"] == []
    assert r["keptCount"] == 0
    assert r["placedCount"] == 0


def test_returns_nothing_placeable_without_working_weekdays():
    r = generate_school_timetable(
        SLOT_TEMPLATE,
        [],
        [{"classId": "c1", "className": "1A", "grade": "1"}],
        [{"grade": "1", "subject": "Math", "periodsPerWeek": 3}],
        [{"classId": "c1", "subject": "Math", "teacherId": "t1"}],
        [],
    )
    assert r["periods"] == []
    assert len(r["unplaced"]) == 3


def test_ignores_break_slots():
    r = generate_school_timetable(
        SLOT_TEMPLATE,
        [1],
        [{"classId": "c1", "className": "1A", "grade": "1"}],
        [{"grade": "1", "subject": "Math", "periodsPerWeek": 6}],
        [{"classId": "c1", "subject": "Math", "teacherId": "t1"}],
        [],
    )
    # 6 periods/day in the template, 2 breaks — a 7th would mean breaks leaked in
    assert len(r["periods"]) == 6
    assert all(1 <= p["periodNumber"] <= 6 for p in r["periods"])


def test_places_subjects_with_no_teacher_assigned():
    r = generate_school_timetable(
        SLOT_TEMPLATE,
        [1, 2],
        [{"classId": "c1", "className": "1A", "grade": "1"}],
        [{"grade": "1", "subject": "Library", "periodsPerWeek": 2, "category": "special"}],
        [],
        [],
    )
    assert len(r["periods"]) == 2
    assert all(not p.get("teacherId") for p in r["periods"])


def test_treats_a_negative_periods_per_week_as_zero():
    r = generate_school_timetable(
        SLOT_TEMPLATE,
        [1],
        [{"classId": "c1", "className": "1A", "grade": "1"}],
        [{"grade": "1", "subject": "Math", "periodsPerWeek": -5}],
        [{"classId": "c1", "subject": "Math", "teacherId": "t1"}],
        [],
    )
    assert r["periods"] == []
    assert r["unplaced"] == []


def test_carries_slot_times_onto_each_generated_period():
    r = generate_school_timetable(
        SLOT_TEMPLATE,
        [1],
        [{"classId": "c1", "className": "1A", "grade": "1"}],
        [{"grade": "1", "subject": "Math", "periodsPerWeek": 6}],
        [{"classId": "c1", "subject": "Math", "teacherId": "t1"}],
        [],
    )
    by_number = {
        s["periodNumber"]: s
        for s in SLOT_TEMPLATE
        if s.get("type") == "period" and s.get("periodNumber") is not None
    }
    for p in r["periods"]:
        template = by_number[p["periodNumber"]]
        assert p["startTime"] == template["startTime"]
        assert p["endTime"] == template["endTime"]
