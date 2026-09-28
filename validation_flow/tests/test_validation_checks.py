"""The guards Node 3 brought back — one per defect the checks were caught making.

These twelve tests were written against `prep_flow/agents/validation.py` and
`simulation.py`. When the master orchestration was split into nodes those modules
were archived, and their guards were archived beside them: a guard kept away from
the code it guards is a guard nobody runs.

Node 3 revived the modules as `validation_flow/checks.py` and `learner.py`,
unchanged in substance. So nine of the twelve come back here, rewired and
otherwise untouched. Every one is a defect that shipped, was found by running the
thing and reading the output, and is cheap to reintroduce.

THREE ARE STILL IN `_deferred/tests/`, and the reason differs:

  * two test `agents/teacher_readiness.py`, removed for cost before the node
    split — one model call per sheet — and never re-implemented. Nothing measures
    whether the person running the period could run it.
  * one tests the generator's window handoff. Generation is the last stage still
    waiting to be re-homed.

Deliberately free of network and database: these run with no API key.

    python -m validation_flow.tests.test_validation_checks      # standalone
    pytest validation_flow/tests/test_validation_checks.py      # or under pytest
"""
import sys
from pathlib import Path

# The repo root: this file is <package>/tests/<name>.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from validation_flow.learner import (DIMENSIONS as LEARNER_DIMS,
                                     _history_entry, diagnosis_rows,
                                     score as learner_score)
from validation_flow.checks import (check_continuity,
                                    check_plan_fidelity,
                                    check_experience_alignment,
                                    check_pedagogy_rules)
from prep_flow.sections import SECTION_ORDER
from prep_flow import repair_surface, selection


# ── helpers ──────────────────────────────────────────────────────────────────

def _bullets(*pairs):
    return [{"text": t, "detail": d} for t, d in pairs]


def _sheet(**over):
    """A structurally complete sheet, so a test can break exactly one thing."""
    material = {
        "_meta": {"revision": 0},
        "previousTopicRefresher": {"previousTopic": "Views of objects", "recap": _bullets(
            ("Remember the cup?", "Bring back the cup the class walked around last time."),
            ("What did we see?", "Ask what changed as they moved around it."),
            ("Same cup, new view", "Confirm the cup never changed, only the view did."))},
        "concept": _bullets(("Trace it", "Place the match-box on paper and draw round it.")),
        "realLife": {"points": _bullets(("Rangoli", "Families trace bangles for patterns."))},
        "challenge": {"activity": "Shape Walk",
                      "points": _bullets(("Trace", "Trace the match-box, name the shape."))},
        "levelSet": {"points": _bullets(("Check", "Ask which is flat: a ball or a coin."))},
        "explore": {"points": _bullets(("Open it", "Open the match-box and look."))},
        "sectionWatch": {s: {"text": "watch", "detail": "Watch for the wrong answer here."}
                         for s in SECTION_ORDER},
        "timings": {s: 5 for s in SECTION_ORDER},
        "materialsUsed": ["match-box", "paper"],
    }
    material.update(over)
    return material


# ── the failures ─────────────────────────────────────────────────────────────


def test_refresher_built_from_own_pages_is_blocking():
    """A Refresher that invents a previous lesson out of THIS topic's pages.

    Measured: the topic opening a generation window scored seam 1.75 against 3.19
    for its in-window siblings, and the judge said the same thing every time —
    "invents a previous match-stick activity that was not in the provided
    PREVIOUS LESSON'S EXPLORE". Word overlap with the previous Explore alone did
    not catch it, because a fabrication drawn from the same chapter shares that
    chapter's vocabulary. The words UNIQUE to each source separate them.
    """
    previous = _sheet(explore={"points": _bullets(
        ("The cup from above", "Look down on the cup and draw what you see."))},
        previousTopicRefresher=None)
    spec = {"index": 2, "topic": "Tracing",
            "excerpt": "Take a match-box, put it on a piece of paper and draw along "
                       "the edges. Observe the shapes formed. Take a bangle and trace it."}

    honest = check_continuity(_sheet(), previous, "Views of objects",
                              is_first=False, spec=spec)
    assert not [f for f in honest if "own pages" in f["message"]], \
        "a Refresher genuinely built from the previous Explore must not be flagged"

    fabricated = _sheet(previousTopicRefresher={
        "previousTopic": "Views of objects", "recap": _bullets(
            ("Remember tracing the match-box?",
             "Last class they drew along the edges of a match-box and traced a bangle."),
            ("What shape did the bangle make?", "Recall the circle the bangle outline made."),
            ("Opening the match-box", "Remind them how the match-box looked unfolded."))})
    caught = [f for f in check_continuity(fabricated, previous, "Views of objects",
                                          is_first=False, spec=spec)
              if "own pages" in f["message"]]
    assert caught, "a Refresher built from this topic's own excerpt must be caught"
    assert caught[0]["severity"] == "blocking", \
        "fabrication must block — as an advisory it ships and only the rubric notices"

def test_first_topic_timings_are_not_a_false_advisory():
    """`_normalise_material` strips the Refresher timing for topic 1 by design.

    The timings check did not know that, so every chapter's first topic raised
    "timings cover 5 of 6" for a section the pipeline itself had removed. A
    validator that always complains is one people stop reading.
    """
    five = _sheet(timings={s: 5 for s in SECTION_ORDER if s != "refresher"})
    as_first = [f for f in check_pedagogy_rules(five, "Shape Walk", "3", None, is_first=True)
                if "timings cover" in f["message"]]
    assert not as_first, "topic 1 legitimately has no Refresher timing"

    as_middle = [f for f in check_pedagogy_rules(five, "Shape Walk", "3", None, is_first=False)
                 if "timings cover" in f["message"]]
    assert as_middle, "a middle topic missing a timing is still a real finding"

def test_a_zero_dimension_fails_whatever_the_average_says():
    """`cannot` means the sheet does not address it, and an average absorbed it.

    A real sheet scored application 0.0 — the child answered "I don't know, we
    only traced matchboxes and books and bangles" — and passed at exactly 0.750
    against a 0.750 bar. It was diagnosed, recorded, and never repaired.

    TWO MECHANISMS NOW CATCH IT, added in that order, and the test has to lift
    both to reproduce the original pass. The zero floor came first; gating
    APPLICATION came later, for the related but distinct reason that a sheet
    shown not to travel past its own example has taught the example. Either
    alone is sufficient, which is why the historical case below needs
    `gates={"application": None}` as well as `zero_fails=False` — without that
    the test would read as though the floor were the only thing standing
    between this sheet and a pass.
    """
    verdicts = {"understanding": {"verdict": "confident"},
                "application": {"verdict": "cannot"},
                "misconception": {"verdict": "confident"},
                "forward": {"verdict": "confident"}}

    historical = learner_score(verdicts, zero_fails=False,
                               gates={"application": None})
    assert historical["passed"] and historical["weighted"] == 0.75, \
        "the behaviour that shipped: 0.750 against a 0.750 bar"

    # The floor alone, with the gate still lifted.
    floored = learner_score(verdicts, gates={"application": None})
    assert not floored["passed"]
    assert floored["unaddressed"] == ["application"]
    assert "not addressed at all" in floored["reason"]

    # And as the pipeline actually runs it, both in force.
    live = learner_score(verdicts)
    assert not live["passed"] and "application" in live["failedGates"]


def test_a_zero_leaves_an_evidence_row_naming_what_caught_it():
    """A zero with no diagnosis behind it still produces a row.

    The point of the row is that a dimension nothing diagnosed is still evidence,
    and `trigger` says WHAT caught it. It reads `gate` rather than `zero` here
    because APPLICATION is gated now and the gate fires first — the sheet is
    caught either way, and which mechanism caught it is the thing the field
    exists to record.
    """
    entry = _history_entry({**learner_score(
        {"understanding": {"verdict": "confident"}, "application": {"verdict": "cannot"},
         "misconception": {"verdict": "confident"}, "forward": {"verdict": "confident"}}),
        "revision": 0, "questions": {"application": "q"}, "diagnosis": []}, 1)
    rows = diagnosis_rows([entry], {"index": 2})
    assert [r["dimension"] for r in rows] == ["application"]
    assert rows[0]["trigger"] == "gate"

    # With the gate lifted, the floor is what catches it, and the row says so.
    ungated = _history_entry({**learner_score(
        {"understanding": {"verdict": "confident"}, "application": {"verdict": "cannot"},
         "misconception": {"verdict": "confident"}, "forward": {"verdict": "confident"}},
        gates={"application": None}),
        "revision": 0, "questions": {"application": "q"}, "diagnosis": []}, 1)
    assert diagnosis_rows([ungated], {"index": 2})[0]["trigger"] == "zero"


def test_a_diagnosis_may_not_rewrite_the_whole_sheet():
    """`_merge_sections` protects untargeted sections — naming all six protects none.

    Measured: the learner gate named 1-2 sections per diagnosis; the teacher
    gate's `struggle` facet named all six, every time.
    """
    everything = list(SECTION_ORDER)
    assert repair_surface.was_clamped(everything)
    kept = repair_surface.clamp(everything)
    assert len(kept) == repair_surface.MAX_SECTIONS
    assert set(kept) < set(SECTION_ORDER), "some sections must stay protected"

    narrow = ["concept", "challenge"]
    assert not repair_surface.was_clamped(narrow)
    assert repair_surface.clamp(narrow) == narrow

    assert repair_surface.clamp([]) == everything, \
        "a structural failure names nothing and does want the whole sheet"

def test_the_best_revision_ships_not_the_last():
    """A repair that made a sheet worse used to ship anyway.

    `readiness_history=` is gone from the call: `selection.select` lost it when
    the teacher-readiness gate was removed for cost, before the node split. The
    comparison it makes — blocking findings first, then the learner score — is
    unchanged.
    """
    def material(revision):
        return {"_meta": {"revision": revision}}

    decision = selection.select(
        7, material=material(1),
        material_history={7: [{"revision": 0, "material": material(0)},
                              {"revision": 1, "material": material(1)}]},
        validation_history={7: [{"revision": 0, "blocking": 0, "advisory": 0},
                                {"revision": 1, "blocking": 0, "advisory": 0}]},
        learner_history={7: [{"revision": 0, "weighted": 0.88, "passed": True},
                             {"revision": 1, "weighted": 0.50, "passed": False}]})
    assert decision["revision"] == 0 and decision["changed"]

    structural = selection.select(
        7, material=material(1),
        material_history={7: [{"revision": 0, "material": material(0)},
                              {"revision": 1, "material": material(1)}]},
        validation_history={7: [{"revision": 0, "blocking": 0, "advisory": 0},
                                {"revision": 1, "blocking": 2, "advisory": 0}]},
        learner_history={7: [{"revision": 0, "weighted": 0.70, "passed": False},
                             {"revision": 1, "weighted": 0.95, "passed": True}]})
    assert structural["revision"] == 0, \
        "blocking findings outrank a higher score — that score was measured on holes"

def test_an_anchor_is_found_when_the_sheet_says_it_in_child_words():
    """Seven of twelve EVS sheets were accused of ignoring their own anchor.

    The anchor is written for a shelf - "family photographs" - and the bullet is
    written for a child - "hold up a photo of your own family". A substring test
    finds nothing, so the check fired on six sheets that were using the object
    exactly as planned, and buried the one sheet (T12, pencil box) that genuinely
    never touched it.
    """
    using_it = _sheet(realLife={"points": _bullets(
        ("Show your family photo",
         "Hold up a photo of your own family and name each person."))})
    plan = {"derived": True, "anchor": "family photographs"}
    assert not [f for f in check_experience_alignment(using_it, plan)
                if "no hands-on section uses it" in f["message"]], \
        "prose about the anchor in a child's words must count as using it"

    ignoring_it = _sheet(
        realLife={"points": _bullets(("Talk about home", "Ask who lives in their house."))},
        challenge={"activity": "Talk", "points": _bullets(("Say", "Say who cooks."))},
        explore={"points": _bullets(("Think", "Think about tomorrow."))})
    assert [f for f in check_experience_alignment(
        ignoring_it, {"derived": True, "anchor": "pencil box"})
        if "no hands-on section uses it" in f["message"]], \
        "a sheet that never reaches for the anchor must still be caught"

def test_correcting_a_misconception_is_not_aiming_at_it():
    """T5's plan was accused of designing the period to land the wrong idea.

    A period that TARGETS a misconception is about the same thing the
    misconception is about, so a correct inference shares its nouns by
    construction - "my family name is the ONLY name I have" against "I have a
    family name that is part of my full name" scored 0.67 on root overlap and
    tripped a 0.6 threshold. What separates them is polarity, not vocabulary: the
    wrong belief closes the door and the right one opens it.
    """
    chain = {5: {"gained": "can say their family's last name"}}
    corrected = {5: {"derived": True,
                     "inference": "I have a family name that is part of my full name",
                     "misconception": "my family name is the only name I have"}}
    assert not [f for f in check_plan_fidelity(corrected, {"chain": chain})
                if "MISCONCEPTION" in f["message"]], \
        "an inference that removes the belief's absolute is the correct target"

    restated = {5: {"derived": True,
                    "inference": "my family name is the only name I have",
                    "misconception": "my family name is the only name I have"}}
    assert [f for f in check_plan_fidelity(restated, {"chain": chain})
            if "MISCONCEPTION" in f["message"]], \
        "an inference that IS the belief must still be caught"

def test_the_gap_and_the_transfer_are_counted_not_just_requested():
    """Fields the model is told about and nobody checks become decoration.

    The anchor had exactly this problem for one chapter and it took a full run to
    notice. The gap closer and the transfer task ship with their checks attached.
    """
    plan = {"derived": True, "anchor": "match-box",
            "gap": "no case where size and structure disagree",
            "gapCloser": "one pair of families with equal members but different relatives",
            "transferTask": "a household of a mother, two children and a grandmother"}

    ignored = check_experience_alignment(_sheet(), plan)
    assert [f for f in ignored if "teaches the page as it stands" in f["message"]], \
        "a named gap closer that never reaches Concept or Challenge must be caught"
    assert [f for f in ignored if f["section"] == "levelSet"], \
        "a transfer task that never reaches Level Set must be caught"

    staged = _sheet(
        concept=_bullets(
            ("Two families, same size",
             "Show one pair of families with equal members but different relatives.")),
        levelSet={"points": _bullets(
            ("Decide and say why",
             "A household of a mother, two children and a grandmother - what is it?"))})
    assert not check_experience_alignment(staged, plan), \
        "a sheet that stages both must pass clean"


# ── runner ───────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for test in tests:
        try:
            test()
            print(f"  PASS  {test.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"  FAIL  {test.__name__}: {exc}")
        except Exception as exc:                       # noqa: BLE001
            failed += 1
            print(f"  ERROR {test.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
