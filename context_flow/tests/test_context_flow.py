"""One guard per way Node 2 can quietly stop being Node 2.

Every failure mode §18 of the implementation framework names is a way this node
can keep running, keep producing plans, and be wrong — none of them raises, and
most of them produce output that reads as competent. So each has a test, and each
test is written from the failure rather than from the function: the name says
what goes wrong in a classroom, not which branch is covered.

Deliberately free of network and database. The one model call is stubbed, which
is the point — everything worth guarding here is deterministic by design, and a
test needing an API key would be a test nobody runs before committing.

    python -m context_flow.tests.test_context_flow       # standalone
    pytest context_flow/tests/test_context_flow.py       # or under pytest
"""
import asyncio
import sys
from pathlib import Path

# The repo root: this file is <package>/tests/<name>.py.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from context_flow import activation, factors, gate, plan as plan_module, profile
from context_flow.graph import apply as apply_plan
from context_flow.state import DEFAULT_CONFIG


# ── fixtures ─────────────────────────────────────────────────────────────────

def _row(**over):
    """One contract topic, complete, so a test can break exactly one thing."""
    row = {
        "index": 1, "topic": "Views of objects",
        "grounding": {"pageStart": 10, "pageEnd": 12, "bookOrder": []},
        "academic": {
            "masteryTarget": "decide which view a drawing was made from",
            "requiredConcepts": ["view", "shape"], "competencies": ["geo.1"],
            "vocabulary": ["above"],
        },
        "knowledgeChain": {"gained": "can say what changes when you move round an "
                                     "object", "bridgesTo": "to drawing the view",
                           "assumes": ""},
        "masteryAudit": {"supplied": ["a cup from two sides"],
                         "missing": [{"kind": "contrast", "missing": "no object that "
                                                                    "looks the same from every side"},
                                     {"kind": "transfer", "missing": "never used on a "
                                                                    "case the page did not choose"}]},
        "misconceptions": ["the object changes shape"],
        "experiencePlan": {"trajectory": ["hold it", "move round it", "compare"],
                           "anchor": "cup", "anchorPool": ["cup", "bottle"],
                           "closesGap": {"kind": "contrast", "gap": "no same-from-every-side object"}},
        "lessonPlan": {"minutes": {"concept": 6, "challenge": 8},
                       "newVocabulary": ["above"]},
        "selectedActivity": {"name": "Shape Walk", "source": "library",
                             "materials": ["a cup"]},
        "sectionSpec": [{"section": "concept", "label": "Concept", "minutes": 6,
                         "mode": "grounded"}],
        "readiness": {"ready": True},
    }
    row.update(over)
    return row


def _profile(**over):
    raw = {
        "class_size": {"value": 52, "provenance": "verified", "origin": "admin"},
        "medium_of_instruction": {"value": "English", "provenance": "verified",
                                  "origin": "school_setup"},
        "home_languages": {"value": ["Marathi"], "provenance": "verified",
                           "origin": "teacher"},
        "has_paper": {"value": False, "provenance": "verified", "origin": "school_setup"},
        "has_board": {"value": True, "provenance": "verified", "origin": "school_setup"},
        "materials_available": {"value": ["cup", "chalk"], "provenance": "verified",
                                "origin": "teacher"},
    }
    raw.update(over)
    return profile.normalise(raw)


def _adaptation(**over):
    a = {
        "topicIndex": 1, "factor": "language", "type": "modify", "purpose": "access",
        "section": "concept",
        "change": "Have pairs say in Marathi what changed before the English term is given.",
        "reason": "the recorded home language differs from the medium of instruction",
        "supports": ["decide which view a drawing was made from"],
        "evidence": ["home_languages", "medium_of_instruction"],
        "dataSource": "verified", "confidence": 0.9,
        "accepted": None, "rejectedBecause": [],
    }
    a.update(over)
    return a


def _run_gate(adaptations, *, prof=None, lowers=False, row=None):
    prof = prof or _profile()
    row = row or _row()
    the_plan = {"adaptations": [dict(a) for a in adaptations],
                "preserve": [], "lowersStandard": lowers, "note": ""}
    report = gate.run(
        the_plan, contract_rows={1: row}, profile=prof,
        activations_by_index={1: activation.activate(prof, row)})
    return the_plan, report


# ── §18: invented local context ──────────────────────────────────────────────

def test_an_unverified_community_fact_never_reaches_the_model():
    """The first failure mode: a model handed an empty field fills it with a
    plausible village.

    Enforced twice over, and both are tested here because either alone is
    breakable. The field is dropped at NORMALISATION, so it cannot be put in a
    prompt; and the factor that would have read it stays INACTIVE, so nothing
    can propose against it.
    """
    prof = _profile(community_occupations={
        "value": ["fishing"], "provenance": "assumed", "origin": "system"})

    assert not prof.has("community_occupations"), \
        "an assumed community fact must not survive normalisation"
    assert any("local-context field" in d["why"] for d in prof.dropped), \
        "and the drop must say why, or a school cannot fix its data"

    entries = {e.factor.id: e for e in activation.activate(prof, _row())}
    assert not entries["funds_of_knowledge"].active
    assert not entries["cultural_community"].active


def test_verified_community_data_does_switch_the_local_factors_on():
    """The other half of the same rule — a tier that never activates is not a
    safety feature, it is a dead branch."""
    prof = _profile(local_markets={
        "value": ["the Tuesday vegetable market"], "provenance": "verified",
        "origin": "teacher"})
    entries = {e.factor.id: e for e in activation.activate(prof, _row())}
    assert entries["local_relevance"].active
    assert "local_markets" in entries["local_relevance"].exposes


def test_a_cited_field_that_does_not_exist_is_caught():
    _, report = _run_gate([_adaptation(factor="local_relevance", purpose="learning",
                                       evidence=["community_occupations"])])
    assert report["accepted"] == 0
    assert report["byCheck"].get("invented_local_context")


# ── §4: the hard rule ────────────────────────────────────────────────────────

def test_confidence_cannot_upgrade_assumed_data_to_verified():
    """§4: "high model confidence cannot turn an assumed or unknown context field
    into verified local knowledge."

    The two are different axes and the gate must not weigh one against the other.
    A 0.99 adaptation resting on an assumed field is a confident guess about a
    guess.
    """
    prof = _profile(class_ability_spread={
        "value": "wide", "provenance": "assumed", "origin": "teacher"})
    _, report = _run_gate(
        [_adaptation(factor="scaffolding", purpose="access",
                     evidence=["class_ability_spread"],
                     dataSource="verified", confidence=0.99)],
        prof=prof)
    assert report["accepted"] == 0
    messages = " ".join(f["message"] for f in report["findings"])
    assert "cannot make an assumed field verified" in messages


def test_an_honest_assumed_claim_on_the_same_field_is_allowed():
    """The rule is about the CLAIM, not about the data. An adaptation that says
    'assumed' and means it is usable — refusing it too would leave the baseline
    tier with nothing to reason from."""
    prof = _profile(class_ability_spread={
        "value": "wide", "provenance": "assumed", "origin": "teacher"})
    _, report = _run_gate(
        [_adaptation(factor="scaffolding", purpose="access",
                     evidence=["class_ability_spread"], dataSource="assumed")],
        prof=prof)
    assert report["accepted"] == 1, report["findings"]


def test_a_physical_room_fact_is_citable_by_any_factor():
    """Found by the first real run against a live model.

    Node 2 proposed drawing on the board for a 52-child room with no paper — a
    feasible, safe, well-grounded change — and cited `has_board`. The gate
    refused it, because `has_board` sits on the resources factor's evidence list
    rather than the classroom factor's. The field was verified, present and true.

    Room facts are CONSTRAINTS, not claims about a community. A classroom
    adaptation that puts something on the board needs to know there is a board,
    and a scaffolding one that hands out a worksheet is unrunnable without paper
    exactly as a resource one is. So `profile.ROOM_FACTS` is exempt from the
    per-factor partition, and the same list is what the prompt shows.
    """
    _, report = _run_gate([_adaptation(
        factor="classroom", purpose="delivery", section="challenge",
        change="Draw two large circles on the board and sort the photos into them.",
        reason="52 children recorded and no paper",
        evidence=["class_size", "has_board", "has_paper"],
        dataSource="verified")])
    assert report["accepted"] == 1, report["findings"]


def test_citing_the_wrong_field_is_not_called_invention():
    """A real field read by the wrong factor is a different defect from a
    fabricated one, and must not wear its name.

    `invented_local_context` exists for a model that made up a village. Filing a
    misdirected citation under it sends whoever reads the log hunting for a
    fabrication that is not there — which is what the live run's
    "50% rejected, suspect the prompt" alarm did.
    """
    prof = _profile(household_skills={
        "value": "net mending", "provenance": "verified", "origin": "teacher"})
    _, report = _run_gate([_adaptation(
        factor="cognitive_load", purpose="access",
        evidence=["household_skills"], dataSource="verified")], prof=prof)
    assert report["accepted"] == 0
    assert report["byCheck"].get("evidence_mismatch") == 1
    assert not report["byCheck"].get("invented_local_context"),         "the field exists — nothing was invented"


def test_a_field_nothing_recorded_is_still_invention():
    """The exemption must not swallow the check it was carved out of."""
    _, report = _run_gate([_adaptation(
        factor="local_relevance", purpose="learning",
        evidence=["community_occupations"])])
    assert report["byCheck"].get("invented_local_context")


def test_an_adaptation_may_not_contradict_the_field_it_cites():
    """The third defect a live run found, and the most dangerous.

    The model cited `class_size` and then reasoned about a class of 30 in a room
    recorded as 52. It cited `home_languages` — Marathi — and wrote "alongside
    their Telugu equivalents", a language named nowhere in the profile OR the
    chapter, inferred from the Telugu-region place names in the text. Both were
    accepted, and the Telugu one landed verbatim in the sheet.

    The gate had checked that the cited field exists, is verified, and is
    readable by that factor — and never that the change agreed with it. A
    citation was being treated as grounding when it was decoration.
    """
    from context_flow.gate import _contradicts_evidence, _unechoed_evidence
    prof = _profile()   # class_size 52, home_languages Marathi

    contradicts = _contradicts_evidence({
        "evidence": ["class_size"], "change": "Use a pair-share instead.",
        "reason": "With a class size of 30 this is impractical."}, prof)
    assert contradicts and contradicts[0]["check"] == "contradicts_evidence"

    unechoed = _unechoed_evidence({
        "evidence": ["home_languages"],
        "change": "Introduce the words alongside their Telugu equivalents.",
        "reason": "the home language (Telugu) differs from the medium"}, prof)
    assert unechoed and unechoed[0]["check"] == "unechoed_evidence"


def test_the_contradiction_checks_do_not_fire_on_honest_adaptations():
    """A fix that refused more would not be a fix. All four of these are
    legitimate and must pass untouched."""
    from context_flow.gate import _contradicts_evidence, _unechoed_evidence
    prof = _profile()

    for evidence, change, reason, label in (
        (["class_size"], "Use fixed pairs at the desk.",
         "a class size of 52 makes a moving circuit impossible", "quotes the number"),
        (["class_size"], "With a large class, use pairs.",
         "the class is large", "does not quote it, but says nothing false"),
        (["has_paper"], "Since there is no paper, use the board.",
         "no paper recorded", "a boolean has no value to echo"),
        (["materials_available"], "Use the chalk to draw two circles.",
         "chalk is available", "one of several recorded values"),
    ):
        a = {"evidence": evidence, "change": change, "reason": reason}
        assert not _contradicts_evidence(a, prof), f"blocked: {label}"
        if label != "does not quote it, but says nothing false":
            assert not _unechoed_evidence(a, prof), f"warned: {label}"


def test_an_unechoed_citation_warns_without_refusing():
    """Advisory, not blocking. A legitimate adaptation can cite `class_size` and
    say "with a large class" without quoting 52, so this asks a reviewer to look
    rather than refusing on its own — and the count is reported apart from
    `rejected`, because the two ask for different actions."""
    the_plan, report = _run_gate([_adaptation(
        factor="language", purpose="access", section="concept",
        change="Introduce the terms alongside their Telugu equivalents.",
        reason="the home language differs from the medium of instruction",
        evidence=["home_languages"], dataSource="verified")])
    assert report["accepted"] == 1, "an advisory must not refuse"
    assert report["warned"] == 1
    assert the_plan["adaptations"][0]["warnings"],         "the warning must ride on the adaptation, where a reader will see it"


# ── §18: stereotyping ────────────────────────────────────────────────────────

def test_a_demographic_label_is_not_evidence():
    _, report = _run_gate([_adaptation(
        factor="socioeconomic", purpose="access", evidence=["rural"],
        dataSource="assumed")])
    assert report["byCheck"].get("stereotyping")


def test_a_reason_that_merely_mentions_home_is_not_stereotyping():
    """The check scans EVIDENCE, never prose.

    The best-evidenced language adaptation there is says "the recorded home
    language differs from the medium of instruction". A keyword scan over
    `reason` would reject exactly the adaptations this node exists to produce.
    """
    _, report = _run_gate([_adaptation(
        reason="most students' home language is not the medium of instruction, "
               "and this is a rural school")])
    assert report["accepted"] == 1, report["findings"]


# ── §18: wrong reinforcement target ──────────────────────────────────────────

def test_a_locally_interesting_change_that_serves_nothing_is_refused():
    _, report = _run_gate([_adaptation(
        factor="motivation", purpose="learning",
        change="Add a points competition between rows.",
        supports=["classroom energy"], evidence=[], dataSource="assumed")])
    assert report["byCheck"].get("wrong_target")


def test_naming_the_target_in_the_frameworks_own_words_is_accepted():
    """§7's example literally uses `"supports": ["mastery_target", "access"]`.
    A check that rejected the spec's own example would be teaching the model to
    paste the target verbatim, which measures nothing."""
    _, report = _run_gate([_adaptation(supports=["mastery_target", "access"])])
    assert report["accepted"] == 1, report["findings"]


# ── §18: resource contradiction ──────────────────────────────────────────────

def test_a_change_needing_paper_is_refused_in_a_school_with_none():
    _, report = _run_gate([_adaptation(
        factor="scaffolding", purpose="access",
        change="Give each pair a printed handout of the three views.",
        evidence=[], dataSource="assumed")])
    assert report["byCheck"].get("resource_contradiction")


def test_a_material_being_avoided_is_not_a_material_being_required():
    """The second defect the first live run found.

    An adaptation that names a missing material IN ORDER TO ROUTE AROUND IT was
    refused for naming it. That is the worst thing to punish: it is the
    adaptation doing exactly what the recorded constraint asked, and the
    cheapest way to satisfy such a check is to stop explaining yourself.

    Both directions are asserted, because a fix that simply stopped refusing
    would have removed the check rather than corrected it.
    """
    from context_flow.gate import _resource_contradiction
    prof = _profile(materials_available={
        "value": ["chalk", "string"], "provenance": "verified", "origin": "teacher"})

    for change, should_refuse in (
        ("Give each pair a printed handout of the three views.", True),
        ("Use the projector to show the three views.", True),
        ("Hand out photocopies of the family tree.", True),
        ("Since there is no paper, draw the two circles on the board instead.", False),
        ("Teacher-drawn faces on the board rather than paper cards.", False),
        ("Without a projector, hold the cup up at the front instead.", False),
        ("Draw two large circles on the board and sort into them.", False),
    ):
        refused = bool(_resource_contradiction({"type": "substitute", "change": change}, prof))
        assert refused == should_refuse, (
            f"{'should refuse' if should_refuse else 'should NOT refuse'}: {change!r}")


def test_a_school_that_recorded_nothing_gets_no_feasibility_verdict():
    """An empty materials list is not proof of an empty room.

    Without this, a school with no setup data would have every substitution
    rejected — and a check that fires on every entry is a check somebody
    switches off, which is how §18's resource contradiction arrives through the
    check meant to prevent it.
    """
    bare = profile.normalise({"class_size": {"value": 30, "provenance": "verified",
                                             "origin": "admin"}})
    _, report = _run_gate([_adaptation(
        factor="scaffolding", purpose="access",
        change="Give each pair a printed handout.", evidence=[],
        dataSource="assumed")], prof=bare)
    assert not report["byCheck"].get("resource_contradiction")


# ── §10: safety ──────────────────────────────────────────────────────────────

def test_no_child_is_asked_to_reveal_their_circumstances():
    """The check is a REQUEST plus a PERSONAL SUBJECT in one sentence, not a
    fixed phrase — and the difference is not academic.

    The first version was a single rigid sequence (ask + child + verb +
    possessive + topic). "Ask each child to tell THE CLASS about their family's
    income" walked straight past it, because three words of ordinary English sat
    where the pattern wanted a possessive. It is here as a table because a
    safety check that one paraphrase defeats reports clean, which is worse than
    not having it.

    The passing rows matter as much as the flagged ones: a check that fires on
    "have pairs say in Marathi what changed" would reject the best adaptation
    this node can make.
    """
    from context_flow.gate import _unsafe_disclosure

    for change, should_flag in (
        ("Ask each child to tell the class about their family's income and job.", True),
        ("Ask children to share what their parents do for work.", True),
        ("Have students describe their home situation.", True),
        ("Raise your hand if you do not have a cup at home.", True),
        ("Put up your hand if nobody at home can help with this.", True),
        ("Children who cannot bring a cup can share with a partner.", True),
        ("Ask pairs to compare two cups and say what changed.", False),
        ("The teacher writes the family of shapes on the board while students watch.", False),
        ("Before naming the English term, have pairs say in Marathi what changed.", False),
        ("Use the market stall example: which side does the seller see?", False),
        ("Ask students to hold the cup and move around it.", False),
    ):
        flagged = bool(_unsafe_disclosure({"change": change}))
        assert flagged == should_flag, (
            f"{'should have flagged' if should_flag else 'should NOT have flagged'}: "
            f"{change!r}")

    # And it reaches the verdict through the gate, not only in isolation.
    _, report = _run_gate([_adaptation(
        factor="family_community", purpose="learning",
        change="Ask each child to tell the class about their family's income and job.",
        evidence=[], dataSource="assumed")])
    assert report["accepted"] == 0


# ── §18: lowered rigor ───────────────────────────────────────────────────────

def test_a_plan_that_admits_it_lowers_the_standard_takes_the_whole_plan_down():
    """The one judgement §10 leaves to the model, and the only check that is
    plan-level rather than per adaptation.

    Per-adaptation rejection is right everywhere else — three good changes and
    one invented village should ship the three. "Does this plan, taken together,
    leave the class knowing less" is a question about the set, and a set that
    fails it has no innocent members.
    """
    _, report = _run_gate([_adaptation(), _adaptation(factor="cognitive_load")],
                          lowers=True)
    assert not report["passed"] and report["accepted"] == 0
    assert report["byCheck"].get("lowered_rigor")


def test_an_omitted_rigor_verdict_defaults_to_refusing():
    """A missing field must not read as a pass. The model that forgot to answer
    is the model whose answer is least worth assuming."""
    normalised = plan_module.normalise({"adaptations": []}, topic_indexes=[1])
    assert normalised["lowersStandard"] is True


# ── §2 / §18: minimal intervention, teacher overload ─────────────────────────

def test_a_teacher_is_never_handed_more_changes_than_a_period_can_absorb():
    """And the surplus dropped is the LOWEST-confidence one.

    The cap runs after the per-adaptation checks so it spends its slots on
    proposals that actually passed — dropping first would let a rejected entry
    consume a slot a good one needed.
    """
    many = [_adaptation(confidence=c, factor=f, purpose=p)
            for c, f, p in ((0.9, "language", "access"),
                            (0.8, "cognitive_load", "access"),
                            (0.7, "classroom", "delivery"),
                            (0.6, "scaffolding", "access"))]
    for a in many:
        a["evidence"] = []
        a["dataSource"] = "assumed"
    the_plan, report = _run_gate(many)
    assert report["accepted"] == plan_module.MAX_ADAPTATIONS_PER_TOPIC
    dropped = [a for a in the_plan["adaptations"] if not a["accepted"]]
    assert len(dropped) == 1 and dropped[0]["factor"] == "scaffolding", \
        "the lowest-confidence change is the one that goes"


def test_no_change_is_a_real_answer_and_costs_no_slot():
    """§8's last type and §18's localization theater are the same point. A
    lesson that already fits its room should be able to say so, and saying so
    must not compete with real adaptations for the per-period cap."""
    the_plan, report = _run_gate([
        _adaptation(type="no_change", reason="this lesson fits this room as planned",
                    change="", purpose="", section="", supports=[], evidence=[]),
        _adaptation(), _adaptation(factor="cognitive_load", evidence=[],
                                   dataSource="assumed"),
        _adaptation(factor="classroom", purpose="delivery", evidence=[],
                    dataSource="assumed"),
    ])
    assert report["accepted"] == 4, report["findings"]


# ── §5: the model does not decide what is available ──────────────────────────

def test_a_factor_the_profile_switched_off_cannot_be_proposed_against():
    _, report = _run_gate([_adaptation(
        factor="continuity", purpose="access",
        change="Add a catch-up bridge for absent children.",
        evidence=[], dataSource="assumed")])
    assert report["byCheck"].get("inactive_factor")


def test_longitudinal_factors_need_measured_evidence_not_merely_verified():
    """§17 of the research framework: do not infer continuity from an isolated
    observation. A teacher can verify that the community fishes; nobody verifies
    an attendance pattern by recalling it, so ORIGIN is what is checked.
    """
    recalled = _profile(attendance_history={
        "value": "patchy", "provenance": "verified", "origin": "teacher"})
    measured = _profile(attendance_history={
        "value": {"present": 0.6}, "provenance": "verified", "origin": "measured"})
    off = {e.factor.id: e for e in activation.activate(recalled, _row())}
    on = {e.factor.id: e for e in activation.activate(measured, _row())}
    assert not off["continuity"].active
    assert on["continuity"].active


def test_a_baseline_factor_stays_on_with_no_data_at_all():
    """§5's baseline row says missing data means "use only safe defaults" — which
    is NOT the same as switching the factor off.

    A cognitive-load adaptation grounded in the CONTRACT (seven new terms in one
    period) is legitimate with no profile data whatsoever, and a tier that went
    silent without school data would lose every adaptation this node can make
    for a school that has recorded nothing.
    """
    bare = profile.normalise({})
    entries = {e.factor.id: e for e in activation.activate(bare, _row())}
    assert entries["cognitive_load"].active
    assert entries["cognitive_load"].exposes == (), \
        "active, but with nothing to cite — it may not state a local fact"


# ── §6: a present factor is not automatically a relevant one ─────────────────

def test_a_factor_with_no_barrier_on_this_topic_is_ruled_out_before_the_call():
    """The framework's own example: a verified home language justifies a
    vocabulary bridge for a language-heavy concept and may justify nothing for a
    highly visual one.

    Ruled out DETERMINISTICALLY where it is a fact — no language mismatch and no
    new terminology is not a judgement call — which is also where §14's token
    argument lands: the factor never reaches the prompt.
    """
    same_language = _profile(home_languages={
        "value": ["English"], "provenance": "verified", "origin": "teacher"})
    visual = _row(lessonPlan={"minutes": {"concept": 6}, "newVocabulary": []})
    entries = {e.factor.id: e for e in activation.activate(same_language, visual)}
    assert entries["language"].active, "the data is there"
    assert entries["language"].relevant is False, "and it changes nothing here"
    assert "language" not in [
        e.factor.id for e in activation.proposable(activation.activate(same_language, visual))]


def test_an_activity_needing_nothing_the_room_lacks_raises_no_resource_question():
    entries = {e.factor.id: e for e in activation.activate(_profile(), _row())}
    assert entries["resources"].relevant is False
    assert "everything the activity needs is present" in entries["resources"].hint


# ── §16: shadow mode ─────────────────────────────────────────────────────────

def test_shadow_mode_is_on_by_default_and_withholds_the_plan():
    """§16 is a sequence — run both arms, have teachers rate the proposals,
    compare a golden set, and only THEN move Node 2 into the generation path.

    A default of "applied" would skip every step of it, and nothing would ever
    be in shadow mode to evaluate.
    """
    assert DEFAULT_CONFIG["shadow_mode"] is True
    assert apply_plan({"status": "shadowed", "shadow": True,
                       "plan": {"adaptations": [_adaptation(accepted=True)]}}) is None


def test_a_refused_plan_is_withheld_even_with_shadow_mode_off():
    """Two independent conditions, not one. Turning shadow mode off is a decision
    about evaluation; it is not permission to ship a plan the gate refused."""
    assert apply_plan({"status": "refused", "shadow": False,
                       "plan": {"adaptations": [_adaptation(accepted=True)]}}) is None


def test_an_applied_plan_hands_over_only_what_was_accepted():
    """Generation should be given instructions, not a mix of instructions and
    rejected proposals it has to filter — the filtering is this node's job and
    doing it twice is how the two disagree."""
    handed = apply_plan({
        "status": "applied", "shadow": False,
        "plan": {"adaptations": [_adaptation(accepted=True),
                                 _adaptation(factor="motivation", accepted=False,
                                             rejectedBecause=["wrong target"])],
                 "preserve": ["masteryTarget"]}})
    assert len(handed["adaptations"]) == 1
    assert "accepted" not in handed["adaptations"][0]
    assert "rejectedBecause" not in handed["adaptations"][0]


# ── §14: no call is made when the answer is already known ────────────────────

def test_no_model_call_is_made_when_no_factor_can_speak():
    """A paid call whose answer is already settled is §14's token waste in its
    purest form. `propose` returns early and says so, and the note is what keeps
    that distinguishable from a call that failed."""
    from context_flow import reasoning

    called = []

    async def _boom(*a, **k):
        called.append(1)
        raise AssertionError("no call should have been made")

    original, reasoning.call_json = reasoning.call_json, _boom
    try:
        bare = profile.normalise({})
        row = _row()
        # Every factor ruled out for this topic: nothing proposable remains.
        entries = [e._replace(relevant=False) for e in activation.activate(bare, row)]
        result = asyncio.run(reasoning.propose(
            rows=[row], profile=bare, activations_by_index={1: entries},
            grade="3", subject="Maths"))
    finally:
        reasoning.call_json = original

    assert not called
    assert result["adaptations"] == []
    assert "no model call was made" in result["note"]


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
