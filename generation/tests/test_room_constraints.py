"""What the room cannot do reaches Generation as a rule, not as prose.

The failure this prevents, verbatim from a real run: handed a school recording
"Basic textbook and notebooks; limited supplementary materials", Generation
wrote "Distribute matchsticks to each pair" and listed matchsticks as a
required material — on a topic where Node 1 had already recorded "no actual
matchsticks or similar manipulatives to build with".
"""
from generation.reinforcement import block, constraints_block


def _plan(**limits):
    return {"adaptations": [], "constraints": limits}


def test_limits_are_rendered_even_with_no_adaptations():
    out = block(_plan(noRequiredHomework=True), 1)
    assert "Work does NOT go home" in out


def test_no_constraints_still_means_no_block():
    assert block({"adaptations": []}, 1) == ""


def test_absent_equipment_is_named_and_forbidden():
    out = constraints_block(_plan(forbidden=["a projector", "internet access"]))
    assert "does NOT have: a projector, internet access" in out
    assert "not even as an alternative" in out


def test_recorded_materials_become_a_closed_list():
    out = constraints_block(_plan(materials=["board", "paper", "textbook"],
                                  materialsRecorded=True))
    assert "board, paper, textbook" in out
    assert "Do NOT require any physical material outside that list" in out
    # The substitution has to be spelled out, or the model drops the activity
    # rather than swapping the object.
    assert "KEEP THE THINKING AND SWAP THE" in out


def test_unrecorded_materials_are_not_a_pass():
    """An unanswered question is not a yes — the matchstick bug in one line."""
    out = constraints_block(_plan(materials=[], materialsRecorded=False))
    assert "Nobody recorded what this room has" in out
    assert "Plan for a bare room" in out
    assert "Do NOT require any physical material outside" not in out


def test_homework_is_a_feasibility_fact_not_a_preference():
    out = constraints_block(_plan(noRequiredHomework=True))
    assert "including an 'optional' look at home" in out
