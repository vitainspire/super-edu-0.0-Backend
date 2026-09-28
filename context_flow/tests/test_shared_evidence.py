"""Ownership of a field and permission to cite it are different questions.

Every case below is from one run: Class 3 Maths chapter 2, against a real
school profile. Nine adaptations were proposed and six refused, and all six
were refused for citing a recorded field their factor did not own. None was
refused for bad reasoning — which is what made the routing, not the reasoning,
the thing to fix.
"""
from context_flow import factors


def test_a_factor_may_always_cite_what_it_owns():
    for f in factors.FACTORS:
        for field in f.evidence:
            assert factors.may_cite(f.id, field), f"{f.id} cannot cite own {field}"


def test_participation_may_cite_the_engagement_record_it_does_not_own():
    # The refused proposal: pair-based exploration, because pair work has drawn
    # this class in before. Owned by factor 18, operationally needed by 8.
    assert "engagement_observations" not in factors.BY_ID["participation"].evidence
    assert factors.may_cite("participation", "engagement_observations")


def test_resources_may_cite_grouping_constraints():
    # The refused proposal: hand material out by pair, because the rows are fixed.
    assert factors.may_cite("resources", "grouping_constraints")


def test_cognitive_load_may_cite_the_ability_spread():
    for field in ("class_ability_spread", "class_readiness_note",
                  "engagement_observations"):
        assert factors.may_cite("cognitive_load", field), field


def test_ownership_is_still_asymmetric():
    """Sharing is not a free-for-all — that would delete the tier system.

    Only factor 18 owns the engagement record and may draw a conclusion about
    how this class engages as a trait. Factors that merely USE it are not
    thereby licensed to interpret it, and factors with no business in it are
    still refused.
    """
    assert not factors.may_cite("technology", "engagement_observations")
    assert not factors.may_cite("language", "attendance_history")
    assert not factors.may_cite("socioeconomic", "local_markets")


def test_no_shared_field_is_invented():
    assert not set(factors.SHARED_EVIDENCE) - set(factors.ALL_EVIDENCE_FIELDS)


def test_no_shared_consumer_is_an_unknown_factor():
    ids = {f.id for f in factors.FACTORS}
    named = {c for cs in factors.SHARED_EVIDENCE.values() for c in cs}
    assert not named - ids


def test_gate_factors_are_never_given_citation_rights():
    # Equity and safety check the finished plan; they do not propose, so a
    # citation permission for either would be permission for nothing.
    for gate_id in factors.GATE_FACTORS:
        assert not factors.citable_by(gate_id)
