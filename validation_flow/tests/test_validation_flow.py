"""One guard per way Node 3 can pass something it should not.

The failure mode this node has that the other two do not: it is the last thing
between a sheet and a classroom, and every way it goes wrong looks like good
news. A check that cannot run, a gate that was skipped, a fingerprint that
verifies against itself — each of those produces a clean verdict on material
nobody actually checked.

So most of these are tests that something is REFUSED or DOWNGRADED, and the few
that assert a pass are there because a validator that fails everything is as
useless as one that passes everything.

Deliberately free of network and database. The two model calls are stubbed.

    python -m validation_flow.tests.test_validation_flow        # standalone
    pytest validation_flow/tests/test_validation_flow.py        # or under pytest
"""
import asyncio
import sys
from pathlib import Path

# The repo root: this file is <package>/tests/<name>.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from prep_flow import contract as contract_module
from validation_flow import adapter, integrity, verdict as verdict_module
from validation_flow.graph import repair_brief, run_validation, shippable
from validation_flow.state import DEFAULT_CONFIG


# ── fixtures ─────────────────────────────────────────────────────────────────

def _contract(**over):
    """A one-topic contract, built through the real builder so its fingerprints
    are the real ones — a fixture with hand-written digests would verify against
    nothing."""
    spec = {
        "index": 1, "topic": "Views of objects", "subtopic": "",
        "page_start": 10, "page_end": 12, "anchor_kind": "heading",
        "excerpt": "<!-- page 10 -->A cup looks different from above.",
        "figures": [], "moves": [], "moves_source": "none",
        "knowledge": {"concepts": ["view", "shape"], "competencies": ["geo.1"],
                      "vocabulary": ["above"], "contexts": [],
                      "learning_outcomes": [], "bloom_level": "understand",
                      "difficulty": 2},
        "canonical_names": {"concepts": ["view", "shape"]},
        "reasoning": {
            "strand": "shapes", "anchors": ["cup", "bottle"],
            "gained": "can say what changes when you move round an object",
            "bridgesTo": "to drawing the view", "assumes": None,
            "misconceptions": ["the object changes shape"],
            "masteryTarget": "decide which view a drawing was made from",
            "supplied": ["a cup seen from two sides"],
            "missing": [{"kind": "contrast", "missing": "no object that looks the "
                                                        "same from every side"}],
        },
    }
    state = {
        "run_id": "r", "thread_id": "t", "grade": "3", "subject": "Maths",
        "chapter_title": "Shapes", "chapter_number": 2,
        "page_start": 10, "page_end": 12, "chapter_arc": "from seeing to drawing",
        "teacher_settings": {"duration": 30, "classSize": 40, "language": "English"},
        "topics": [spec],
        "reasoning": {"prerequisites": ["can hold an object"],
                      "misconceptions": [{"belief": "the object changes shape",
                                          "topics": [1]}],
                      "anchors": [{"strand": "shapes", "objects": ["cup", "bottle"]}],
                      "chain": {1: spec["reasoning"]}},
        "experience": {1: {"trajectory": ["hold it", "move round it", "compare"],
                           "conceptualJump": "the object did not change",
                           "anchor": "cup", "anchorReason": "its rim changes",
                           "studentAction": "walk round a cup",
                           "inference": "same object, different view",
                           "gapKind": "contrast", "gap": "no same-from-every-side object",
                           "gapCloser": "a ball beside the cup"}},
        "plans": {1: {"minutes": {"refresher": 3, "concept": 6, "realLife": 4,
                                  "challenge": 8, "levelSet": 4, "explore": 5},
                      "emphasis": {"heaviest": "challenge", "lightest": "refresher"},
                      "newVocabulary": ["above"],
                      "exploreHook": {"question": "which side did I draw from?"}}},
        "selections": {1: {"activity": {"id": "a1", "name": "Shape Walk",
                                        "source": "library"},
                           "context": {"name": "classroom"}}},
        "metrics": {}, "errors": [],
    }
    state.update(over)
    return contract_module.build_chapter_contract(state)


def _bullets(*pairs):
    return [{"text": t, "detail": d} for t, d in pairs]


def _sheet(**over):
    material = {
        "_meta": {"revision": 0},
        "objective": "Decide which view a drawing of a cup was made from.",
        "previousTopicRefresher": {"previousTopic": "", "recap": []},
        "concept": _bullets(
            ("A cup from above", "The rim is a circle from above (Page 10)."),
            ("A cup from the side", "From the side the rim is a line (Page 10)."),
            ("Same cup", "Confirm the cup never changed, only the view did.")),
        "realLife": {"points": _bullets(
            ("Plates", "A plate looks round from above and thin from the side."),
            ("A bucket", "Ask what the top of a bucket looks like from above."),
            ("From a chair", "What would the desk look like from up there?"))},
        "challenge": {"activity": "Shape Walk", "points": _bullets(
            ("Walk it", "Pairs walk round the cup and stop at two places."),
            ("Draw both", "Each partner draws the cup from where they stopped."),
            ("Compare", "Partners compare drawings and say what changed."))},
        "levelSet": {"points": _bullets(
            ("Which view", "Hold up a drawing and ask which side it was made from."),
            ("Not the cup", "Show a ball: it looks the same from every side."),
            ("Watch", "Watch for children who say the object changed shape."))},
        "explore": {"points": _bullets(
            ("From the ceiling", "What would this room look like from above?"),
            ("Draw one thing", "Draw one thing at home from above."),
            ("Tomorrow", "Which side did I draw from?")),
            "handoff": {"scene": "a cup on the desk", "object": "cup",
                        "question": "which side did I draw from?"}},
        "sectionWatch": {s: {"text": "watch", "detail": "Watch for the wrong answer."}
                         for s in ("refresher", "concept", "realLife", "challenge",
                                   "levelSet", "explore")},
        "timings": {"refresher": 3, "concept": 6, "realLife": 4, "challenge": 8,
                    "levelSet": 4, "explore": 5},
        "materialsUsed": ["a cup"],
        "pagesCited": [10],
    }
    material.update(over)
    return material


_PASSING_LEARNER = {"answers": {
    name: {"question": "q", "childWouldSay": "a", "verdict": "confident"}
    for name in ("understanding", "application", "misconception", "forward")}}


def _stub(learner_answers=None):
    async def call(prompt, *, label="", **kwargs):
        if label == "consistency":
            return {"verdict": "consistent", "chapterNote": "", "findings": []}
        if label.startswith("learner"):
            return learner_answers or _PASSING_LEARNER
        return {}
    return call


def _judge(contract=None, materials=None, **kwargs):
    """Run the node with both model calls stubbed."""
    import validation_flow.checks as checks
    import validation_flow.learner as learner

    stub = _stub(kwargs.pop("learner_answers", None))
    originals = (checks.call_json, learner.call_json)
    checks.call_json = learner.call_json = stub
    try:
        return asyncio.run(run_validation(
            contract=contract if contract is not None else _contract(),
            materials=materials if materials is not None else {1: _sheet()},
            **kwargs))
    finally:
        checks.call_json, learner.call_json = originals


# ── the destination must not move ────────────────────────────────────────────

def test_a_moved_mastery_target_refuses_the_sheet():
    """The far end of the hook `prep_flow/contract.py` left: it fingerprints the
    five preserved fields and says Node 3 recomputes them. This is that.

    REFUSE rather than needs_review, because the two are different kinds of bad
    news. A thin Refresher is a weaker version of the right lesson; a moved
    target is a different lesson.
    """
    original = _contract()
    tampered = dict(original)
    tampered["topics"] = [{**original["topics"][0],
                           "academic": {**original["topics"][0]["academic"],
                                        "masteryTarget": "name two views of a cup"}}]

    state = _judge(contract=tampered)
    document = state["verdict"]
    assert document["verdict"] == verdict_module.REFUSE
    assert document["metrics"]["targetPreserved"] is False
    assert shippable(state) == []


def test_the_fingerprint_is_recomputed_not_taken_on_trust():
    """The bug this test was written for.

    The first `verify()` read `returned["integrity"]` — the fingerprints the
    returned document asserts about ITSELF. A node that edited a mastery target
    and left the integrity block alone walked straight through, which is
    precisely the one thing the fingerprint exists to catch. Here the integrity
    block is deliberately left intact and only the field is changed, which is
    exactly the shape of that bypass.
    """
    original = _contract()
    tampered = dict(original)
    tampered["topics"] = [{**original["topics"][0],
                           "academic": {**original["topics"][0]["academic"],
                                        "masteryTarget": "something else entirely"}}]
    tampered["integrity"] = original["integrity"]      # untouched, as a bypass would leave it

    result = contract_module.verify(original, tampered)
    assert result["recomputed"] is True
    assert not result["preserved"] and result["changed"] == ["1"]


def test_adapting_the_route_is_not_moving_the_target():
    """The other direction, and the one a too-strict fingerprint would break.

    Node 2's whole job is to come back with a different anchor, a substituted
    activity, an added language step. If any of that tripped the check, the gate
    would refuse every successful adaptation and be switched off.
    """
    original = _contract()
    row = original["topics"][0]
    adapted = dict(original)
    adapted["topics"] = [{
        **row,
        "experiencePlan": {**row["experiencePlan"], "anchor": "a steel tumbler"},
        "selectedActivity": {**row["selectedActivity"], "name": "Tumbler Walk"},
        "lessonPlan": {**row["lessonPlan"], "newVocabulary": ["above", "view"]},
    }]
    assert contract_module.verify(original, adapted)["preserved"]


# ── a check that could not run must never read as a pass ─────────────────────

def test_a_sheet_judged_without_the_textbook_says_so():
    """Grounding is the check that asks whether the sheet stayed inside the book,
    and a grounding check with no book passes anything fluent.

    Two things have to hold: the finding is raised, and `coverage` records it —
    so a `ship` reached without the book can never be mistaken for one that
    survived it.
    """
    state = _judge()                                   # no chapter_text
    document = state["verdict"]
    assert document["coverage"]["groundingChecked"] is False
    findings = [f for row in document["topics"] for f in row["findings"]]
    assert any(f["check"] == "grounding" and "no textbook text" in f["message"]
               for f in findings)


def test_a_sheet_the_learner_gate_never_saw_cannot_ship():
    """`--no-simulate` is legitimate and cheap. It is not a way to get a `ship`.

    The design's claim is that material should not be published until it has been
    shown to teach, and `ship` is what publishing reads.
    """
    state = _judge(config={"simulate_learner": False}, chapter_text="<!-- page 10 -->x")
    document = state["verdict"]
    assert document["coverage"]["learnerGateRan"] is False
    assert all(row["verdict"] != verdict_module.SHIP for row in document["topics"])


def test_a_sheet_the_learner_gate_failed_cannot_ship():
    failing = {"answers": {
        "understanding": {"question": "q", "childWouldSay": "a", "verdict": "confident"},
        "application": {"question": "q", "childWouldSay": "I don't know",
                        "verdict": "cannot"},
        "misconception": {"question": "q", "childWouldSay": "a", "verdict": "confident"},
        "forward": {"question": "q", "childWouldSay": "a", "verdict": "confident"},
    }}
    state = _judge(learner_answers=failing)
    row = state["verdict"]["topics"][0]
    assert row["learner"]["passed"] is False
    assert row["verdict"] == verdict_module.NEEDS_REVIEW
    assert 1 in state["verdict"]["repairTargets"]


# ── the pipeline's own promises ──────────────────────────────────────────────

def test_an_adaptation_the_sheet_shows_no_trace_of_is_reported():
    """§18's "Generation ignores Node 2". An accepted, gated, feasible adaptation
    that simply is not there means the contextual layer ran, cost a call, and
    changed nothing."""
    plan = {"adaptations": [{
        "topicIndex": 1, "factor": "language", "type": "modify", "purpose": "access",
        "section": "concept",
        "change": "Have partners say in Marathi what changed before the English term.",
        "reason": "home language differs", "supports": ["view"],
        "evidence": ["home_languages"], "dataSource": "verified",
        "confidence": 0.9, "accepted": True, "rejectedBecause": []}],
        "preserve": [], "lowersStandard": False}

    state = _judge(plan=plan)
    findings = [f for row in state["verdict"]["topics"] for f in row["findings"]]
    assert any(f["check"] == "context_adoption" for f in findings)
    assert state["verdict"]["metrics"]["contextAdoptionConfirmed"] == 0.0


def test_adoption_is_measured_on_what_the_adaptation_added():
    """The refinement a test forced.

    The first version subtracted nothing and scored a Marathi language step as
    adopted on the strength of "partners", "changed" and "view" — words any sheet
    about views of objects contains, adaptation or no adaptation. The vocabulary
    the topic was going to contain anyway is subtracted first, so what is
    searched for is the adaptation's own contribution.
    """
    row = _contract()["topics"][0]
    expected = integrity._expected_vocabulary(row)
    # Stemmed, and `clauses.roots` drops words of three letters or fewer — so the
    # assertion is on the roots that survive, not on the surface words. ("cup" is
    # the anchor and is dropped by that length rule; it is short enough that no
    # adaptation clears the three-root bar on it alone.)
    assert {"view", "object", "shap", "draw"} <= expected, \
        "the concepts, the target and the trajectory are words the sheet would " \
        "contain regardless of any adaptation"
    assert "marathi" not in expected, "and a genuinely new word is not"

    # The point, end to end: the same shape of adaptation is unconfirmable when
    # its only words are the lesson's own, and confirmable when it adds one.
    generic = integrity._content(
        "have partners compare the object and say what changed") - expected
    specific = integrity._content(
        "have partners say in Marathi what changed") - expected
    assert len(generic) < integrity._ADOPTION_ROOTS, \
        "an adaptation phrased entirely in the lesson's own words leaves nothing "\
        "to search the sheet for"
    assert "marathi" in specific


def test_no_context_plan_means_no_accusation():
    """A chapter generated without a plan has not ignored one. An adoption rate
    of 0% there would be an accusation about a run that never received the thing
    it is accused of dropping."""
    state = _judge(plan=None)
    assert state["verdict"]["metrics"]["contextAdoptionConfirmed"] is None
    findings = [f for row in state["verdict"]["topics"] for f in row["findings"]]
    assert not any(f["check"] == "context_adoption" for f in findings)


def test_a_refused_context_plan_reaching_material_is_blocking():
    """Node 2 gates this itself — `apply()` returns None unless the gate passed.
    This is the same question asked from the other side, because a guard nobody
    checks from outside survives being accidentally removed."""
    plan = {"adaptations": [], "preserve": [], "lowersStandard": True}
    state = _judge(plan=plan)
    document = state["verdict"]
    assert document["verdict"] == verdict_module.REFUSE
    assert any(f["check"] == "plan_provenance" and f["severity"] == "blocking"
               for f in document["chapterFindings"])


# ── the adapter ──────────────────────────────────────────────────────────────

def test_the_adapter_hands_the_checks_what_they_were_written_against():
    """`checks.py` and `learner.py` came back from `_deferred/` unchanged. That is
    only safe if the adapter reconstitutes every field they read — a missing one
    does not raise, it makes a check quietly measure nothing."""
    document = _contract()
    state = adapter.to_state(document, materials={1: _sheet()},
                             sources={1: "<!-- page 10 -->A cup."})
    spec = state["topics"][0]

    assert spec["excerpt"], "the source is what the grounding checks read"
    assert spec["knowledge"]["concepts"] == ["view", "shape"]
    assert spec["reasoning"]["masteryTarget"]
    assert spec["reasoning"]["anchors"] == ["cup", "bottle"]
    assert spec["reasoning"]["misconceptions"] == ["the object changes shape"]
    assert state["plans"][1]["minutes"]["challenge"] == 8
    assert state["selections"][1]["activity"]["name"] == "Shape Walk"
    assert state["reasoning"]["chain"][1]["gained"]
    assert state["experience"][1]["derived"] is True, \
        "a plan with a trajectory is a real plan, not the fallback"


def test_index_keys_survive_a_json_round_trip():
    """The contract reaches this node through a file, a database column or an
    HTTP response, and JSON has no integer keys. A caller handing back what it
    was given would otherwise index every map with a string and find nothing,
    silently, on every topic."""
    state = adapter.to_state(_contract(), materials={"1": _sheet()},
                             sources={"1": "text"})
    assert 1 in state["materials"] and state["topics"][0]["excerpt"] == "text"


# ── what it hands on ─────────────────────────────────────────────────────────

def test_the_repair_brief_carries_the_reason_not_just_the_section():
    """A repair prompt told only WHICH sections to rewrite rewrites them to the
    same standard that failed. The finding text is the only thing in the brief
    that says what was actually wrong."""
    state = _judge(materials={1: _sheet(concept=[])})
    brief = repair_brief(state)
    assert brief["topics"], "a sheet with no Concept must be a repair target"
    assert brief["topics"][0]["why"], "and the brief must say why"
    assert brief["contractFingerprint"], \
        "and name the contract it was judged against, so a rewrite cannot be "\
        "applied to a different one"


def test_a_chapter_is_not_failed_by_its_worst_sheet():
    """Thirty-four clean sheets and six flagged ones is a usable chapter with a
    to-do list. `shippable()` returns a LIST for that reason — a caller that
    asked "may I ship this?" and got False would withhold all forty."""
    document = _contract()
    row = document["topics"][0]
    document["topics"] = [row, {**row, "index": 2}]
    document["integrity"]["topicFingerprints"]["2"] = row["fingerprint"]

    state = _judge(contract=document,
                   materials={1: _sheet(), 2: _sheet(concept=[])},
                   chapter_text="<!-- page 10 -->A cup looks different from above.")
    publishable = shippable(state)
    assert 2 not in publishable
    assert state["verdict"]["verdict"] == verdict_module.NEEDS_REVIEW


def test_the_node_does_not_repair():
    """It names the sections and stops. A validator that also rewrites is a
    validator marking its own work — and the statelessness that buys is what
    makes a verdict reproducible from a logged contract and a logged sheet."""
    from validation_flow import graph
    assert not hasattr(graph, "repair_node")
    assert "repair" not in graph.build_validation_graph().get_graph().nodes


def test_the_learner_gate_is_on_by_default():
    assert DEFAULT_CONFIG["simulate_learner"] is True


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
