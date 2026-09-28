"""The rules that make the agent layer safe to switch on.

Three properties, each of which the architecture states and each of which is
cheap to lose in a refactor:

  * the order of authority is enforced on the filesystem (section 6/9) - Node 2
    cannot write where the academic contract lives;
  * inactive context factors never reach the model (section 11);
  * the tools cannot be talked into reading another school's data, because the
    run's identity is bound at construction and is not an argument.
"""
import pytest

from deep_agents import permissions as perms
from deep_agents.tools import RunContext, room


def _match(rules, operation: str, path: str) -> str:
    """First matching rule wins; no match means allow. Mirrors deepagents.

    Reimplemented here rather than imported so the test asserts the SEMANTICS we
    are relying on. If `deepagents` ever changed to last-match-wins, importing
    its matcher would make this test agree with the new behaviour instead of
    catching it.
    """
    from wcmatch import glob as wcglob

    for rule in rules:
        if operation not in rule.operations:
            continue
        for pattern in rule.paths:
            if wcglob.globmatch(path, pattern, flags=wcglob.GLOBSTAR):
                return rule.mode
    return "allow"


# ── The order of authority ───────────────────────────────────────────────────

def test_curriculum_may_write_only_its_own_artifacts():
    rules = perms.curriculum_permissions()
    assert _match(rules, "write", "/workspace/curriculum/ch2.json") == "allow"
    assert _match(rules, "write", "/workspace/notes/ch2/scratch.md") == "allow"
    assert _match(rules, "write", "/workspace/reinforcement/ch2.json") == "deny"
    assert _match(rules, "write", "/workspace/lesson_design/ch2.json") == "deny"
    # The catch-all: a path nobody anticipated must be refused, not permitted.
    assert _match(rules, "write", "/somewhere/nobody/expected.json") == "deny"


def test_lesson_design_may_not_amend_the_curriculum_it_was_handed():
    """A designer that could rewrite its own inputs would make the two stages one
    stage with a longer prompt."""
    rules = perms.lesson_design_permissions()
    assert _match(rules, "write", "/workspace/lesson_design/ch2.json") == "allow"
    assert _match(rules, "write", "/workspace/curriculum/ch2.json") == "deny"
    assert _match(rules, "read", "/workspace/curriculum/ch2.json") == "allow"


def test_node_2_cannot_touch_the_academic_contract():
    """Architecture section 9, as a filesystem rule. This is the single most
    load-bearing permission in the system."""
    rules = perms.context_permissions()
    assert _match(rules, "write", "/workspace/reinforcement/w7.json") == "allow"
    assert _match(rules, "write", "/workspace/curriculum/ch2.json") == "deny"
    assert _match(rules, "write", "/workspace/lesson_design/ch2.json") == "deny"
    # It must still be able to READ what it is adapting.
    assert _match(rules, "read", "/workspace/curriculum/ch2.json") == "allow"


@pytest.mark.parametrize("builder", [
    perms.curriculum_permissions,
    perms.lesson_design_permissions,
    perms.context_permissions,
])
def test_the_library_is_readable_and_not_writable(builder):
    rules = builder()
    assert _match(rules, "read", "/skills/core_pedagogy/SKILL.md") == "allow"
    assert _match(rules, "read", "/memory/AGENTS.md") == "allow"
    # Editing the pedagogy library changes how every future chapter is reasoned
    # about. A background run may not; see section 11.
    assert _match(rules, "write", "/skills/core_pedagogy/SKILL.md") == "deny"
    assert _match(rules, "write", "/memory/AGENTS.md") == "deny"


@pytest.mark.parametrize("builder", [
    perms.curriculum_permissions,
    perms.lesson_design_permissions,
    perms.context_permissions,
])
def test_hitl_turns_a_library_refusal_into_an_approval(builder):
    rules = builder(hitl=True)
    assert _match(rules, "write", "/skills/core_pedagogy/SKILL.md") == "interrupt"
    # An interrupt nobody is present to answer is a hung batch job, which is why
    # this is opt-in rather than the default.
    assert _match(builder(), "write", "/skills/core_pedagogy/SKILL.md") == "deny"


# ── Inactive factors never reach the model ───────────────────────────────────

def _ctx_with(activation: dict, fields: dict = None) -> RunContext:
    return RunContext(
        grade="3", subject="Mathematics",
        contract={"topics": [{"index": 1, "topic": "Place value",
                              "academic": {"masteryTarget": "place decides value",
                                           "concepts": ["place value"]},
                              "grounding": {"pageStart": 15, "pageEnd": 17}}]},
        profile={"fields": fields or {}},
        activation=activation,
    )


def _tool(ctx, name):
    return next(t for t in room.build(ctx) if t.name == name)


def test_only_proposable_factors_are_offered():
    """A factor that activation ruled out must not appear at all - not as a
    'ruled out' entry the model could reason from, and not as an evidence line."""
    ctx = _ctx_with({1: {"active": ["cultural_community", "language"],
                         "proposable": ["language"],
                         "inactive": {"funds_of_knowledge": "no verified input"},
                         "ruledOutForTopic": {"cultural_community": "not relevant here"}}},
                    fields={"home_languages": {"value": "Telugu", "provenance": "verified",
                                               "origin": "admin"}})
    answer = _tool(ctx, "get_active_context").invoke({"topic_index": 1})

    assert "[language]" in answer
    # Ruled out for this topic is named so the model does not re-propose it, but
    # it carries no evidence and no role - it is a prohibition, not an option.
    assert "funds_of_knowledge" not in answer


def test_no_proposable_factor_is_a_correct_answer_not_an_error():
    ctx = _ctx_with({1: {"active": [], "proposable": [], "inactive": {},
                         "ruledOutForTopic": {}}})
    answer = _tool(ctx, "get_active_context").invoke({"topic_index": 1})
    assert "empty adaptation list" in answer
    assert "correct and expected" in answer


def test_local_context_tool_withholds_assumed_evidence():
    """A Real Life section built on an assumed local fact is a fabrication about
    a real village. The tool must not offer the raw value at all."""
    ctx = _ctx_with({}, fields={
        "community_practices": {"value": "rice farming", "provenance": "assumed",
                                "origin": "system"},
        "local_tools": {"value": "sickle", "provenance": "verified",
                        "origin": "teacher"},
    })
    answer = _tool(ctx, "get_verified_local_context").invoke({})
    assert "sickle" in answer
    assert "rice farming" not in answer


def test_provenance_travels_with_every_value():
    ctx = _ctx_with({}, fields={"home_languages": {"value": "Telugu",
                                                   "provenance": "verified",
                                                   "origin": "school_setup"}})
    answer = _tool(ctx, "get_language_profile").invoke({})
    assert "verified" in answer and "school_setup" in answer


def test_contract_tool_states_the_target_as_immovable():
    ctx = _ctx_with({})
    answer = _tool(ctx, "get_academic_contract").invoke({"topic_index": 1})
    assert "immovable" in answer.lower()
    assert "place decides value" in answer


def test_there_is_no_tool_that_writes_the_contract():
    """Node 2 produces a patch. `permissions.py` blocks the path; this asserts
    the weaker and more important thing - it is not offered a verb either."""
    names = {t.name for t in room.build(_ctx_with({}))}
    assert not any(n.startswith(("set_", "write_", "update_", "patch_"))
                   for n in names), names


# ── Identity is bound, not argued ────────────────────────────────────────────

def test_no_tool_accepts_a_school_or_class_identifier():
    """A tool that accepts whose data to read is a tool that can be talked into
    reading somebody else's. See deep_agents/tools/runtime.py."""
    from deep_agents.tools import book, knowledge, pedagogy

    ctx = RunContext(grade="3", subject="Mathematics", school_id="school-a",
                     chapter_markdown="<!-- page 1 -->\nx")
    forbidden = {"school_id", "class_id", "teacher_id", "schoolId", "classId",
                 "chapter_markdown", "profile", "contract"}

    every = [*book.build(ctx), *knowledge.build(ctx, allow_writes=True),
             *pedagogy.build(ctx), *room.build(ctx)]
    for tool in every:
        args = set((tool.args_schema.model_json_schema().get("properties") or {}))
        assert not (args & forbidden), f"{tool.name} exposes {args & forbidden}"


def test_library_writes_are_absent_unless_asked_for():
    """A tool that is not offered cannot be called, which is a stronger
    guarantee than one that is offered and refused."""
    from deep_agents.tools import knowledge

    ctx = RunContext(grade="3", subject="Mathematics")
    assert not any(t.name == "write_canonical_knowledge"
                   for t in knowledge.build(ctx))
    assert any(t.name == "write_canonical_knowledge"
               for t in knowledge.build(ctx, allow_writes=True))


def test_the_lesson_designer_has_no_route_back_to_the_textbook():
    """Deliberate narrowing: a designer that can re-read the page re-derives the
    content instead of designing the route through it."""
    from deep_agents import registry

    ctx = RunContext(grade="3", subject="Mathematics", chapter_title="Numbers",
                     chapter_markdown="<!-- page 1 -->\nx")
    agent, _ = registry.build_lesson_designer(ctx)
    names = set(agent.nodes["tools"].bound._tools_by_name)  # type: ignore[attr-defined]
    assert "get_page_range" not in names
    assert "search_book" not in names
