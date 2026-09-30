"""Which factors are active, and — separately — which are relevant to THIS topic.

§5 and §6 are two different questions and this module keeps them apart, because
collapsing them is how a system ends up adapting a lesson for a factor that is
merely *available*:

    ACTIVATION (§5)   Does this factor have evidence to stand on at all?
                      A property of the PROFILE. Deterministic, no model.
    RELEVANCE  (§6)   Given that it does, does it create a real barrier,
                      opportunity or delivery constraint for THIS topic and THIS
                      mastery target? A property of the CONTRACT ROW.

The framework's own example is the cleanest statement of why: a verified home
language justifies a vocabulary bridge for a language-heavy concept, and may
justify nothing at all for a highly visual one. The factor is active in both
cases. It is relevant in one.

WHAT THIS MODULE DECIDES AND WHAT IT LEAVES TO THE MODEL. Activation is settled
here, completely — §5's "The model does not decide whether a factor is
available." Relevance is settled here only where it is a FACT: an activity that
needs no technology cannot have a technology barrier, and a room with no devices
cannot have a technology opportunity. Where relevance is a judgement — does this
concept lean on language, would a local example genuinely carry it — the factor
is passed to the model with a `hint` saying what the deterministic signals were,
and the model has to defend its answer under §6.4's "recommend an adaptation only
when the relationship is defensible".

WHY THE HINTS ARE COMPUTED RATHER THAN DESCRIBED. "This topic introduces 7 new
vocabulary terms and its Concept section is grounded" is a fact about the
contract that a model reading prose would have to re-derive, badly. Counting it
here costs nothing, and it turns a vague prompt instruction into a number the
model can be wrong about visibly.
"""
from typing import NamedTuple, Optional

from . import factors as factors_module
from . import profile as profile_module
from .factors import BASELINE, GATE, LOCAL, LONGITUDINAL


class Activation(NamedTuple):
    """One factor's standing on one lesson."""
    factor: factors_module.Factor
    active: bool
    reason: str
    # The profile fields this factor may expose to the model — verified only for
    # LOCAL and LONGITUDINAL, verified-or-assumed for BASELINE. §5's "LLM
    # receives" column, made concrete.
    exposes: tuple
    relevant: Optional[bool]    # None = the model decides
    hint: str                   # what the deterministic signals were


# ── §5: activation ───────────────────────────────────────────────────────────

def _activate_one(factor: factors_module.Factor,
                  profile: profile_module.ContextProfile) -> tuple[bool, str, tuple]:
    if factor.tier == GATE:
        # Never an adaptation generator, always run. Returning it as active with
        # nothing exposed is what keeps `gate.py` from having to special-case
        # two of the twenty factors out of the registry it reads.
        return True, "equity and safety are cross-cutting constraints, always checked", ()

    if factor.tier == BASELINE:
        present = profile.verified_present(factor.evidence) or profile.present(factor.evidence)
        if present:
            return True, f"{len(present)} recorded field(s)", tuple(present)
        # ACTIVE WITH NOTHING EXPOSED. §5's baseline row says missing data means
        # "use only safe defaults; do not invent local facts" — which is not the
        # same as switching the factor off. A room whose size nobody recorded
        # still needs a lesson that works in a normal room, and a cognitive-load
        # adaptation grounded in the CONTRACT (seven new terms in one period) is
        # legitimate with no profile data at all.
        return True, "no recorded data — safe defaults only, no local facts", ()

    if factor.tier == LOCAL:
        verified = profile.verified_present(factor.evidence)
        if verified:
            return True, f"{len(verified)} verified local field(s)", tuple(verified)
        return False, "no verified community or cultural input", ()

    if factor.tier == LONGITUDINAL:
        measured = [n for n in profile.verified_present(factor.evidence)
                    if (profile.get(n) or profile_module.Field(n, None, "", "", None)).origin
                    == profile_module.MEASURED]
        if measured:
            return True, f"{len(measured)} measured longitudinal field(s)", tuple(measured)
        # Stricter than LOCAL on purpose: a teacher may verify that the community
        # fishes, and cannot verify an attendance pattern by recalling it. §17 of
        # the research framework — "do not infer dropout risk from isolated
        # observations" — is a rule about the ORIGIN of the evidence, so origin
        # is what is checked.
        return False, "no measured attendance or engagement history", ()

    return False, f"unknown tier '{factor.tier}'", ()


# ── §6: topic relevance ──────────────────────────────────────────────────────
#
# One function per factor that can be settled deterministically. Each returns
# (relevant, hint) where `relevant` may be None — "the model decides, here is
# what I could measure". Anything with no entry here is always the model's call.

def _language_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    academic = row.get("academic") or {}
    plan = row.get("lessonPlan") or {}
    new_terms = len(plan.get("newVocabulary") or [])
    vocabulary = len(academic.get("vocabulary") or [])
    medium = profile.value("medium_of_instruction")
    home = profile.value("home_languages")
    mismatch = bool(home and medium and _differs(home, medium))
    hint = (f"{new_terms} new term(s) this period, {vocabulary} in the topic's "
            f"vocabulary; home/medium language mismatch: {mismatch}")
    if not mismatch and new_terms == 0:
        # No language barrier recorded AND nothing new to name. The concept may
        # still be hard; it is not hard for a LANGUAGE reason, and an adaptation
        # here would be the "wrong reinforcement target" failure in §18.
        return False, hint + " — no recorded barrier and no new terminology"
    return None, hint


def _technology_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    available = profile_module.technology_available(profile)
    activity = row.get("selectedActivity") or {}
    materials = " ".join(str(m).lower() for m in (activity.get("materials") or []))
    wants_tech = any(word in materials for word in
                     ("device", "tablet", "phone", "projector", "screen",
                      "computer", "laptop", "video", "internet"))
    hint = (f"planned activity needs technology: {wants_tech}; "
            f"technology usable in this room: {available}")
    if not wants_tech and not available:
        # Nothing to substitute and nothing to offer. §7 of the research
        # framework: technology is a conditional resource, and a room with
        # neither the need nor the means has no technology question to answer.
        return False, hint + " — neither needed nor available"
    if wants_tech and not available:
        # The one case that is deterministically relevant. A substitution is
        # REQUIRED, not proposed: the planned activity cannot run.
        return True, hint + " — the planned activity cannot run as written"
    return None, hint


def _resource_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    activity = row.get("selectedActivity") or {}
    needs = [str(m).strip().lower() for m in (activity.get("materials") or []) if str(m).strip()]
    if not profile_module.materials_recorded(profile):
        return None, "nothing recorded about this room's materials"
    have = profile_module.available_materials(profile)
    missing = [m for m in needs if not any(h in m or m in h for h in have)]
    hint = (f"activity needs {needs or '(nothing)'}; room has "
            f"{sorted(have) or '(nothing recorded)'}")
    if missing:
        return True, hint + f" — missing: {', '.join(missing)}"
    if needs:
        return False, hint + " — everything the activity needs is present"
    return None, hint


def _cognitive_load_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    plan = row.get("lessonPlan") or {}
    academic = row.get("academic") or {}
    experience = row.get("experiencePlan") or {}
    new_terms = len(plan.get("newVocabulary") or [])
    concepts = len(academic.get("requiredConcepts") or [])
    steps = len(experience.get("trajectory") or [])
    duration = profile.value("lesson_duration") or sum(
        (plan.get("minutes") or {}).values()) or 0
    hint = (f"{concepts} concept(s), {new_terms} new term(s), {steps}-step "
            f"trajectory, {duration} minute period")
    return None, hint


def _classroom_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    activity = row.get("selectedActivity") or {}
    size = profile.value("class_size")
    grouping = activity.get("grouping") or profile.value("grouping_constraints")
    hint = f"class size {size or 'unrecorded'}; activity grouping {grouping or 'unspecified'}"
    return None, hint


def _prior_knowledge_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    chain = row.get("knowledgeChain") or {}
    assumes = chain.get("assumes") or ""
    weak = profile.value("known_weak_topics") or []
    hint = (f"this period assumes: {assumes or '(nothing — it opens a strand)'}; "
            f"{len(weak) if isinstance(weak, (list, tuple)) else 0} recorded weak topic(s)")
    if not assumes and not weak:
        return False, hint + " — nothing assumed and nothing recorded as weak"
    return None, hint


def _participation_relevance(row: dict, profile) -> tuple[Optional[bool], str]:
    requirements = profile.value("accessibility_requirements")
    modes = profile.value("response_modes_available")
    hint = (f"accessibility requirements recorded: {bool(requirements)}; "
            f"response modes recorded: {bool(modes)}")
    return None, hint


def _local_relevance_hint(row: dict, profile) -> tuple[Optional[bool], str]:
    """The audited shortfall is the opening, and it is the honest one.

    A LOCAL adaptation has somewhere real to land when Node 1's mastery audit
    found the page short of an example, a contrast or an application — those are
    exactly the shortfalls an authentic local situation can supply. Where the
    audit found nothing missing, a local example is decoration, which is §18's
    "localization theater".
    """
    audit = row.get("masteryAudit") or {}
    closes = (row.get("experiencePlan") or {}).get("closesGap") or {}
    open_kinds = [m.get("kind") for m in (audit.get("missing") or [])
                  if m.get("kind") and m.get("kind") != closes.get("kind")]
    hint = (f"shortfalls this period does NOT already close: "
            f"{', '.join(open_kinds) or '(none)'}; the period already closes "
            f"'{closes.get('kind') or 'nothing'}'")
    if not open_kinds and closes:
        return False, hint + " — the audit's shortfall is already spoken for"
    return None, hint


_RELEVANCE = {
    "language": _language_relevance,
    "technology": _technology_relevance,
    "resources": _resource_relevance,
    "cognitive_load": _cognitive_load_relevance,
    "classroom": _classroom_relevance,
    "prior_knowledge": _prior_knowledge_relevance,
    "participation": _participation_relevance,
    "local_relevance": _local_relevance_hint,
    "cultural_community": _local_relevance_hint,
    "funds_of_knowledge": _local_relevance_hint,
}


def _differs(home, medium) -> bool:
    languages = home if isinstance(home, (list, tuple, set)) else [home]
    spoken = {str(v).strip().lower() for v in languages if str(v).strip()}
    return bool(spoken) and str(medium).strip().lower() not in spoken


# ── The entry point ──────────────────────────────────────────────────────────

def activate(profile: profile_module.ContextProfile, row: dict) -> list[Activation]:
    """Every factor's standing on one topic, in registry order.

    Returns ALL twenty, active or not. The inactive ones never reach a prompt —
    `reasoning.py` filters on `.active` — but they do reach the log, because
    "the local-relevance factor was inactive: no verified community input" is
    the single most useful line for a school wondering why their lessons come
    back unlocalised, and a list that silently omitted it could not say so.
    """
    out: list[Activation] = []
    for factor in factors_module.FACTORS:
        active, reason, exposes = _activate_one(factor, profile)
        relevant, hint = (None, "")
        if active and factor.tier != GATE:
            checker = _RELEVANCE.get(factor.id)
            if checker:
                try:
                    relevant, hint = checker(row, profile)
                except Exception as exc:          # never fail a run on a hint
                    relevant, hint = None, f"(relevance check failed: {exc})"
        out.append(Activation(factor, active, reason, exposes, relevant, hint))
    return out


def proposable(activations: list[Activation]) -> list[Activation]:
    """The factors the model may actually propose an adaptation for.

    Active, not a gate, and not deterministically ruled out for this topic. This
    is the list that becomes the prompt, and it is where §14's token argument is
    settled: a chapter with no verified community data never sends the five
    LOCAL factors, and a topic whose activity needs nothing the room lacks never
    sends the resource factor.
    """
    return [a for a in activations
            if a.active and a.factor.tier != GATE and a.relevant is not False]


def summarise(activations: list[Activation]) -> dict:
    return {
        "active": sorted(a.factor.id for a in activations if a.active),
        "inactive": {a.factor.id: a.reason for a in activations if not a.active},
        "ruledOutForTopic": {a.factor.id: a.hint for a in activations
                             if a.active and a.relevant is False},
        "proposable": sorted(a.factor.id for a in proposable(activations)),
    }
