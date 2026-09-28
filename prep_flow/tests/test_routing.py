"""Routing must be invisible until it is switched on, and exact once it is.

The property that matters most is the first one. Every default in this pipeline
is chosen so that an unconfigured checkout behaves the way it did before the
feature existed, and routing is the easiest of them to get wrong: a table with
models in it, consulted unconditionally, silently changes which model answers
every call in the system the moment it is merged.
"""
import asyncio

import pytest

from prep_flow import routing


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("PREP_FLOW_ROUTING", "PREP_FLOW_MODEL_HARD",
                "PREP_FLOW_MODEL_STANDARD", "PREP_FLOW_MODEL_CHEAP"):
        monkeypatch.delenv(var, raising=False)


# ── Off by default ───────────────────────────────────────────────────────────

def test_routing_is_off_until_asked_for():
    assert routing.active()["enabled"] is False
    for label in ("reasoning", "generation[T1-T3]", "moves[1-4]", "learner[T7]"):
        assert routing.resolve(label) is None, label


@pytest.mark.parametrize("value", ["", "0", "off", "false", "none"])
def test_falsey_preset_values_leave_it_off(monkeypatch, value):
    monkeypatch.setenv("PREP_FLOW_ROUTING", value)
    assert routing.resolve("reasoning") is None


def test_an_unknown_preset_does_not_route_anything(monkeypatch, capsys):
    """A typo must not half-configure the pipeline. Loud, and off."""
    monkeypatch.setenv("PREP_FLOW_ROUTING", "freee")
    assert routing.resolve("reasoning") is None
    assert "unknown preset" in capsys.readouterr().out


# ── The table ────────────────────────────────────────────────────────────────

def test_every_production_label_lands_in_a_tier():
    """The labels below are the real ones, taken from the call sites. A stage
    that stops matching its prefix falls silently back to the global model, and
    that is exactly the kind of regression nothing else would catch."""
    live = [
        "reasoning", "reasoning-rederive", "mastery[1-4]", "experience[1-3]",
        "planning[1-3]", "moves[T7]", "moves[1-4]", "generation[T1-T3]",
        "repair[T2#1]", "activity-invention", "competency-mapping",
        "knowledge-extraction[T4]", "learner[T7]", "consistency",
        "context-reinforcement[1-3]", "feedback-optimization",
        "sequence-override-refresher", "deep:curriculum", "deep:experience",
        "deep:context",
    ]
    unclaimed = [lbl for lbl in live if routing.tier_for(lbl) is None]
    assert not unclaimed, f"no tier claims: {unclaimed}"


def test_the_rederive_prefix_beats_the_shorter_one():
    """`reasoning-rederive` starts with `reasoning`. Longest-match-first is what
    stops the second re-derive of a chapter being routed as something else."""
    assert routing.tier_for("reasoning-rederive") == routing.HARD
    assert routing.tier_for("reasoning") == routing.HARD


def test_an_unknown_label_is_left_alone():
    """Unclaimed is a real answer — a new stage keeps today's behaviour until
    somebody decides where it belongs."""
    assert routing.tier_for("some-new-stage[1]") is None
    assert routing.resolve("some-new-stage[1]") is None


def test_the_cached_chapter_call_is_in_the_top_tier():
    """Curriculum reasoning is cached per book: a bad chain is served to every
    future class studying it and no later stage can repair it. If this ever
    drops to a cheap tier it should be a deliberate, visible decision."""
    assert routing.tier_for("reasoning") == routing.HARD
    assert routing.tier_for("mastery[1-4]") == routing.HARD


# ── Presets and overrides ────────────────────────────────────────────────────

def test_the_free_preset_routes_every_tier(monkeypatch):
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    state = routing.active()
    assert state["enabled"] is True
    assert all(state["tiers"][tier]["model"] for tier in routing.TIERS)
    assert routing.resolve("reasoning") == routing.PRESETS["free"][routing.HARD]["model"]
    assert routing.resolve("moves[1-4]") == routing.PRESETS["free"][routing.CHEAP]["model"]


def test_a_tier_override_beats_the_preset(monkeypatch):
    """Pinning one tier while measuring another is how this gets tuned."""
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    monkeypatch.setenv("PREP_FLOW_MODEL_HARD", "some/pinned-model")
    assert routing.resolve("reasoning") == "some/pinned-model"
    assert routing.resolve("moves[1-4]") == routing.PRESETS["free"][routing.CHEAP]["model"]


def test_a_tier_override_works_with_no_preset(monkeypatch):
    monkeypatch.setenv("PREP_FLOW_MODEL_CHEAP", "some/cheap-model")
    assert routing.resolve("moves[1-4]") == "some/cheap-model"
    assert routing.resolve("reasoning") is None, "other tiers stay unrouted"


def test_env_is_read_at_call_time_not_import_time(monkeypatch):
    """The CLIs and these tests set the vars long after the module tree is
    imported. A value captured at import would make the flag appear to work and
    do nothing."""
    assert routing.resolve("reasoning") is None
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    assert routing.resolve("reasoning") is not None


# ── The seam into call_json ──────────────────────────────────────────────────

def _capture_options(monkeypatch):
    """Run one call_json and return the options ai.py would have received."""
    seen = {}

    async def fake_call_ai(messages, options=None):
        seen.update(options or {})
        return '{"ok": true}'

    from prep_flow import llm
    monkeypatch.setattr(llm, "call_ai", fake_call_ai)
    return seen


def test_call_json_sends_no_model_when_routing_is_off(monkeypatch):
    from prep_flow import llm
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="reasoning", required=("ok",)))
    assert "model" not in seen, "an unrouted call must leave ai.py's default alone"


def test_call_json_applies_the_routed_model(monkeypatch):
    from prep_flow import llm
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="reasoning", required=("ok",)))
    assert seen.get("model") == routing.PRESETS["free"][routing.HARD]["model"]


def test_an_explicit_model_beats_the_routing_table(monkeypatch):
    """`generation/compose.py` already passes a model via
    OPENROUTER_GENERATION_MODEL. Routing must not take that away."""
    from prep_flow import llm
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="generation[T1-T3]", required=("ok",),
                              model="explicit/choice"))
    assert seen.get("model") == "explicit/choice"


def test_two_tiers_can_reach_two_models_in_one_run(monkeypatch):
    """The whole point: one run, different models per stage."""
    from prep_flow import llm
    monkeypatch.setenv("PREP_FLOW_MODEL_HARD", "hard/model")
    monkeypatch.setenv("PREP_FLOW_MODEL_CHEAP", "cheap/model")
    seen = _capture_options(monkeypatch)

    asyncio.run(llm.call_json("hi", label="reasoning", required=("ok",)))
    assert seen.get("model") == "hard/model"
    asyncio.run(llm.call_json("hi", label="moves[1-4]", required=("ok",)))
    assert seen.get("model") == "cheap/model"


# ── Token headroom ───────────────────────────────────────────────────────────

def test_headroom_is_capped(monkeypatch):
    """THE SECOND BUG. Headroom multiplies the CALLER's budget, and callers are
    not uniform — move extraction asks for 6,000 tokens where reasoning asks for
    2,400. A flat 4x turned the first into 24,000, and a thinking model handed
    24,000 tokens uses them: a real trace run sat on one call for over two
    minutes. The cap is measured, not chosen."""
    assert routing.bounded_tokens(6000, 4) == routing.MAX_EFFECTIVE_TOKENS
    assert routing.bounded_tokens(2400, 4) == 9600, "under the cap, multiply"
    assert routing.bounded_tokens(6000, 1) == 6000, "no headroom, no change"
    # Routing may buy a model more room, never less.
    assert routing.bounded_tokens(20000, 4) == 20000


def test_a_thinking_model_gets_the_room_it_needs(monkeypatch):
    """THE BUG THIS TEST EXISTS FOR. The first `free` preset put a model needing
    4x the tokens on the CHEAP tier, which serves the smallest budgets in the
    pipeline. Live, every call came back EMPTY — a thinking model that runs out
    of budget returns no content at all, not a short answer — and burned three
    retries plus the fallback before failing."""
    from prep_flow import llm
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free-spread")
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="moves[1-4]", required=("ok",),
                              max_tokens=500))
    room = routing.PRESETS["free-spread"][routing.CHEAP]["headroom"]
    assert seen["max_tokens"] == routing.bounded_tokens(500, room)


def test_a_direct_model_gets_exactly_the_budget_asked_for(monkeypatch):
    from prep_flow import llm
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="reasoning", required=("ok",),
                              max_tokens=500))
    assert seen["max_tokens"] == 500, "the hard tier answers directly; no multiplier"


def test_headroom_is_untouched_when_routing_is_off(monkeypatch):
    from prep_flow import llm
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="moves[1-4]", required=("ok",),
                              max_tokens=500))
    assert seen["max_tokens"] == 500


def test_an_explicit_model_keeps_its_own_budget(monkeypatch):
    """A caller that named a model sized its own max_tokens for it. Routing must
    not silently quadruple somebody else's budget."""
    from prep_flow import llm
    monkeypatch.setenv("PREP_FLOW_ROUTING", "free")
    seen = _capture_options(monkeypatch)
    asyncio.run(llm.call_json("hi", label="moves[1-4]", required=("ok",),
                              max_tokens=500, model="explicit/choice"))
    assert seen["max_tokens"] == 500


def test_a_hand_pinned_model_is_assumed_to_need_room(monkeypatch):
    """Most of the free tier thinks before answering and fails silent-empty, so
    the safe default for a model nobody has measured is room, not precision."""
    monkeypatch.setenv("PREP_FLOW_MODEL_CHEAP", "some/unmeasured-model")
    assert routing.headroom("moves[1-4]") > 1


def test_headroom_can_be_pinned_per_tier(monkeypatch):
    monkeypatch.setenv("PREP_FLOW_MODEL_CHEAP", "some/model")
    monkeypatch.setenv("PREP_FLOW_HEADROOM_CHEAP", "1")
    assert routing.headroom("moves[1-4]") == 1
