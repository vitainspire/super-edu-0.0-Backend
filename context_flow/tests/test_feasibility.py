"""Can the room supply the object the period is built on?

The case throughout is real. A school recorded "Basic textbook and notebooks;
limited supplementary materials" with paper verified present. Node 1 built
period 2 on `match-sticks` and period 3 on `bottle caps`, and recorded on
period 2 that there were "no actual matchsticks or similar manipulatives".
Generation printed "Distribute matchsticks to each pair".
"""
import pytest

from context_flow import feasibility as FE
from context_flow import profile as P


def _profile(**over):
    raw = {"materials_available": {"value": "Basic textbook and notebooks",
                                   "provenance": "verified", "origin": "school_setup"},
           "has_paper": {"value": True, "provenance": "verified", "origin": "school_setup"},
           "has_board": {"value": True, "provenance": "verified", "origin": "school_setup"}}
    raw.update(over)
    return P.normalise(raw)


def test_a_single_demonstration_object_is_never_substituted():
    """Quantity is the test. One cup is not a procurement problem."""
    assert FE.anchor_state("cup", _profile()) == FE.AVAILABLE
    assert FE._role_of("cup") is None
    assert FE.substitute_for("cup", _profile()) is None


def test_a_bulk_manipulative_the_room_lacks_is_unavailable():
    assert FE.anchor_state("match-sticks", _profile()) == FE.UNAVAILABLE
    assert FE.anchor_state("bottle caps", _profile()) == FE.UNAVAILABLE


def test_paper_stands_in_for_both_roles():
    straight, _ = FE.substitute_for("match-sticks", _profile())
    countable, _ = FE.substitute_for("bottle caps", _profile())
    assert "strips" in straight            # straight things need straight things
    assert "squares" in countable          # countable things only need counting
    assert straight != countable


def test_an_unrecorded_room_is_unknown_not_unavailable():
    """The state that caused the bug: a gap in paperwork is not a shortage.

    Substituting here would rewrite the lessons of every school that documented
    least, which is precisely backwards.
    """
    bare = P.normalise({})
    assert FE.anchor_state("match-sticks", bare) == FE.UNKNOWN
    assert FE.anchor_substitutions(
        {"topics": [{"index": 2, "experiencePlan": {"anchor": "match-sticks"}}]},
        bare) == {}


def test_a_room_with_the_object_keeps_it():
    have = _profile(materials_available={
        "value": "textbook, notebooks, match-sticks, bottle caps",
        "provenance": "verified", "origin": "teacher"})
    assert FE.anchor_state("match-sticks", have) == FE.AVAILABLE
    assert FE.anchor_substitutions(
        {"topics": [{"index": 2, "experiencePlan": {"anchor": "match-sticks"}}]},
        have) == {}


def test_nothing_to_stand_in_with_is_reported_not_invented():
    """No paper and no board: say so rather than name a third thing."""
    nothing = P.normalise({"materials_available": {
        "value": "textbook", "provenance": "verified", "origin": "admin"}})
    out = FE.anchor_substitutions(
        {"topics": [{"index": 2, "experiencePlan": {"anchor": "match-sticks"}}]},
        nothing)
    assert out["2"]["to"] is None
    assert "nothing recorded can stand in" in out["2"]["why"]


def test_the_real_contract_swaps_two_of_three():
    contract = {"topics": [
        {"index": 1, "experiencePlan": {"anchor": "cup"}},
        {"index": 2, "experiencePlan": {"anchor": "match-sticks"}},
        {"index": 3, "experiencePlan": {"anchor": "bottle caps"}}]}
    out = FE.anchor_substitutions(contract, _profile())
    assert set(out) == {"2", "3"}
    assert all(out[k]["to"] for k in out)


@pytest.mark.parametrize("anchor", ["cap", "a cup of water", "cupboard"])
def test_word_boundaries_do_not_confuse_cup_and_cap(anchor):
    role = FE._role_of(anchor)
    assert role in (None, "countable")
    if "cup" in anchor:
        assert role is None
