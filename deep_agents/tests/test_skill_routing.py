"""Every stage is pointed at the skills it needs, and the pointing reaches a prompt.

WHY THIS FILE EXISTS AT ALL, given that every skill is already mounted for every
agent. Mounting decides what an agent CAN read; the prompt decides what it is
TOLD to read, and only the second one is load-bearing. A stage that is never
pointed at `differentiation` does not error, does not warn, and does not produce
anything that looks wrong — it produces a lesson that is correct in every local
respect and forgets the child two years behind. `skills/README.md` names that
failure and states the requirement:

    `learning-goals` and `core-pedagogy` should be in **every** stage's prompt.
    [...] the gate itself only runs if `learning-goals` is present.

That is a requirement written in prose in a README, and prose does not fail a
build. These tests are the same requirement written so that it does.

THE LAST THREE TESTS ARE THE ONES THAT MATTER MOST, and they are the same lesson
`reinforcement/liveness.py` was built around: a table nothing renders is
indistinguishable from a table that is right. So they capture the prompt at the
moment `build_*` hands it to `create_deep_agent` and look for the real skill
names in it — the one place the answer cannot be re-derived from the thing under
test.
"""
import pytest

from deep_agents import registry, skillset
from deep_agents.tools import RunContext


def _ctx(subject: str = "Mathematics") -> RunContext:
    return RunContext(grade="3", subject=subject, chapter_title="Fractions",
                      chapter_number=2, resource_level=0,
                      topics=[{"index": i} for i in range(1, 4)])


def _prompts(ctx: RunContext, monkeypatch) -> dict[str, str]:
    """The three system prompts, exactly as they are handed to `create_deep_agent`.

    CAPTURED AT THE CALL, not read off the built agent and not re-derived from
    the template. `deepagents` closes over the prompt inside a compiled graph,
    so there is nowhere honest to read it from afterwards — and re-formatting
    the template here would pass happily on the one failure this is for: a
    `build_*` that stopped passing `skills=` and now ships a prompt containing
    the literal text `{skills}`.
    """
    captured: dict[str, str] = {}

    def spy(*args, **kwargs):
        captured[kwargs.get("name", "?")] = kwargs.get("system_prompt", "")
        return object()

    monkeypatch.setattr(registry, "create_deep_agent", spy)
    registry.build_curriculum_agent(ctx)
    registry.build_lesson_designer(ctx)
    registry.build_context_reinforcer(ctx, topic_indexes=[1, 2, 3])
    return {
        skillset.CURRICULUM: captured["curriculum-reasoner"],
        skillset.EXPERIENCE: captured["lesson-designer"],
        skillset.CONTEXT: captured["context-reinforcer"],
    }


# ── The routing table ────────────────────────────────────────────────────────

def test_every_routed_skill_exists_on_disk():
    """A prompt naming a file that is not there spends a tool call on an error
    and then carries on without it — silently, and looking exactly like a run
    that chose not to load it."""
    assert skillset.missing() == (), (
        f"routed but absent from deep_agents/skills/: {skillset.missing()}")


def test_every_skill_on_disk_is_routed_somewhere():
    """Not a safety property — an unrouted skill is still offered by the
    middleware. It is asserted because eighteen files with one orphan is
    overwhelmingly an oversight, and the orphan is never noticed by reading
    output."""
    assert skillset.unrouted() == (), (
        f"on disk but no stage is told to load it: {skillset.unrouted()}")


@pytest.mark.parametrize("stage", skillset.STAGES)
def test_every_stage_loads_the_universal_two(stage):
    """The README's wiring requirement, as an assertion.

    `learning-goals` is the only file that asks a stage to grade its own draft,
    so its absence does not fail loudly — the stage just stops asking.
    """
    routed = skillset.for_stage(stage)
    assert set(skillset.ALWAYS) <= set(routed)


@pytest.mark.parametrize("stage", skillset.STAGES)
def test_learning_goals_is_first(stage):
    """Order is not decoration here. The gate in `learning-goals` is what the
    other skills are read against, and a stage that meets it after its own
    craft skill has already read the craft skill without knowing what it is
    for."""
    assert skillset.for_stage(stage)[0] == "learning-goals"


def test_no_duplicates_in_a_stage():
    """`differentiation` is routed to all three stages and the subject skill can
    collide with nothing; a repeated line in a prompt is a wasted line."""
    for stage in skillset.STAGES:
        for subject in ("Mathematics", "English", "EVS"):
            routed = skillset.for_stage(stage, subject=subject)
            assert len(routed) == len(set(routed)), (stage, subject, routed)


# ── Subject routing ──────────────────────────────────────────────────────────

@pytest.mark.parametrize("subject,expected", [
    ("Mathematics", "early-math"), ("Maths", "early-math"), ("3EM_MAT", "early-math"),
    ("English", "literacy"), ("Telugu", "literacy"), ("Hindi", "literacy"),
    # No subject skill is the CORRECT answer for these, not a gap. Pointing an
    # EVS chapter at the misconceptions place value produces would be worse than
    # pointing it at nothing.
    ("Environmental Studies", None), ("EVS", None), ("Science", None),
    ("", None), (None, None),
])
def test_subject_skill(subject, expected):
    assert skillset.subject_skill(subject) == expected


def test_a_subject_skill_is_appended_not_substituted():
    with_subject = skillset.for_stage(skillset.EXPERIENCE, subject="Maths")
    without = skillset.for_stage(skillset.EXPERIENCE, subject="EVS")
    assert with_subject[-1] == "early-math"
    assert list(without) == list(with_subject[:-1])


# ── What each stage is and is not given ──────────────────────────────────────

def test_node_2_is_not_pointed_at_the_downstream_goals():
    """Architecture §9: Node 2 adapts the route and may not touch the
    destination. `assessment-alignment`, `self-study` and `stretch-design` own
    what the period assesses, sends home and offers a fast finisher — all
    settled before Node 2 runs — so naming them would invite it into decisions
    the equity gate would then have to refuse.

    It still gets `learning-goals`, whose check 9 (standard intact) is the part
    of that file Node 2 is answerable for.
    """
    routed = set(skillset.for_stage(skillset.CONTEXT))
    assert not routed & {"assessment-alignment", "self-study", "stretch-design"}
    assert "learning-goals" in routed


def test_the_designer_carries_every_goal():
    """The period is where all five are won or lost, so agent 2 is the one stage
    that must see a home skill for each."""
    routed = set(skillset.for_stage(skillset.EXPERIENCE))
    for goal_owner in ("differentiation", "real-world-connection", "engagement",
                       "assessment-alignment", "self-study", "stretch-design"):
        assert goal_owner in routed, goal_owner


def test_curriculum_keeps_its_grounding_skills():
    routed = set(skillset.for_stage(skillset.CURRICULUM))
    assert {"curriculum-reasoning", "grounding", "misconception-analysis"} <= routed


# ── The part that is not just a table ────────────────────────────────────────

@pytest.mark.parametrize("stage", skillset.STAGES)
def test_the_rendered_prompt_names_every_routed_skill(stage, monkeypatch):
    """The liveness question: does the routing actually reach the model?

    This is the assertion the whole file is for. Everything above tests a table;
    a table nothing renders is indistinguishable from a table that is right —
    the lesson `reinforcement/liveness.py` was built around.
    """
    ctx = _ctx("Mathematics")
    text = _prompts(ctx, monkeypatch)[stage]
    for name in skillset.for_stage(stage, subject=ctx.subject):
        assert f"`{name}`" in text, f"{stage} prompt never names {name}"
    assert "{skills}" not in text and "{gate}" not in text


@pytest.mark.parametrize("stage", skillset.STAGES)
def test_the_rendered_prompt_carries_the_gate(stage, monkeypatch):
    text = _prompts(_ctx(), monkeypatch)[stage]
    assert "ten-check gate" in text
    assert "anti-boilerplate" in text


def test_the_subject_skill_reaches_the_prompt(monkeypatch):
    """Subject routing is the one part that varies per run, so it is the part a
    hard-coded prompt would silently get wrong."""
    maths = _prompts(_ctx("Mathematics"), monkeypatch)[skillset.EXPERIENCE]
    assert "`early-math`" in maths and "`literacy`" not in maths

    telugu = _prompts(_ctx("Telugu"), monkeypatch)[skillset.EXPERIENCE]
    assert "`literacy`" in telugu and "`early-math`" not in telugu

    evs = _prompts(_ctx("Environmental Studies"), monkeypatch)[skillset.EXPERIENCE]
    assert "`early-math`" not in evs and "`literacy`" not in evs
