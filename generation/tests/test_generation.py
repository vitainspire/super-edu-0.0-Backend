"""One guard per way the A/B can lie about whether Node 2 landed.

The whole apparatus rests on a single claim: the two arms differ in exactly one
input. If anything else varies between them, a difference in output proves
nothing and the app on top of it is a confident picture of noise. Most of these
tests defend that claim.

Deliberately free of network and database. The model call is stubbed.

    python -m generation.tests.test_generation
    pytest generation/tests/test_generation.py
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from generation import reinforcement
from generation.compare import _landed, compare_materials


# ── fixtures ─────────────────────────────────────────────────────────────────

def _adaptation(**over):
    a = {"topicIndex": 1, "factor": "language", "type": "modify", "purpose": "access",
         "section": "concept", "change": "Say it in Marathi first, then in English.",
         "reason": "the recorded home language differs from the medium of instruction",
         "supports": ["view"], "evidence": ["home_languages"],
         "dataSource": "verified", "confidence": 0.9, "accepted": True}
    a.update(over)
    return a


def _plan(*adaptations):
    return {"adaptations": list(adaptations) or [_adaptation()],
            "preserve": ["masteryTarget"], "lowersStandard": False}


def _b(*pairs):
    return [{"text": t, "detail": d} for t, d in pairs]


def _sheet(concept="The rim is a circle from above.", **over):
    material = {
        "_meta": {"revision": 0},
        "previousTopicRefresher": {"previousTopic": "", "recap": _b(("a", "one"), ("b", "two"))},
        "concept": _b(("From above", concept)),
        "realLife": {"points": _b(("Plates", "A plate is round from above."))},
        "challenge": {"activity": "Shape Walk", "points": _b(("Walk", "Pairs walk round it."))},
        "levelSet": {"points": _b(("Which side", "Ask which side it was drawn from."))},
        "explore": {"points": _b(("Ceiling", "The room from above?"))},
    }
    material.update(over)
    return material


# ── the claim the whole comparison rests on ──────────────────────────────────

def test_no_plan_contributes_nothing_to_the_prompt():
    """The baseline arm must be what generation produced before Node 2 existed.

    Not "roughly the same" — an empty string. If the no-plan case emitted so much
    as a "no adaptations for this topic" notice, the two arms would differ by
    that notice as well as by the adaptations, and every difference downstream
    would be uninterpretable.
    """
    assert reinforcement.block(None, 1) == ""
    assert reinforcement.block({"adaptations": []}, 1) == ""
    # A plan that exists but says nothing about THIS topic is the same case.
    assert reinforcement.block(_plan(_adaptation(topicIndex=7)), 1) == ""


def test_a_refused_adaptation_never_reaches_the_prompt():
    """Node 2's gate already strips these, and this strips them again.

    The two calls are in different packages, and a plan arriving by some other
    route — read from a log, replayed from a shadow record — must not smuggle a
    refused adaptation into a prompt.
    """
    block = reinforcement.block(
        _plan(_adaptation(accepted=False, change="REFUSED — must never appear")), 1)
    assert "REFUSED" not in block
    assert block == ""


def test_a_no_change_entry_is_not_an_instruction():
    """`no_change` is Node 2 saying the lesson already fits. Printing it as an
    instruction would ask the generator to act on a decision to do nothing."""
    assert reinforcement.block(_plan(_adaptation(type="no_change")), 1) == ""


def test_the_block_carries_the_reason_with_the_change():
    """An instruction with no reason is one the model trades away silently when
    it conflicts with something else in the prompt."""
    block = reinforcement.block(_plan(), 1)
    assert "Say it in Marathi first" in block
    assert "home language differs" in block, "the reason must travel with the change"


def test_confidence_and_provenance_stay_out_of_the_prompt():
    """Both were already acted on by Node 2's gate. Handing the survivors'
    confidence scores to the generator invites it to re-litigate a decision made
    by a node with more information — a 0.62 adaptation is not an optional one,
    it is one that passed."""
    block = reinforcement.block(_plan(_adaptation(confidence=0.62)), 1)
    assert "0.62" not in block
    assert "verified" not in block.lower()


def test_adaptations_are_grouped_by_the_section_they_land_in():
    """The model writes the sheet section by section. A flat list at the top of a
    six-section prompt is a list it has stopped tracking by section four."""
    block = reinforcement.block(_plan(
        _adaptation(section="explore", change="Ask about the ceiling."),
        _adaptation(section="concept", change="Say it in Marathi first."),
    ), 1)
    assert block.index("In Concept:") < block.index("In Explore:"), \
        "sections must appear in the order the sheet is written, not the order proposed"


# ── did it land where it aimed ───────────────────────────────────────────────

def test_landing_distinguishes_ignored_from_misplaced():
    """`elsewhere` and `no` mean opposite things about the generator: one used the
    adaptation and put it somewhere else, the other did not use it."""
    assert _landed("concept", {"concept"}, {"concept"}) == "yes"
    assert _landed("concept", set(), {"concept"}) == "noise-only"
    assert _landed("concept", {"explore"}, {"explore"}) == "elsewhere"
    assert _landed("concept", set(), set()) == "no"
    assert _landed(None, {"concept"}, {"concept"}) == "unplaced"


def test_a_section_that_moved_only_within_noise_does_not_count_as_landed():
    """Generation runs at temperature 0.6, so two runs of the SAME arm differ.
    A change no larger than that gap is the sampler, not the plan — and reporting
    it as a landing would make the app a confident picture of noise.
    """
    baseline = {1: _sheet("The rim is a circle from above.")}
    adapted  = {1: _sheet("The rim is a circle when seen from above.")}
    # A second baseline that wobbled at least as much.
    noise    = {1: _sheet("From above, the rim reads as a circle entirely.")}

    strict = compare_materials(baseline, adapted, plan=_plan(), noise=noise)
    assert strict["topics"][0]["adaptations"][0]["landed"] == "noise-only"
    assert strict["summary"]["landedWhereAimed"] == 0

    # With no ruler, the same movement counts — and `noiseArmRan` is how a
    # reader knows which of the two answers they are looking at.
    loose = compare_materials(baseline, adapted, plan=_plan(), noise=None)
    assert loose["topics"][0]["adaptations"][0]["landed"] == "yes"
    assert loose["noiseArmRan"] is False and strict["noiseArmRan"] is True


def test_the_section_comparison_catches_what_word_overlap_misses():
    """The case the app exists to show.

    An adaptation aimed at Level Set, whose words happen to appear elsewhere on
    the sheet: the fuzzy adoption check is satisfied, and the section it named
    is untouched. The section comparison is a byte difference between two runs
    that varied by one input, so its `no` is not a guess.
    """
    plan = _plan(_adaptation(
        section="levelSet",
        change="Let children answer by holding up their drawing instead of speaking."))
    baseline = {1: _sheet()}
    # The adapted arm changed Concept and left Level Set exactly as it was.
    adapted = {1: _sheet(concept="Children hold up their drawing while you speak.")}

    result = compare_materials(baseline, adapted, plan=plan, noise=None)
    verdict = result["topics"][0]["adaptations"][0]
    assert verdict["landed"] == "elsewhere", \
        "aimed at Level Set, Level Set untouched — however many of its words " \
        "turn up in Concept"
    assert "levelSet" not in result["topics"][0]["sectionsChanged"]


def test_an_unchanged_chapter_reports_nothing_landed():
    """The finding that matters most, and the one an eager tool would hide: the
    plan was handed over and the generator ignored all of it."""
    sheets = {1: _sheet()}
    result = compare_materials(sheets, {1: _sheet()}, plan=_plan(), noise=None)
    assert result["summary"]["sectionsChanged"] == 0
    assert result["summary"]["landedWhereAimed"] == 0
    assert result["summary"]["didNotLand"] == 1
    assert result["topics"][0]["adaptations"][0]["landed"] == "no"


def test_a_topic_node_2_left_alone_is_not_a_failure():
    """Zero adaptations on a topic is Node 2 saying the lesson already fits the
    room. Counting that as a miss would push the node towards adapting for the
    sake of it — the localization-theater failure, arriving through the metric
    meant to detect the opposite."""
    result = compare_materials({1: _sheet()}, {1: _sheet()},
                               plan={"adaptations": []}, noise=None)
    assert result["topics"][0]["adaptations"] == []
    assert result["summary"]["didNotLand"] == 0
    assert result["summary"]["adaptationsHandedOver"] == 0


# ── the app ──────────────────────────────────────────────────────────────────

def test_the_app_template_has_no_untokenised_colour():
    """Every colour must come from a token defined in all three theme states, or
    the page renders one theme's text on the other theme's ground."""
    import re
    template = (Path(__file__).resolve().parents[1] / "app_template.html")
    html = template.read_text(encoding="utf-8")
    style = html[html.index("*, *::before"):html.index("</style>")]
    assert not re.findall(r"#[0-9A-Fa-f]{3,6}", style), \
        "a hex literal outside the token blocks"
    for state in (":root {", ":root:not([data-theme=\"light\"])", ":root[data-theme=\"dark\"]"):
        assert state in html, f"missing theme state: {state}"
    assert "background: var(--paper)" in html, \
        "a transparent body borrows the host's ground"
    assert "/*__DATA__*/null" in html, "the data placeholder must survive edits"


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


# ── Naming the Challenge when the book sets the task ─────────────────────────

_BOOK_TASK = [{"ord": 1, "type": "activity", "page": 12,
               "verbatim": "1. Look at the pictures of a house drawn here.",
               "gist": "asks child to identify which house view is top, front or side"}]
_NO_TASK = [{"ord": 1, "type": "practice", "page": 22, "gist": "exercises"}]


def _named(moves, written, selected="Shape Walk"):
    from generation.compose import _normalise_material
    out = _normalise_material(
        {"index": 1, "challenge": {"activity": written, "points": []}},
        {"index": 1, "topic": "t", "moves": moves, "page_start": 12, "page_end": 13},
        {}, {"activity": {"name": selected}})
    return (out.get("challenge") or {}).get("activity")


def test_the_library_name_does_not_survive_a_book_task():
    """A Challenge that runs page 18's match-sticks under the title "Clap
    Patterns" tells the teacher to do two different things.

    ENFORCED RATHER THAN ASKED FOR. The rule was in the prompt throughout, was
    rewritten when a run showed it ignored in three of six topics, and rewritten
    again when the field label turned out to be the real cause — and it still
    copied on one of two topics. A requirement that survives three prompt
    revisions belongs in code.
    """
    assert _named(_BOOK_TASK, "Shape Walk") == "Identify which house view is top, front or side"


def test_a_name_the_model_got_right_is_left_alone():
    """The backstop replaces a COPY, never a correct answer."""
    assert _named(_BOOK_TASK, "Match the three house views") == "Match the three house views"


def test_an_empty_name_is_filled_from_the_book():
    assert _named(_BOOK_TASK, "") == "Identify which house view is top, front or side"


def test_without_a_book_task_the_library_name_is_correct_and_kept():
    """The pages set no task, so the library template IS the Challenge. This was
    misread as a bug once; copying here is the rule working."""
    assert _named(_NO_TASK, "Compare Collections", "Compare Collections") == "Compare Collections"


def test_decoration_is_still_stripped_when_there_is_no_book_task():
    assert _named(_NO_TASK, "Compare Collections (number)",
                  "Compare Collections") == "Compare Collections"


def test_an_unusable_gist_leaves_the_name_alone():
    """Better the library name than a mangled title."""
    thin = [{"ord": 1, "type": "activity", "verbatim": "x", "gist": "asks child to"}]
    assert _named(thin, "Shape Walk") == "Shape Walk"
