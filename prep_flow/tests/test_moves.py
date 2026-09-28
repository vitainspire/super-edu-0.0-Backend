"""Turning the book's own activity move into a Challenge name."""
from prep_flow.moves import challenge_name_from, usable_challenge_name

# ---------------------------------------------------------------------------
# Naming the Challenge when the book sets the task.
#
# Every string below was captured from a real run: Class 3 EVS chapter 4 and
# Class 5 EVS chapter 2, both on gemini-2.5-flash. They are here because the
# first version of the backstop passed all its tests and still shipped a
# seventy-word textbook monologue as a Challenge name.
# ---------------------------------------------------------------------------

def test_a_copied_book_heading_is_not_a_usable_name():
    # What the model actually wrote for Class 3 EVS ch4 T2 — the page's own
    # heading, question mark and all.
    assert not usable_challenge_name("Where do birds live?")


def test_lifted_prose_is_not_a_usable_name():
    assert not usable_challenge_name(
        "Jaya, Vijaya and Latha also have drawn a picture. Look at their "
        "picture and identify the parts that belong to different animals.")


def test_a_seventy_word_monologue_is_not_a_usable_name():
    # Class 5 EVS ch2 T2. The model titled the Challenge with the whole passage.
    assert not usable_challenge_name(
        "Imagine how Lakshmi feels: I have to jump and play to earn money. "
        "Whether I like it or not, I am forced to perform in the circus.")


def test_a_real_short_title_survives():
    for name in ("Clap Patterns", "Object View Sketch",
                 "Identify which house view is top, front or side"):
        assert usable_challenge_name(name), name


def test_a_gist_naming_the_doer_becomes_an_imperative():
    # "children identify ..." is a description of who acts; the title wants the
    # instruction. Class 3 EVS ch4 T3.
    assert challenge_name_from(
        {"gist": "children identify parts belonging to different animals "
                 "in a drawn picture"}) == "Identify parts belonging to different animals"


def test_truncation_never_leaves_a_dangling_phrase():
    name = challenge_name_from(
        {"gist": "children observe crops in an agricultural field and list "
                 "creatures helping the farmer"})
    assert not name.rstrip(".").lower().endswith((" in", " a", " an", " the",
                                                  " of", " with", " to"))


def test_the_maths_case_still_derives_as_before():
    # The chapter this backstop was built on must not move.
    assert challenge_name_from(
        {"gist": "asks child to identify which house view is top, front or "
                 "side"}) == "Identify which house view is top, front or side"
