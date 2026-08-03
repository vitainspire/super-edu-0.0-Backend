"""Substitute-finder behaviour, driven by shared/parity/substitute-finder.json.

The same fixture drives frontend/__tests__/lib/substituteFinder.parity.test.ts
against the TypeScript original in frontend/lib/substituteFinder.ts. Add cases to
the fixture, not here, so both implementations stay pinned to one spec.
"""

import pytest

from app.lib.substitute_finder import find_substitute, suggest_swap
from conftest import load_parity_fixture

FIXTURE = load_parity_fixture("substitute-finder.json")


def hydrate(raw: dict) -> dict:
    """JSON has no sets; convert the encoded arrays back.

    An absent cap key and an explicit null both mean "no cap". The distinction is
    preserved rather than normalised away so the implementation is what's tested
    — `.get()` returning None must be the thing that reads as uncapped.
    """
    candidate = {
        "teacherId": raw["teacherId"],
        "name": raw["name"],
        "subjectsTaught": set(raw["subjectsTaught"]),
        "busySlots": set(raw["busySlots"]),
        "weeklyLoad": raw["weeklyLoad"],
    }
    for cap in ("maxPeriodsPerDay", "maxPeriodsPerWeek"):
        if cap in raw:
            candidate[cap] = raw[cap]
    return candidate


# ── find_substitute ───────────────────────────────────────────────────────────


def test_find_substitute_fixture_is_not_empty():
    assert len(FIXTURE["findSubstitute"]) > 0


@pytest.mark.parametrize(
    "case", FIXTURE["findSubstitute"], ids=[c["name"] for c in FIXTURE["findSubstitute"]]
)
def test_find_substitute(case):
    actual = find_substitute(
        case["need"],
        [hydrate(c) for c in case["candidates"]],
        set(case.get("excludeTeacherIds", [])),
        set(case.get("alreadyUsedThisSlot", [])),
        case.get("requireSubjectMatch", True),
    )
    assert actual == case["expected"]


# ── suggest_swap ──────────────────────────────────────────────────────────────


def test_suggest_swap_fixture_is_not_empty():
    assert len(FIXTURE["suggestSwap"]) > 0


@pytest.mark.parametrize(
    "case", FIXTURE["suggestSwap"], ids=[c["name"] for c in FIXTURE["suggestSwap"]]
)
def test_suggest_swap(case):
    used_by_slot = case.get("alreadyUsedThisSlot", {})

    actual = suggest_swap(
        case["need"],
        case["classPeriodsThatDay"],
        [hydrate(c) for c in case["candidates"]],
        set(case.get("excludeTeacherIds", [])),
        lambda period_number: set(used_by_slot.get(str(period_number), [])),
    )
    assert actual == case["expected"]
