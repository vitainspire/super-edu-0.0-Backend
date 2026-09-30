"""A citation counts as echoed when the change REFERS to the value, not quotes it.

Calibrated against one real chapter (Class 3 Maths ch2, `mixed_constrained`
profile): the verbatim test flagged 23 of 26 citations, the overlap test flags
5, and all five are citations the change never actually uses.
"""
from context_flow import gate, profile as P


def _profile(**fields):
    return P.normalise({k: {"value": v, "provenance": "verified", "origin": "teacher"}
                        for k, v in fields.items()})


def _check(profile, evidence, change, reason=""):
    return gate._unechoed_evidence(
        {"evidence": evidence, "change": change, "reason": reason}, profile)


def test_a_prose_field_referred_to_in_other_words_is_not_flagged():
    """The false positive that made the check unreadable.

    Nothing will ever contain this sentence verbatim, so under the old test
    every citation of a prose field warned.
    """
    prof = _profile(class_readiness_note=(
        "The class understands basic concepts but prerequisite mastery is "
        "inconsistent. New abstract concepts need explicit review."))
    assert not _check(prof, ["class_readiness_note"],
                      "Review the prerequisite concepts explicitly before the "
                      "abstract idea, since mastery is inconsistent.")


def test_the_same_words_in_a_different_order_are_an_echo():
    # `literacy_level = "Below grade level"` against a change using all three
    # words — flagged before, because the exact phrase never appeared.
    prof = _profile(literacy_level="Below grade level")
    assert not _check(prof, ["literacy_level"],
                      "Reading is below the expected level for this grade, so "
                      "keep written instructions short.")


def test_a_citation_the_change_never_uses_is_still_flagged():
    """The defect the check exists for, and it must survive recalibration."""
    prof = _profile(home_languages="Telugu, Hindi, Urdu")
    found = _check(prof, ["home_languages"],
                   "Explicitly introduce and define the new vocabulary with "
                   "visual aids and gestures.")
    assert len(found) == 1
    assert found[0]["check"] == "unechoed_evidence"


def test_naming_one_of_several_recorded_values_is_enough():
    prof = _profile(home_languages="Telugu, Hindi, Urdu")
    assert not _check(prof, ["home_languages"],
                      "Let pairs discuss the new words in Telugu first.")


def test_a_one_word_value_the_change_ignores_is_flagged():
    # `oral_language_strength = "Moderate"` cited by a change about visual aids.
    prof = _profile(oral_language_strength="Moderate")
    assert _check(prof, ["oral_language_strength"],
                  "Introduce the vocabulary with visual aids and gestures.")


def test_booleans_are_still_exempt():
    prof = _profile(has_paper=True)
    assert not _check(prof, ["has_paper"], "Use paper strips.")


def test_a_field_nobody_recorded_is_not_this_check_s_business():
    assert not _check(_profile(), ["class_size"], "With a large class, use pairs.")
