"""What each agent is required to return - architecture sections 6 and 12.

"Structured output is important. No free-form essay gets inserted into your
state." These schemas are that sentence, enforced by the harness: `deepagents`
passes them as `response_format`, so the final answer arrives as a
schema-validated tool call rather than as JSON the caller has to parse and hope
about.

THE FIELD NAMES ARE NOT NEGOTIABLE and they are not chosen here. Each model
below mirrors, key for key, the RAW dict the corresponding existing agent's
`_normalise` already accepts:

    CurriculumReasoning     -> prep_flow.agents.reasoning._normalise
    ExperiencePlan          -> prep_flow.agents.experience._normalise
    ContextReinforcement    -> context_flow.plan.normalise

That is the whole integration strategy in one sentence. The Deep Agent replaces
the model CALL and nothing else; the bounding, the deduplication, the anchor
snap-back, the chain sealing and the confidence clamping all still happen in the
functions that already do them. A chapter derived through an agent and a chapter
derived through `call_json` therefore converge on byte-identical state, which is
what makes the A/B in `eval/` a comparison of reasoning rather than of plumbing.

Change a name here and you have silently disabled a normaliser. The tests in
`tests/test_schema_parity.py` exist to catch exactly that.
"""
from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field


# ── Curriculum reasoning (Agent 1) ───────────────────────────────────────────

class Misconception(BaseModel):
    """A wrong conclusion this chapter reliably produces."""

    belief: str = Field(
        description="What the child will wrongly conclude, in the child's own "
                    "terms - not 'confuses area and perimeter' but 'thinks the "
                    "bigger shape must need more fence'. Max ~160 characters.")
    topics: list[int] = Field(
        default_factory=list,
        description="Indices of the topics where this misconception is likely "
                    "to surface. Empty is acceptable: a misconception true of "
                    "the chapter but not of any one period is still worth "
                    "recording.")


class AnchorGroup(BaseModel):
    """The pool of real objects one strand of the chapter can be taught with."""

    strand: str = Field(
        default="main",
        description="Which thread of the chapter these objects serve. Use "
                    "'main' unless the chapter genuinely changes subject "
                    "(shapes, then counting).")
    objects: list[str] = Field(
        default_factory=list,
        description="Objects a government primary classroom actually contains "
                    "or a child actually brings. Things you can count, hold or "
                    "point at. No manipulative that must be bought.")


class ChainEntry(BaseModel):
    """One topic's link in the chapter's knowledge chain."""

    index: int = Field(description="The topic index this entry describes.")
    strand: str = Field(default="main", description="The thread this topic belongs to.")
    gained: str = Field(
        description="What THIS period leaves the child able to do. One clause. "
                    "Not the chapter's ambition - this single lesson's.")
    bridgesTo: str = Field(
        description="What the next topic can now assume. Write it in the words "
                    "the next topic's `assumes` will use, because the two are "
                    "checked against each other for overlap.")
    assumes: Optional[str] = Field(
        default=None,
        description="What this topic requires already in place. Leave null for "
                    "the first topic of a strand - its floor is the chapter's "
                    "prerequisites, not a previous lesson.")


class CurriculumReasoning(BaseModel):
    """What the textbook does not say - the whole chapter, in one answer."""

    prerequisites: list[str] = Field(
        default_factory=list,
        description="What a child must already be able to do before the "
                    "chapter's first sentence means anything. Abilities, not "
                    "topics. Max 8. Do not list anything this chapter itself "
                    "teaches on a later page - check with search_book first.")
    anchors: list[AnchorGroup] = Field(
        default_factory=list,
        description="Object pools, grouped by strand. Prefer objects already "
                    "printed in the chapter's figures.")
    misconceptions: list[Misconception] = Field(
        default_factory=list,
        description="Max 6. What children actually get wrong here, not what a "
                    "syllabus says is difficult.")
    chain: list[ChainEntry] = Field(
        default_factory=list,
        description="One entry per topic, in teaching order. This is the "
                    "forward/backward learning chain and every topic must "
                    "appear exactly once.")


# ── Lesson experience (Agent 2) ──────────────────────────────────────────────

class ExperiencePlan(BaseModel):
    """The cognitive path through one period, and the object that makes it happen."""

    index: int = Field(description="The topic index this plan is for.")
    entryPoint: str = Field(
        default="",
        description="Where the period starts, in the room. Max ~80 chars.")
    trajectory: list[str] = Field(
        default_factory=list,
        description="The path the child's thinking takes, step by step. Max 8 "
                    "steps, each one clause. Each step must be something the "
                    "child DOES or NOTICES, not something the teacher says.")
    conceptualJump: str = Field(
        default="",
        description="The single moment where understanding actually changes. "
                    "If you cannot name one, the period has no lesson in it.")
    scaffolding: list[str] = Field(
        default_factory=list, description="Max 3 supports for children who stall.")
    anchor: str = Field(
        default="",
        description="The one object this period is built around. MUST come "
                    "from the strand's anchor pool - anything else is snapped "
                    "back to the pool and your reason is discarded.")
    anchorReason: str = Field(
        default="", description="Why this object and not another in the pool.")
    studentAction: str = Field(
        default="",
        description="What forty children physically do. Max ~90 chars. It must "
                    "be runnable in the period length and class size you were "
                    "given by get_room_constraints.")
    observation: str = Field(
        default="", description="What the teacher watches for. Max ~90 chars.")
    inference: str = Field(
        default="",
        description="What the child concludes. This must BE this topic's "
                    "`gained` clause, not the chapter's mastery target. Aiming "
                    "at the target instead is the most common failure here and "
                    "it is detected and corrected automatically.")
    evidence: str = Field(
        default="", description="How the teacher knows it landed.")
    gap: str = Field(
        default="",
        description="The one audited shortfall this period closes, chosen from "
                    "the topic's `missing` list. Leave empty unless you also "
                    "give gapCloser - an unpaired gap is discarded.")
    gapKind: str = Field(
        default="",
        description="The kind label of that shortfall, copied exactly from the "
                    "audit's vocabulary. An invented label is dropped.")
    gapCloser: str = Field(
        default="",
        description="What stages that gap in a room of forty. Concrete.")
    floor: str = Field(
        default="",
        description="What the child furthest behind leaves able to do -- ONE "
                    "sentence, in the child's own voice, physically demonstrable "
                    "in under 30 seconds by a child who cannot read the page. "
                    "Not a lowered target: the first reachable step on the same "
                    "road as `inference`, usually the trajectory step just "
                    "before `conceptualJump`. See `differentiation`. Example: "
                    "'I can stand over my school bag and point to what I would "
                    "see from above.'")
    transferTask: str = Field(
        default="",
        description="A case nobody showed them, that this period makes "
                    "decidable. Max ~180 chars.")


class ExperienceBatch(BaseModel):
    """The experience agent answers for a window of topics at once, because the
    seam between two periods has to be written from both sides in one place."""

    plans: list[ExperiencePlan] = Field(
        default_factory=list,
        description="One plan per topic in the window, in the order given.")


# ── Context reinforcement (Agent 3) ──────────────────────────────────────────

class Adaptation(BaseModel):
    """One change to how a lesson is DELIVERED. Never to what it teaches."""

    topicIndex: int = Field(description="Which topic this adapts.")
    factor: str = Field(
        description="The factor id this rests on, exactly as get_active_context "
                    "listed it. A factor not in that list is rejected.")
    type: Literal["add", "modify", "substitute", "fallback", "no_change"] = Field(
        description="add: something new. modify: reword or restage what is "
                    "there. substitute: swap a material or example for an "
                    "equivalent. fallback: a simpler route when the first needs "
                    "something absent. no_change: explicitly nothing.")
    purpose: Literal["learning", "access", "delivery"] = Field(
        description="learning: deepens the same target. access: removes a "
                    "barrier to the same target. delivery: changes staging "
                    "only. IT MUST BE A PURPOSE THIS FACTOR PRODUCES - "
                    "`get_active_context` lists them per factor, and claiming "
                    "one the factor does not produce is rejected as aiming the "
                    "change at the wrong problem.")
    section: Literal["refresher", "concept", "realLife", "challenge",
                     "levelSet", "explore"] = Field(
        description="Which of the six sections this changes. Exactly one.")
    change: str = Field(
        description="What to do, concretely enough that a teacher could follow "
                    "it. Max ~400 chars.")
    reason: str = Field(
        description="Why this room needs it. Cite the recorded evidence, not a "
                    "demographic assumption. Max ~400 chars.")
    supports: list[str] = Field(
        default_factory=list,
        description="The mastery target or required concept this helps the "
                    "class reach, IN THE TOPIC'S OWN WORDS as the contract "
                    "states them - not a label like 'mastery_target' or "
                    "'access'. The gate matches these against the topic's "
                    "actual target and concept list, so a generic label names "
                    "nothing and is rejected. An adaptation that is locally "
                    "interesting and unrelated to the target is the most common "
                    "way this goes wrong. Never leave this empty.")
    evidence: list[str] = Field(
        default_factory=list,
        description="The exact profile field names this rests on, as printed by "
                    "the context tools. This is checked against the profile: an "
                    "adaptation citing a field that is absent, or assumed where "
                    "verified was required, is rejected by the gate. Claiming "
                    "nothing is better than claiming a field you did not read.")
    data_source: Literal["verified", "assumed", "none"] = Field(
        description="The weakest provenance among your cited evidence. Be "
                    "honest: the gate re-derives this and a mismatch is a "
                    "rejection.")
    confidence: float = Field(
        default=0.5, ge=0.0, le=1.0,
        description="How sure you are this room needs this.")


class ContextReinforcement(BaseModel):
    """The Context Reinforcement Plan - a patch, not a second lesson."""

    adaptations: list[Adaptation] = Field(
        default_factory=list,
        description="Max 3 per topic. An empty list is a correct answer for a "
                    "room with nothing recorded about it.")
    preserve: list[str] = Field(
        default_factory=list,
        description="What must survive unchanged. Always includes the mastery "
                    "target and the required concepts.")
    lowers_standard: bool = Field(
        default=True,
        description="True if ANY adaptation reduces what a child ends up able "
                    "to do. Answer honestly - this defaults to True precisely "
                    "so that an omission stops the plan rather than shipping "
                    "it.")
    note: str = Field(
        default="",
        description="One line for a human reading this months later. Say why "
                    "the list is empty when it is.")


# ── Repair evidence (Agent 4) ────────────────────────────────────────────────

class PageEvidence(BaseModel):
    """One thing the printed page actually says, against one failed finding."""

    about: str = Field(
        description="Which finding this answers, quoted or closely paraphrased "
                    "from the finding text you were given.")
    section: Optional[str] = Field(
        default=None,
        description="The sheet section the finding was filed against: "
                    "refresher | concept | realLife | challenge | levelSet | explore.")
    page: Optional[int] = Field(
        default=None,
        description="The page you read this off, from the <!-- page N --> "
                    "markers. Omit only when the answer is that no page "
                    "carries it.")
    verbatim: str = Field(
        default="",
        description="What the page ACTUALLY prints, copied exactly - the words, "
                    "the number, the caption. Empty string when the honest "
                    "answer is that the chapter does not contain it anywhere, "
                    "which is itself the evidence a rewrite needs.")
    correction: str = Field(
        description="What the sheet should say instead, in one sentence. State "
                    "the real number or the real wording; do not restate the "
                    "problem.")


class RepairEvidence(BaseModel):
    """The facts a rewrite needs, gathered from the book BEFORE rewriting.

    Deliberately not a rewritten sheet. The writer that produced the sheet is
    better at prose than this agent is; what it could not do was go and look at
    the page. So this agent looks, and hands back only what it found - which is
    the one thing the repair prompt was missing.
    """

    evidence: list[PageEvidence] = Field(
        default_factory=list,
        description="One entry per finding you could check. Skip findings that "
                    "the book cannot settle (pacing, staging, classroom "
                    "feasibility) rather than guessing at them.")
    unverifiable: list[str] = Field(
        default_factory=list,
        description="Findings you deliberately did not answer, and why - one "
                    "short line each. An honest gap here is worth more than an "
                    "invented fact.")
