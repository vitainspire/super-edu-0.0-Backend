"""One guard per failure Node 1 was actually caught making.

Not a general test suite. Every case here is a defect that shipped, was found by
running the thing and reading the output, and is cheap to reintroduce — the kind
that passes every structural check and only shows up in a score nobody reads back
to its cause.

Deliberately free of network and database: these run with no API key, which is
the point. The rubric judge is the expensive instrument and it was unavailable
the day half of these were written.

THIS FILE WAS SPLIT when the master orchestration was split into nodes. Twelve of
the sixteen guards here were about generated material — the validator's checks,
the learner gate's scoring, the repair surface, the revision selection, the
generator's own window handoff — and all of those agents now belong to the
generation stage or to Node 3. Their guards went with them, to
`_deferred/prep_flow/tests/test_discovered_failures.py`, because a guard kept away from
the code it guards is a guard nobody runs.

(Two of the moved guards were ALREADY failing to import before the split: they
test `agents/teacher_readiness.py`, which was removed for cost some time ago and
which nothing has re-implemented. They are moved rather than deleted for the
same reason the rest are — the defect they describe is still reachable by
anything that re-implements that gate.)

    python -m prep_flow.tests.test_discovered_failures        # standalone
    pytest prep_flow/tests/test_discovered_failures.py        # or under pytest
"""
import sys
from pathlib import Path

# The repo root: this file is <package>/tests/<name>.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from prep_flow.agents.reasoning import _seal_threads, chain_defects
from prep_flow.agents.sequence_override import resequence_contract
from prep_flow.moves import teaching_order
from prep_flow.sections import SECTION_ORDER
from prep_flow import contract as contract_module


# ── the failures ─────────────────────────────────────────────────────────────


def test_a_book_that_explains_first_still_gets_the_canonical_order():
    """Guards the reverted `max(at, 1)` experiment, which broke exactly this."""
    def moves(*types):
        return [{"type": t, "ord": i, "page": 1, "gist": "", "fidelity": "staged"}
                for i, t in enumerate(types, 1)]

    assert teaching_order(moves("concept", "observe", "activity", "practice")) \
        == list(SECTION_ORDER)
    assert teaching_order([]) == list(SECTION_ORDER)
    hooked = teaching_order(moves("observe", "concept", "activity"))
    assert hooked.index("realLife") < hooked.index("concept"), \
        "a book that hooks before it explains still reorders"

def test_a_methodology_change_makes_snapshots_incomparable():
    """The fingerprint covers the judge PROMPT, not only the rubric text.

    Adding the textbook excerpt to the prompt moved a sheet 1.3 points on a 1-5
    scale while touching no criterion wording. Over RUBRIC alone, the before and
    after would have compared as if nothing happened.
    """
    from prep_flow import regression
    from prep_flow.evaluation import RUBRIC

    def snapshot(fingerprint):
        sheets = [{"index": i, "mean": 4.0, "scores": {k: 4 for k in RUBRIC}}
                  for i in range(1, 5)]
        return {"schema": regression.SCHEMA, "id": fingerprint,
                "rubric": {"fingerprint": fingerprint},
                "fixture": {"fingerprint": "FIX"},
                "environment": {}, "summary": {"mean": 4.0},
                "perCriterion": {k: {"mean": 4.0, "ci95": 0.0} for k in RUBRIC},
                "sheets": sheets}

    live = regression.rubric_fingerprint()
    same = regression.compare(snapshot(live), snapshot(live))
    assert same["verdict"] != "incomparable"

    changed = regression.compare(snapshot("OLD_METHOD"), snapshot(live))
    assert changed["verdict"] == "incomparable"
    assert "delta" not in changed, "a refusal must not report a number"

def test_a_chapter_where_nothing_is_ever_learned_is_caught():
    """The EVS family chapter: nine of twelve topics gained what they assumed.

    Measured on the first full EVS run. `gained` had been filled with the PREVIOUS
    topic's `bridgesTo` and `bridgesTo` with the NEXT topic's title, so the chain
    agreed with itself at every seam and promised nothing:

        T5  assumes: can say their family's last name
            gained:  can say their family's last name
            bridges: can sort families into types

    Every check that existed passed it. The seam test compares `bridgesTo` to the
    next topic's `assumes` and those matched perfectly - they were the same
    sentence. Only the topic's own two lines, read together, show it.
    """
    degenerate = {
        1: {"gained": "can name members of a given family",
            "bridgesTo": "can identify specific family members", "assumes": None},
        5: {"gained": "can say their family's last name",
            "bridgesTo": "can sort families into types",
            "assumes": "can say their family's last name"},
        6: {"gained": "can sort families into types",
            "bridgesTo": "can compare two different families",
            "assumes": "can sort families into types"},
    }
    defects = chain_defects(degenerate, [1, 5, 6])
    assert len(defects) == 2, f"both standing-still topics must be named: {defects}"
    assert all("gains nothing" in d for d in defects)
    assert not [d for d in defects if d.startswith("T1 ")], \
        "a topic that genuinely moves must not be flagged"

    honest = {1: {"gained": "can name members of a given family",
                  "bridgesTo": "each family member has a name of their own",
                  "assumes": None},
              2: {"gained": "can match a spoken name to one person in a photo",
                  "bridgesTo": "people in a photo can be told apart by name",
                  "assumes": "each family member has a name of their own"}}
    assert not chain_defects(honest, [1, 2]), "a chain that moves must pass clean"

def test_a_thread_boundary_is_sealed_on_both_sides():
    """T10 opened a new thread while claiming to stand on the old one.

    The prompt has always said a boundary is declared by nulling both sides. The
    normaliser only ever nulled `assumes` for the chapter's FIRST topic, so a
    correctly-detected seam still shipped an assumption written across it - and
    the seam check skips declared boundaries, so nothing downstream looked.
    """
    chain = {
        8: {"strand": "sensory", "gained": "can feel raised dots",
            "bridgesTo": "raised dots can be read", "assumes": None},
        9: {"strand": "sensory", "gained": "can name tools for special needs",
            "bridgesTo": None, "assumes": "raised dots can be read"},
        10: {"strand": "family", "gained": "can list what families do",
             "bridgesTo": "families can be described",
             "assumes": "can list things learned about families"},
    }
    _seal_threads(chain, [8, 9, 10])
    assert chain[10]["assumes"] is None, \
        "the first topic of a new thread stands on the chapter, not on the old thread"
    assert chain[9]["assumes"] == "raised dots can be read", \
        "sealing a boundary must not touch topics inside a thread"


# ── the contract: the node boundary itself ───────────────────────────────────
#
# These four are not defects that shipped — nothing has shipped against this
# boundary yet. They are here because the contract is the ONE thing Node 2 and
# Node 3 both depend on, and a boundary with no guard is a boundary that drifts
# silently: the first symptom is a downstream node reading a field that quietly
# stopped being written, on a run that reports success.

def _spec(index=1, **over):
    """One TopicSpec with everything the contract requires, so a test can break
    exactly one thing."""
    spec = {
        "index": index, "topic": "Views of objects", "subtopic": "",
        "page_start": 10, "page_end": 12, "anchor_kind": "heading",
        "excerpt": "<!-- page 10 -->A cup looks different from above.",
        "figures": [], "moves": [], "moves_source": "none",
        "knowledge": {"concepts": ["view", "shape"], "competencies": ["geo.1"],
                      "vocabulary": ["above"], "contexts": [], "bloom_level": "understand",
                      "difficulty": 2},
        "canonical_names": {"concepts": ["view", "shape"]},
        "reasoning": {
            "strand": "shapes", "anchors": ["cup", "bottle"],
            "gained": "can say what changes when you move round an object",
            "bridgesTo": "to drawing the view", "assumes": None,
            "misconceptions": ["the object changes shape"],
            "masteryTarget": "decide which view a drawing was made from",
            "supplied": ["a cup seen from two sides"],
            "missing": [{"kind": "contrast", "missing": "no object that looks the same from every side"}],
        },
    }
    spec.update(over)
    return spec


def _state(*specs):
    return {
        "run_id": "r", "thread_id": "t", "grade": "3", "subject": "Maths",
        "chapter_title": "Shapes", "chapter_number": 2,
        "page_start": 10, "page_end": 20, "chapter_arc": "from seeing to drawing",
        "teacher_settings": {"duration": 30, "classSize": 40, "language": "English"},
        "topics": list(specs),
        "reasoning": {"prerequisites": ["can hold an object"],
                      "misconceptions": [{"belief": "the object changes shape", "topics": [1]}],
                      "anchors": [{"strand": "shapes", "objects": ["cup", "bottle"]}],
                      "chain": {s["index"]: s["reasoning"] for s in specs}},
        "experience": {s["index"]: {
            "trajectory": ["hold it", "move round it", "compare"],
            "conceptualJump": "the object did not change, the view did",
            "anchor": "cup", "anchorReason": "it has a rim you can see change",
            "studentAction": "walk round a cup on a desk",
            "inference": "same object, different view",
            "gapKind": "contrast", "gap": "no object that looks the same from every side",
            "gapCloser": "a ball, held up beside the cup",
        } for s in specs},
        "plans": {s["index"]: {
            "minutes": {"refresher": 3, "concept": 6, "realLife": 4,
                        "challenge": 8, "levelSet": 4, "explore": 5},
            "emphasis": {"heaviest": "challenge", "lightest": "refresher"},
            "newVocabulary": ["above"],
            "exploreHook": {"question": "which side did I draw from?"},
        } for s in specs},
        "selections": {s["index"]: {
            "activity": {"id": "a1", "name": "Shape Walk", "category": "observation",
                         "source": "library", "materials": ["a cup"]},
            "context": {"name": "classroom"}, "formats": {"levelSet": "thumbs"},
            "rationale": "matched 2 competencies; best of 4",
            "candidateCount": 4,
        } for s in specs},
        "metrics": {}, "errors": [],
    }


def test_the_contract_carries_everything_node_2_was_promised():
    """Section 3.1 of the Node 2 implementation framework lists what Node 1 owes
    it. Every one of those names must survive into the document — a contract
    that merely LOOKS complete is the failure mode, because Node 2 fills a
    missing field with something plausible rather than noticing it is gone.
    """
    document = contract_module.build_chapter_contract(_state(_spec()))
    topic = document["topics"][0]

    assert topic["academic"]["masteryTarget"]
    assert topic["academic"]["requiredConcepts"] == ["view", "shape"]
    assert topic["academic"]["competencies"] == ["geo.1"]
    assert topic["knowledgeChain"]["gained"] and topic["knowledgeChain"]["bridgesTo"]
    assert topic["misconceptions"] == ["the object changes shape"]
    assert topic["experiencePlan"]["trajectory"]
    assert topic["experiencePlan"]["closesGap"]["kind"] == "contrast"
    assert topic["lessonPlan"]["minutes"]["challenge"] == 8
    assert topic["selectedActivity"]["name"] == "Shape Walk"
    assert document["chapterWide"]["prerequisites"] == ["can hold an object"]
    assert document["readiness"]["generationReady"] is True

    # The excerpt is the one thing deliberately left out — it is the largest
    # field in the state and Node 2 has no use for it. Its SIZE is kept, because
    # "this contract was built from 40 characters of book" is a real diagnosis.
    assert "excerpt" not in topic["grounding"]
    assert topic["grounding"]["excerptChars"] == len(_spec()["excerpt"])


def test_a_topic_missing_its_path_is_not_quietly_shipped_as_ready():
    """A topic with no experience plan is not one Node 2 can adapt or generation
    can write from. The honest place to say so is the handoff.

    `generationReady` is ALL topics, not most: the Refresher of T(n) is written
    from the Explore of T(n-1), so one unready topic breaks the seam on the one
    after it too.
    """
    state = _state(_spec(1), _spec(2))
    state["experience"][2] = {}
    document = contract_module.build_chapter_contract(state)

    assert document["readiness"]["topicsReady"] == 1
    assert document["readiness"]["generationReady"] is False
    assert document["readiness"]["notReady"] == [{"index": 2, "missing": ["experiencePlan"]}]


def test_a_mastery_target_that_moved_cannot_be_argued_about():
    """The deterministic half of the equity/safety gate (framework §10).

    Node 2 is meant to adapt the ROUTE. If it comes back having edited the
    destination, that is not a judgement call about whether the adaptation was
    useful — it is a hash mismatch. And the fingerprint must cover ONLY the
    preserved fields, or every successful adaptation would trip it.
    """
    document = contract_module.build_chapter_contract(_state(_spec()))

    unchanged = contract_module.verify(document, document)
    assert unchanged["preserved"] and unchanged["checked"] == 1

    # A legitimate Node 2 adaptation: a different anchor, a language bridge, a
    # substituted activity. None of it touches the destination.
    adapted = {**document, "topics": [{
        **document["topics"][0],
        "experiencePlan": {**document["topics"][0]["experiencePlan"], "anchor": "a steel tumbler"},
        "selectedActivity": {**document["topics"][0]["selectedActivity"], "name": "Tumbler Walk"},
    }]}
    adapted["integrity"] = document["integrity"]
    assert contract_module.verify(document, adapted)["preserved"], \
        "a changed route must not read as a changed target"

    # The destination itself, quietly lowered.
    lowered = contract_module.build_chapter_contract(
        _state(_spec(reasoning={**_spec()["reasoning"],
                                "masteryTarget": "name two views of a cup"})))
    verdict = contract_module.verify(document, lowered)
    assert not verdict["preserved"] and verdict["changed"] == ["1"]


def test_reordering_says_what_the_chain_will_not_support():
    """A teacher's order that breaks the chain must be reported, not absorbed.

    The reseam half of sequence override needs the written Refresher and so runs
    after generation; the half that judges the ORDER is Node 1's, because the
    chain is.
    """
    first = _spec(1)
    second = _spec(2, reasoning={**_spec()["reasoning"],
                                 "gained": "can draw the view they chose",
                                 "assumes": "can say what changes when you move round an object",
                                 "bridgesTo": ""})
    document = contract_module.build_chapter_contract(_state(first, second))

    kept = resequence_contract(document, [1, 2])
    assert kept["findings"] == [] and kept["verdicts"] == {1: "safe", 2: "safe"}

    flipped = resequence_contract(document, [2, 1])
    assert [r["index"] for r in flipped["contract"]["topics"]] == [2, 1]
    assert flipped["contract"]["canonicalOrder"] == [1, 2]
    # The original document is not mutated — the caller still wants the book's
    # order to compare against.
    assert [r["index"] for r in document["topics"]] == [1, 2]


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
