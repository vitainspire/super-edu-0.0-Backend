"""The twenty factors, as data — and the tier that decides when each may speak.

This is the Contextual Reinforcement Research Framework's factor set, encoded so
that code rather than a model decides which of them are available on any given
lesson. Nothing here is a prompt. It is the table §5 of the implementation
framework describes as "deterministic code decides which contextual factors are
active and what data can be exposed to the model", and the reason it is a table
rather than a paragraph in a prompt is the first failure mode in §18: a model
handed an empty `community practices` field fills it with a plausible village.

THE TIER IS THE WHOLE MECHANISM. Each factor names what evidence it needs before
it is allowed to influence anything, and the four tiers differ in what happens
when that evidence is absent:

    BASELINE      the classroom facts a school already has, plus everything
                  Node 1 derived. Missing data means SAFE DEFAULTS — a lesson
                  planned for a room whose size nobody recorded is planned for a
                  normal room, not for an invented one.
    LOCAL         community, cultural and funds-of-knowledge context. Missing
                  data means the factor STAYS INACTIVE. There is no safe default
                  for "what do families here do for a living", and the difference
                  between this tier and the one above it is the difference
                  between contextual reinforcement and a stereotype engine.
    LONGITUDINAL  attendance, engagement history, intervention history. Missing
                  data means INACTIVE, for a second reason on top of the first:
                  §6 of the research framework is explicit that deep engagement
                  and continuity cannot be inferred from one lesson, and a single
                  observation converted into a learner trait is a diagnosis
                  nobody is qualified to make.
    GATE          equity and safety. ALWAYS active, and not adaptation
                  generators — they are constraints the finished plan is checked
                  against. See `gate.py`.

WHY `purposes` IS ON EVERY ROW. §9 splits adaptation into three kinds — learning
reinforcement, access reinforcement, delivery reinforcement — and the split is
load-bearing rather than descriptive. A language bridge is an ACCESS adaptation
and belongs wherever the learner enters the task; a materials substitution is a
DELIVERY adaptation and is a fact about the room. An adaptation that claims the
wrong purpose is usually an adaptation aimed at the wrong problem, and the gate
checks the claim.

`evidence` NAMES PROFILE FIELDS, not prose. It is what `activation.py` reads to
decide whether this factor has anything to stand on, and what `reasoning.py`
copies into the prompt — only those fields, only when verified, which is where
§14's "inactive factors are not included in the prompt" is actually enforced.
"""
from typing import NamedTuple

# ── Tiers ────────────────────────────────────────────────────────────────────
BASELINE, LOCAL, LONGITUDINAL, GATE = "baseline", "local", "longitudinal", "gate"

# ── The three purposes of adaptation (§9) ────────────────────────────────────
LEARNING, ACCESS, DELIVERY = "learning", "access", "delivery"

# ── The four research groups, kept for reporting rather than for logic ───────
LEARNER, ENVIRONMENT, CONTEXT, CONDITIONS = (
    "learner", "environment", "context", "conditions")


class Factor(NamedTuple):
    id: str
    number: int
    name: str
    group: str
    tier: str
    question: str          # the factor's core research question
    evidence: tuple        # ContextProfile field names that feed it
    role: str              # what contextual reinforcement means for this factor
    purposes: tuple        # which of the three kinds of adaptation it produces


FACTORS: tuple[Factor, ...] = (
    Factor(
        id="cultural_community", number=1, name="Cultural & Community Context",
        group=CONTEXT, tier=LOCAL,
        question="What social, cultural, family and community realities surround "
                 "the learner?",
        evidence=("community_practices", "local_traditions", "community_events",
                  "family_structures"),
        role="Use authentic community context as an entry point when it genuinely "
             "supports the learning target. Never generic 'local' decoration.",
        purposes=(LEARNING,),
    ),
    Factor(
        id="funds_of_knowledge", number=2, name="Funds of Knowledge",
        group=CONTEXT, tier=LOCAL,
        question="What knowledge and skills already exist in learners, families "
                 "and communities?",
        evidence=("household_skills", "community_occupations", "local_tools",
                  "craft_practices"),
        role="Activate existing knowledge as a bridge into the academic concept. "
             "Do not assume an asset exists because of geography or demographics.",
        purposes=(LEARNING,),
    ),
    Factor(
        id="cultural_representation", number=3, name="Cultural Representation",
        group=CONTEXT, tier=LOCAL,
        question="Are the people and perspectives in the material authentic and "
                 "inclusive?",
        evidence=("community_profile", "represented_identities",
                  "verified_cultural_practices"),
        role="Check whose experiences appear and whether localization is authentic "
             "rather than tokenistic or stereotyped.",
        purposes=(LEARNING, ACCESS),
    ),
    Factor(
        id="language", number=4, name="Language & Communication",
        group=LEARNER, tier=BASELINE,
        question="What languages and communication demands shape access to this "
                 "lesson?",
        evidence=("medium_of_instruction", "home_languages", "teacher_languages",
                  "literacy_level", "oral_language_strength"),
        role="Oral, visual and bilingual entry points, and academic-language "
             "bridges — while preserving the concept and its vocabulary target.",
        purposes=(ACCESS,),
    ),
    Factor(
        id="socioeconomic", number=5, name="Socioeconomic & Household Context",
        group=CONTEXT, tier=BASELINE,
        question="What out-of-school demands affect participation, homework and "
                 "available time?",
        evidence=("homework_feasible", "household_responsibilities",
                  "home_support_available"),
        role="Avoid designs assuming stable time, adult help or long home tasks. "
             "Prefer low-burden and flexible work where the recorded constraints "
             "say so.",
        purposes=(ACCESS, DELIVERY),
    ),
    Factor(
        id="resources", number=6, name="Resource & Infrastructure Conditions",
        group=CONDITIONS, tier=BASELINE,
        question="What physical and operational resources are genuinely available?",
        evidence=("materials_available", "has_board", "has_paper",
                  "classroom_space", "electricity", "resource_level"),
        role="Select activities executable in the actual room, with a sensible "
             "alternative when the planned one is not.",
        purposes=(DELIVERY,),
    ),
    Factor(
        id="technology", number=7, name="Technology Access & Usability",
        group=CONDITIONS, tier=BASELINE,
        question="What digital infrastructure exists, and is using it actually "
                 "beneficial?",
        evidence=("devices_available", "has_projector", "has_internet",
                  "electricity", "digital_literacy"),
        role="Technology is a conditional resource, never a default requirement. "
             "Provide an equivalent non-digital path.",
        purposes=(DELIVERY,),
    ),
    Factor(
        id="participation", number=8, name="Learner Access & Participation",
        group=LEARNER, tier=BASELINE,
        question="What participation routes let learners demonstrate the intended "
                 "knowledge?",
        evidence=("accessibility_requirements", "response_modes_available",
                  "participation_constraints"),
        role="Separate the mastery criterion from the modality used to "
             "demonstrate it.",
        purposes=(ACCESS,),
    ),
    Factor(
        id="prior_knowledge", number=9, name="Prior Knowledge & Readiness",
        group=LEARNER, tier=BASELINE,
        # Node 1 data, not school data: the contract already carries the
        # prerequisites, the chain and the misconceptions. This factor asks what
        # the ROOM does about them, not what they are.
        question="Does this class have what the lesson assumes?",
        evidence=("known_weak_topics", "prerequisite_mastery", "class_readiness_note"),
        role="Activate or bridge only the prerequisites the target needs, using "
             "the knowledge chain Node 1 already derived.",
        purposes=(LEARNING, ACCESS),
    ),
    Factor(
        id="cognitive_load", number=10, name="Cognitive Load & Task Complexity",
        group=LEARNER, tier=BASELINE,
        question="How much must the learner process at once?",
        evidence=("lesson_duration", "class_size", "known_weak_topics"),
        role="Sequence tasks and reduce simultaneous novelty. Protect attention "
             "for the target concept — never by removing the concept.",
        purposes=(ACCESS,),
    ),
    Factor(
        id="scaffolding", number=11, name="Scaffolding & Differentiation",
        group=LEARNER, tier=BASELINE,
        question="What temporary supports enable successful performance?",
        evidence=("class_ability_spread", "support_staff", "known_weak_topics"),
        role="Move from modelling and guided practice toward independent transfer "
             "without lowering the final expectation.",
        purposes=(ACCESS, LEARNING),
    ),
    Factor(
        id="classroom", number=12, name="Classroom Environment & Management",
        group=ENVIRONMENT, tier=BASELINE,
        question="What classroom conditions enable the planned activity?",
        evidence=("class_size", "grouping_constraints", "lesson_duration",
                  "classroom_space", "seating"),
        role="Design the practical conditions around the academic activity rather "
             "than leaving them implicit.",
        purposes=(DELIVERY,),
    ),
    Factor(
        id="psychological_safety", number=13, name="Psychological Safety & Belonging",
        group=ENVIRONMENT, tier=BASELINE,
        question="Does the environment make participation feel safe and socially "
                 "possible?",
        evidence=("participation_constraints", "class_size", "grouping_constraints"),
        # The clause after the semicolon is the boundary, and it is in the role
        # rather than only in the prompt because the gate reads roles.
        role="Predictable routines and low-risk entry routes; never an attempt to "
             "diagnose private psychological states.",
        purposes=(ACCESS,),
    ),
    Factor(
        id="motivation", number=14, name="Motivation & Agency",
        group=LEARNER, tier=BASELINE,
        question="What makes sustained engagement more likely here?",
        evidence=("class_interests", "teaching_style", "learning_objective"),
        role="Build purpose, achievable challenge and appropriate choice INTO the "
             "task. Not generic games, points or rewards.",
        purposes=(LEARNING, ACCESS),
    ),
    Factor(
        id="local_relevance", number=15, name="Local Relevance & Real-World Application",
        group=CONTEXT, tier=LOCAL,
        question="Where does this academic concept appear in the learner's actual "
                 "world?",
        evidence=("local_markets", "community_occupations", "local_phenomena",
                  "local_transport", "community_problems"),
        role="Connect the concept to authentic local situations and useful "
             "applications — the concept, not a decoration around it.",
        purposes=(LEARNING,),
    ),
    Factor(
        id="family_community", number=16, name="Family & Community Connection",
        group=CONTEXT, tier=LOCAL,
        question="How can appropriate home or community participation reinforce "
                 "this learning?",
        evidence=("community_assets", "local_role_models", "household_skills",
                  "home_support_available"),
        role="ONE manageable link when it adds learning value. Never a task "
             "assuming extensive family time or disclosure.",
        purposes=(LEARNING,),
    ),
    Factor(
        id="continuity", number=17, name="Attendance & Learning Continuity",
        group=CONDITIONS, tier=LONGITUDINAL,
        question="What happens when learners miss part of the sequence?",
        evidence=("attendance_history", "missed_topics", "seasonal_absence"),
        role="Support catch-up and re-entry where real longitudinal evidence "
             "exists. Never infer dropout risk from an isolated observation.",
        purposes=(ACCESS, LEARNING),
    ),
    Factor(
        id="engagement_profile", number=18, name="Learner Engagement Profile",
        group=LEARNER, tier=LONGITUDINAL,
        question="What does observed evidence show about how this class responds "
                 "to learning conditions?",
        evidence=("engagement_observations", "participation_history",
                  "intervention_history"),
        role="Use measured patterns to choose supports. Never convert sparse "
             "observations into psychological diagnoses or fixed learner traits.",
        purposes=(ACCESS, LEARNING),
    ),
    Factor(
        id="equity", number=19, name="Equity", group=ENVIRONMENT, tier=GATE,
        question="Does the adaptation increase access without changing the "
                 "intended standard?",
        evidence=(),
        role="Hold the academic destination constant while allowing different "
             "routes, scaffolds and participation modes.",
        purposes=(),
    ),
    Factor(
        id="safety", number=20, name="Safety & Appropriateness",
        group=ENVIRONMENT, tier=GATE,
        question="Could contextualization itself cause harm or introduce "
                 "unsupported assumptions?",
        evidence=(),
        role="Reject adaptations that are unsafe, stereotyped, unsupported, "
             "inaccessible or unnecessarily intrusive.",
        purposes=(),
    ),
)

BY_ID: dict[str, Factor] = {f.id: f for f in FACTORS}

# The two that are never adaptation generators. Kept as a named set because
# three modules ask the same question and a fourth spelling of it would drift.
GATE_FACTORS = frozenset(f.id for f in FACTORS if f.tier == GATE)
ADAPTABLE = tuple(f for f in FACTORS if f.tier != GATE)

# Every profile field any factor can read. `profile.py` validates against this,
# so a field added to the profile and to no factor is caught at import rather
# than by never being used.
ALL_EVIDENCE_FIELDS = frozenset(
    field for factor in FACTORS for field in factor.evidence)


def factors_for_tier(tier: str) -> tuple[Factor, ...]:
    return tuple(f for f in FACTORS if f.tier == tier)


def purposes_of(factor_id: str) -> tuple:
    factor = BY_ID.get(factor_id)
    return factor.purposes if factor else ()


# ── Who OWNS a field, and who may CITE it ────────────────────────────────────
#
# `Factor.evidence` answers one question: what switches this factor on, and what
# may be shown to the model when it does. That is OWNERSHIP, and it is right.
#
# It was also being used to answer a second, different question — which factor
# is allowed to CITE a field as justification — and those are not the same
# question. A run of Class 3 Maths against a real school profile proposed nine
# adaptations and refused six, and every one of the six was refused for citing a
# recorded field its factor did not own. None was refused for bad reasoning:
#
#   participation  wanted pair-based exploration, citing that pair work has
#                  historically drawn more of this class in, that the class is
#                  42, and that only pairs fit the fixed rows. Refused: it does
#                  not own `engagement_observations` or `grouping_constraints`.
#   cognitive_load wanted to chunk a concept, citing a wide ability spread and
#                  attention falling off after ten minutes. Refused likewise.
#   resources      wanted to hand out material by pair rather than individually,
#                  citing the fixed rows. Refused likewise.
#
# Each is a sound judgement about the room, and refusing it teaches nobody
# anything. So citation permission is now its own table.
#
# THE ASYMMETRY IS THE POINT, and it is why this is not simply "let any factor
# cite anything". The engagement record belongs to factor 18: only 18 may
# conclude something about how this class engages OVER TIME. But factor 8 may
# read the same record when deciding how people take part TODAY. Ownership
# governs interpretation; citation governs use. A field appears here only when a
# second factor has honest operational use for it.
#
# Room facts stay in `profile.ROOM_FACTS`: they are citable by everything, and
# listing twelve fields against nineteen factors here would say the same thing
# at far greater length.

SHARED_EVIDENCE: dict[str, tuple] = {
    # How the room can be organised. Every factor planning an activity has to
    # respect it; `classroom` is still the only one that interprets it.
    "grouping_constraints": ("resources", "participation", "scaffolding",
                             "psychological_safety", "cognitive_load",
                             "motivation", "language"),

    # What this class has been OBSERVED to do, over time. Owned by 18, which
    # alone may draw a conclusion about the class's engagement as a trait.
    "engagement_observations": ("participation", "classroom", "motivation",
                                "cognitive_load", "scaffolding"),
    "participation_history": ("participation", "psychological_safety",
                              "motivation", "socioeconomic", "prior_knowledge"),

    # Where the class stands. Read by anyone pitching a task at it.
    "class_ability_spread": ("cognitive_load", "prior_knowledge",
                             "participation", "language"),
    "class_readiness_note": ("cognitive_load", "scaffolding", "prior_knowledge",
                             "motivation", "language"),
    "prerequisite_mastery": ("cognitive_load", "scaffolding", "language"),
    "known_weak_topics": ("prior_knowledge", "language", "motivation"),

    # How this teacher runs a period, and whether anyone helps.
    "teaching_style": ("cognitive_load", "classroom", "scaffolding",
                       "participation"),
    "support_staff": ("classroom", "participation", "cognitive_load",
                      "scaffolding"),

    # Language reaches everything a child must read, say or be asked.
    "medium_of_instruction": ("participation", "cognitive_load",
                              "psychological_safety"),
    "home_languages": ("participation", "motivation", "psychological_safety"),
    "literacy_level": ("prior_knowledge", "cognitive_load", "participation",
                       "scaffolding"),

    # Who can take part, and how.
    "accessibility_requirements": ("participation", "classroom", "resources"),
    "response_modes_available": ("participation", "language",
                                 "psychological_safety", "scaffolding"),
    "participation_constraints": ("participation", "psychological_safety",
                                  "classroom", "motivation", "language"),

    # Out-of-school reality, which bounds what a lesson may set.
    "homework_feasible": ("socioeconomic", "scaffolding", "prior_knowledge",
                          "continuity", "family_community"),
    "home_support_available": ("socioeconomic", "scaffolding",
                               "family_community"),
    "household_responsibilities": ("socioeconomic", "continuity",
                                   "psychological_safety"),

    "class_interests": ("motivation", "family_community", "local_relevance"),
}


def may_cite(factor_id: str, field: str) -> bool:
    """May this factor cite this field as justification?

    True when the factor OWNS the field (it is activation evidence) or when the
    field is shared with it above. Says nothing about whether the field is
    recorded, verified, or the factor active — the gate checks each of those
    separately, and folding them together here is what made a routing problem
    look like a data problem.
    """
    factor = BY_ID.get(factor_id)
    if factor is None:
        return False
    if field in factor.evidence:
        return True
    return factor_id in SHARED_EVIDENCE.get(field, ())


def citable_by(factor_id: str) -> frozenset:
    """Every field this factor may cite — owned and shared together."""
    factor = BY_ID.get(factor_id)
    if factor is None:
        return frozenset()
    return frozenset(factor.evidence) | frozenset(
        field for field, consumers in SHARED_EVIDENCE.items()
        if factor_id in consumers)
