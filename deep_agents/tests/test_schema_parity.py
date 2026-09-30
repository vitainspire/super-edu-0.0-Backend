"""The integration contract: agent output must be readable by the existing normalisers.

THIS IS THE TEST THAT MATTERS MOST IN THIS PACKAGE. `deep_agents/bridge.py`
claims that an agent's structured answer is a drop-in for the raw dict the
`call_json` it replaced returned - and that claim is what lets the pipeline keep
its chain gate, its anchor snap-back, its confidence clamp and its equity gate
unchanged.

A renamed field would break that claim silently. `_normalise` reads with
`.get()` throughout, so a schema that says `bridges_to` where the normaliser
reads `bridgesTo` does not raise; it produces a chain where every entry has an
empty bridge, the gate reports every topic as defective, and the failure looks
like a bad model rather than a bad key.

So each test below round-trips a schema instance through the real normaliser and
asserts the values survived.
"""
import pytest

from deep_agents import schemas


TOPICS = [
    {"index": 1, "topic": "Numbers to 999", "subtopic": "reading"},
    {"index": 2, "topic": "Place value", "subtopic": "hundreds tens ones"},
    {"index": 3, "topic": "Comparing", "subtopic": "greater and smaller"},
]


def _curriculum_answer() -> schemas.CurriculumReasoning:
    return schemas.CurriculumReasoning(
        prerequisites=["can count to 100 aloud", "can say which pile has more"],
        anchors=[schemas.AnchorGroup(strand="number",
                                     objects=["bottle caps", "seeds", "sticks"])],
        misconceptions=[schemas.Misconception(
            belief="the 2 in 25 is just a two", topics=[2])],
        chain=[
            schemas.ChainEntry(index=1, strand="number",
                               gained="can read a three-digit number aloud",
                               bridgesTo="a written number can be said", assumes=None),
            schemas.ChainEntry(index=2, strand="number",
                               gained="can say what each digit is worth",
                               bridgesTo="digits carry different values",
                               assumes="a written number can be said"),
            schemas.ChainEntry(index=3, strand="number",
                               gained="can decide which of two numbers is bigger",
                               bridgesTo="", assumes="digits carry different values"),
        ],
    )


def test_curriculum_schema_survives_the_real_normaliser():
    from prep_flow.agents.reasoning import _normalise

    answer = _curriculum_answer()
    raw = {
        "prerequisites": list(answer.prerequisites),
        "anchors": [g.model_dump() for g in answer.anchors],
        "misconceptions": [m.model_dump() for m in answer.misconceptions],
        "chain": [c.model_dump() for c in answer.chain],
    }
    out = _normalise(raw, TOPICS)

    assert out["prerequisites"] == ["can count to 100 aloud",
                                    "can say which pile has more"]
    assert out["misconceptions"][0]["belief"] == "the 2 in 25 is just a two"
    assert out["misconceptions"][0]["topics"] == [2]

    # The anchor pool reached the strand the chain actually uses. A mismatch here
    # would fold every object into "main" and leave the real strand with none.
    pools = {g["strand"]: g["objects"] for g in out["anchors"]}
    assert "bottle caps" in pools["number"]

    chain = out["chain"]
    assert set(chain) == {1, 2, 3}
    assert chain[1]["assumes"] is None, "topic 1's floor is the prerequisites"
    assert chain[2]["gained"] == "can say what each digit is worth"
    # The seam: T1's bridge and T2's assumption were authored to match, and the
    # normaliser must not have dropped either side of it.
    assert chain[1]["bridgesTo"] == chain[2]["assumes"]


def test_curriculum_chain_passes_the_gate_it_will_actually_face():
    """A well-formed answer must survive `chain_defects`, which is the gate that
    decides whether reasoning is cached for every future run of this book."""
    from prep_flow.agents.reasoning import _normalise, chain_defects

    answer = _curriculum_answer()
    raw = {"prerequisites": list(answer.prerequisites),
           "anchors": [g.model_dump() for g in answer.anchors],
           "misconceptions": [m.model_dump() for m in answer.misconceptions],
           "chain": [c.model_dump() for c in answer.chain]}
    out = _normalise(raw, TOPICS)

    assert chain_defects(out["chain"], [1, 2, 3]) == []


def test_chain_slip_is_still_caught_through_the_schema():
    """The schema must not accidentally launder the failure the gate exists for.

    A chain whose `gained` restates its own `assumes` has taught nothing. If a
    Pydantic model could make that answer look well-formed, the agent path would
    be strictly worse than the call_json path it replaces.
    """
    from prep_flow.agents.reasoning import _normalise, chain_defects

    slipped = [
        schemas.ChainEntry(index=1, gained="can name a shape",
                           bridgesTo="shapes have names", assumes=None),
        schemas.ChainEntry(index=2, gained="shapes have names",
                           bridgesTo="shapes recur", assumes="shapes have names"),
        schemas.ChainEntry(index=3, gained="shapes recur",
                           bridgesTo="", assumes="shapes recur"),
    ]
    out = _normalise({"chain": [c.model_dump() for c in slipped]}, TOPICS)
    assert chain_defects(out["chain"], [1, 2, 3]), \
        "a standing-still chain must still be reported as defective"


def test_experience_schema_survives_the_real_normaliser():
    from prep_flow.agents.experience import _normalise

    plan = schemas.ExperiencePlan(
        index=2, entryPoint="a handful of bottle caps on the desk",
        trajectory=["hold ten caps", "bundle them", "count the bundles",
                    "read the number", "say what each digit meant"],
        conceptualJump="the bundle is one thing and ten things at once",
        scaffolding=["count the bundle aloud together"],
        anchor="bottle caps", anchorReason="there are hundreds of them",
        studentAction="each row bundles ten caps and holds them up",
        observation="who counts the bundle as one",
        inference="can say what each digit is worth",
        evidence="asks a child to point at the tens",
        gap="no two numbers with the same digits in a different order",
        gapKind="contrast", gapCloser="write 25 and 52 and bundle both",
        transferTask="which is more, 108 or 81?",
    )
    spec = {"index": 2, "reasoning": {
        "gained": "can say what each digit is worth",
        "masteryTarget": "place decides value, not the digit's size",
        "anchors": ["bottle caps", "seeds"],
        "misconceptions": ["the 2 in 25 is just a two"],
        "bridgesTo": "digits carry different values",
    }}
    out = _normalise(plan.model_dump(), spec, {})

    assert out["index"] == 2
    assert out["anchor"] == "bottle caps", "an in-pool anchor must not be snapped"
    assert out["conceptualJump"].startswith("the bundle is one thing")
    assert out["trajectory"][0] == "hold ten caps"
    # `inference` is this period's gain, not the chapter's mastery target. The
    # normaliser corrects the confusion; a correct answer must pass through it
    # untouched.
    assert out["inference"] == "can say what each digit is worth"
    assert out["aimedAtMastery"] is False
    # Gap and closer are paired: an unpaired one is dropped, so both must survive.
    assert out["gap"] and out["gapCloser"]
    assert out["gapKind"] == "contrast"
    # Carried from reasoning, never regenerated.
    assert out["masteryTarget"] == "place decides value, not the digit's size"
    assert out["forwardBridge"] == "digits carry different values"


def test_out_of_pool_anchor_is_still_snapped_back():
    """The pool is what the room has. An agent reaching past it has invented a
    prop, and the normaliser's correction must still fire on schema output."""
    from prep_flow.agents.experience import _normalise

    plan = schemas.ExperiencePlan(index=1, anchor="interactive whiteboard",
                                  inference="can count to ten")
    spec = {"index": 1, "reasoning": {"gained": "can count to ten",
                                      "anchors": ["bottle caps", "seeds"]}}
    out = _normalise(plan.model_dump(), spec, {})
    assert out["anchor"] in {"bottle caps", "seeds"}


def test_context_schema_survives_plan_normalise():
    from context_flow import plan as plan_module

    answer = schemas.ContextReinforcement(
        adaptations=[schemas.Adaptation(
            topicIndex=7, factor="language_medium", type="modify",
            purpose="access", section="refresher",
            change="Say the word for 'bundle' in Telugu first, then in English, "
                   "while holding the bundle.",
            reason="Home language is Telugu and the medium is English.",
            supports=["mastery_target", "access"],
            evidence=["home_languages", "medium_of_instruction"],
            data_source="verified", confidence=0.91)],
        preserve=["mastery_target", "required_concepts"],
        lowers_standard=False, note="one language bridge",
    )
    raw = {"adaptations": [a.model_dump() for a in answer.adaptations],
           "preserve": list(answer.preserve),
           "lowers_standard": answer.lowers_standard, "note": answer.note}
    out = plan_module.normalise(raw, topic_indexes=[7, 8, 9])

    got = out["adaptations"][0]
    assert got["topicIndex"] == 7
    assert got["factor"] == "language_medium"
    assert got["type"] in plan_module.TYPES and got["type"] == "modify"
    assert got["purpose"] in plan_module.PURPOSES
    assert got["section"] == "refresher"
    # `evidence` is the field the gate uses to go and look at the profile rather
    # than take the model's word for its own provenance. Losing it would reduce
    # the provenance check to self-assessment.
    assert got["evidence"] == ["home_languages", "medium_of_instruction"]
    assert got["dataSource"] == "verified"
    assert got["confidence"] == pytest.approx(0.91)
    # Set by the gate, never by the model.
    assert got["accepted"] is None
    assert out["lowersStandard"] is False


def test_lowers_standard_defaults_to_stopping_the_plan():
    """An omitted honesty flag must stop a plan, not ship it. The schema default
    and the normaliser default have to agree on that, in that direction."""
    from context_flow import plan as plan_module

    assert schemas.ContextReinforcement().lowers_standard is True
    assert plan_module.normalise({}, topic_indexes=[1])["lowersStandard"] is True


def test_section_and_type_vocabularies_match_the_gate():
    """The schema's Literals are the model's menu; the plan module's tuples are
    what the gate accepts. They are written in two files and must not drift."""
    import typing

    from context_flow import plan as plan_module

    def literals(field: str) -> set:
        return set(typing.get_args(
            schemas.Adaptation.model_fields[field].annotation))

    assert literals("section") == set(plan_module.SECTIONS)
    assert literals("type") == set(plan_module.TYPES)
    assert literals("purpose") == set(plan_module.PURPOSES)
