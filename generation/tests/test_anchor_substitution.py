"""The substitute IS the plan by the time Generation writes.

Applied to the spec rather than argued for in the prompt, because the prompt
argument was tried and lost: told the room had only textbook and notebooks,
Node 2 proposed "provide each pair of learners with a small bundle of actual
matchsticks".
"""
from generation.adapt import apply_anchor_substitutions


def _state():
    return {"topics": [
        {"index": 1, "experience": {"anchor": "cup", "anchorReason": "a cup is at hand"}},
        {"index": 2, "experience": {"anchor": "match-sticks",
                                    "anchorReason": "match-sticks are straight and identical"}}]}


def _plan(**swaps):
    return {"constraints": {"anchorSubstitutions": swaps}}


def test_nothing_happens_without_substitutions():
    state = _state()
    assert apply_anchor_substitutions(state, _plan()) == 0
    assert state["topics"][1]["experience"]["anchor"] == "match-sticks"
    assert apply_anchor_substitutions(state, None) == 0


def test_the_named_topic_is_rewritten_and_others_are_left():
    state = _state()
    n = apply_anchor_substitutions(state, _plan(**{
        "2": {"from": "match-sticks", "to": "strips torn from a sheet of paper",
              "why": "match-sticks is not among this room's recorded materials"}}))
    assert n == 1
    assert state["topics"][0]["experience"]["anchor"] == "cup"
    assert state["topics"][1]["experience"]["anchor"] == "strips torn from a sheet of paper"


def test_the_reason_is_rewritten_too():
    """Left alone it argues for an object that is no longer there.

    "match-sticks are straight and identical" printed beside paper strips reads
    as an editing mistake, and a teacher would be right to distrust the rest of
    the sheet after finding one.
    """
    state = _state()
    apply_anchor_substitutions(state, _plan(**{
        "2": {"from": "match-sticks", "to": "paper strips", "why": "no match-sticks recorded"}}))
    reason = state["topics"][1]["experience"]["anchorReason"]
    assert "match-sticks are straight and identical" not in reason
    assert "Keep the thinking" in reason
    assert state["topics"][1]["experience"]["anchorSubstitutedFrom"] == "match-sticks"


def test_a_substitution_with_no_replacement_is_not_applied():
    """`to: None` means the room cannot run the period — do not invent one."""
    state = _state()
    n = apply_anchor_substitutions(state, _plan(**{
        "2": {"from": "match-sticks", "to": None, "why": "nothing can stand in"}}))
    assert n == 0
    assert state["topics"][1]["experience"]["anchor"] == "match-sticks"
