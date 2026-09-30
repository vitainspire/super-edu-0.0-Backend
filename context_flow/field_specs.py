"""How each of the fifty-six profile fields should be ASKED for.

`context_flow/factors.py` says which fields exist and which factor reads each
one. It deliberately says nothing about how to collect them, because the
pipeline does not care — a field is a value, a provenance and an origin. A
person filling a form does care, so this module adds the one layer the registry
leaves out: a label, a widget, and a placeholder that shows the shape of a real
answer.

THIS FILE IS NOT A SECOND REGISTRY. It never adds a field, never renames one and
never decides a tier. `studio.py` iterates `factors.FACTORS` and looks each
evidence field up here; anything missing falls back to a text box with a
prettified label, so a field added to the registry tomorrow still appears in the
form. `context_flow/tests/test_field_specs.py` asserts the two stay in step.

WHY PLACEHOLDERS AND NOT DEFAULTS. Every value here would otherwise be a value
somebody did not type. §18's first failure mode is a model handed an empty
community field filling it with a plausible village; a form that pre-fills
"weekly vegetable market" for a school nobody asked commits the same error one
layer earlier, and with more authority, because by then it looks like data.
"""
from __future__ import annotations

from typing import NamedTuple

TEXT, AREA, BOOL, INT, CHOICE = "text", "area", "bool", "int", "choice"


class Spec(NamedTuple):
    label: str
    kind: str = TEXT
    help: str = ""
    placeholder: str = ""
    choices: tuple = ()
    min: int = 0
    max: int = 200
    default: int = 0


SPECS = {
    # ── Node 1's four, plus the two preferences it also reads ────────────────
    "class_size": Spec(
        "Class size", INT, "Learners on the roll for this period.",
        min=1, max=200, default=40),
    "lesson_duration": Spec(
        "Lesson duration (minutes)", INT, "One period, as timetabled.",
        min=10, max=120, default=30),
    "medium_of_instruction": Spec(
        "Medium of instruction", TEXT, "The language the lesson is taught in.",
        placeholder="English"),
    "resource_level": Spec(
        "Resource level", CHOICE,
        "0 = board and chalk only, 3 = a well-equipped room.",
        choices=("0", "1", "2", "3")),
    "teaching_style": Spec(
        "Teaching style", TEXT, "How this teacher prefers to run a period.",
        placeholder="demonstration then paired practice"),
    "learning_objective": Spec(
        "Learning objective override", AREA,
        "Leave empty unless this class needs a different objective from the "
        "one the chapter implies."),

    # ── 4. Language & Communication (baseline) ───────────────────────────────
    "home_languages": Spec(
        "Home languages", TEXT, "What learners speak outside school.",
        placeholder="Telugu"),
    "teacher_languages": Spec(
        "Teacher's languages", TEXT, "Languages this teacher can bridge into.",
        placeholder="Telugu, English"),
    "literacy_level": Spec(
        "Reading level", TEXT, "Where this class actually reads, not the grade.",
        placeholder="most read simple sentences; six are pre-reading"),
    "oral_language_strength": Spec(
        "Oral language strength", TEXT,
        "Speaking and listening, which often runs ahead of reading.",
        placeholder="strong in Telugu, hesitant in English"),

    # ── 5. Socioeconomic & Household (baseline) ──────────────────────────────
    "homework_feasible": Spec(
        "Homework is feasible", BOOL,
        "Can work sent home realistically be done and returned?"),
    "household_responsibilities": Spec(
        "Household responsibilities", AREA,
        "Out-of-school demands on learners' time.",
        placeholder="older children mind siblings until dusk"),
    "home_support_available": Spec(
        "Home support available", TEXT,
        "Is there an adult who can help with school work?",
        placeholder="few homes have a literate adult free in the evening"),

    # ── 6. Resource & Infrastructure (baseline) ──────────────────────────────
    "materials_available": Spec(
        "Materials available", AREA,
        "What is genuinely in the room — the Challenge is planned against this.",
        placeholder="blackboard, chalk, slates, a bag of tamarind seeds"),
    "has_board": Spec("Has a usable board", BOOL),
    "has_paper": Spec("Paper is available", BOOL),
    "classroom_space": Spec(
        "Classroom space", TEXT, "Room to move, or desks fixed in rows?",
        placeholder="benches fixed in rows, a narrow aisle"),
    "electricity": Spec("Reliable electricity", BOOL),

    # ── 7. Technology Access (baseline) ──────────────────────────────────────
    "devices_available": Spec(
        "Devices available", TEXT, placeholder="one shared tablet"),
    "has_projector": Spec("Has a projector", BOOL),
    "has_internet": Spec("Has internet", BOOL),
    "digital_literacy": Spec(
        "Digital literacy", TEXT,
        "Of the teacher and the class — a device nobody can drive is not a "
        "resource.", placeholder="teacher confident, learners unfamiliar"),

    # ── 8. Learner Access & Participation (baseline) ─────────────────────────
    "accessibility_requirements": Spec(
        "Accessibility requirements", AREA,
        "Named needs that change how a learner can take part.",
        placeholder="one learner has low vision and sits at the front"),
    "response_modes_available": Spec(
        "Ways learners can respond", TEXT,
        "Speaking, slate, gesture, pointing — what the lesson may ask for.",
        placeholder="speaking and slate work; little writing at length"),
    "participation_constraints": Spec(
        "Participation constraints", AREA,
        placeholder="girls seldom volunteer answers in front of the class"),

    # ── 9. Prior Knowledge & Readiness (baseline) ────────────────────────────
    "known_weak_topics": Spec(
        "Known weak topics", AREA,
        "What this class has NOT secured — the refresher is planned from it.",
        placeholder="place value beyond 99; subtraction with borrowing"),
    "prerequisite_mastery": Spec(
        "Prerequisite mastery", TEXT,
        "What the chapter assumes, and whether they have it.",
        placeholder="counting to 100 secure; comparing two-digit numbers shaky"),
    "class_readiness_note": Spec(
        "Readiness note", AREA, "Anything else about where this class stands."),

    # ── 11. Scaffolding & Differentiation (baseline) ─────────────────────────
    "class_ability_spread": Spec(
        "Ability spread", TEXT,
        placeholder="wide — eight are a year ahead, five are a year behind"),
    "support_staff": Spec(
        "Support staff", TEXT, "Another adult in the room, if any.",
        placeholder="none"),

    # ── 12. Classroom Environment (baseline) ─────────────────────────────────
    "grouping_constraints": Spec(
        "Grouping constraints", TEXT,
        "What group work the room and the furniture actually permit.",
        placeholder="pairs only; benches cannot be moved"),
    "seating": Spec(
        "Seating", TEXT, placeholder="three to a bench, facing the board"),

    # ── 14. Motivation & Agency (baseline) ───────────────────────────────────
    "class_interests": Spec(
        "Class interests", AREA,
        "What this particular class leans into.",
        placeholder="cricket scores, the weekly market, animals"),

    # ── 1. Cultural & Community Context (LOCAL — verified only) ──────────────
    "community_practices": Spec(
        "Community practices", AREA,
        placeholder="most families farm; harvest is shared work"),
    "local_traditions": Spec(
        "Local traditions", AREA, placeholder="Sankranti kite-flying"),
    "community_events": Spec(
        "Community events", AREA, placeholder="the Tuesday cattle fair"),
    "family_structures": Spec(
        "Family structures", AREA,
        placeholder="mostly joint families; grandparents at home in the day"),

    # ── 2. Funds of Knowledge (LOCAL) ────────────────────────────────────────
    "household_skills": Spec(
        "Household skills", AREA,
        "What learners already do competently at home.",
        placeholder="children weigh and bag vegetables for family stalls"),
    "community_occupations": Spec(
        "Community occupations", AREA,
        placeholder="farming, shopkeeping, weaving"),
    "local_tools": Spec(
        "Local tools", AREA,
        placeholder="balance scales, measuring pots, the seed drill"),
    "craft_practices": Spec(
        "Craft practices", AREA, placeholder="handloom weaving, pot-making"),

    # ── 3. Cultural Representation (LOCAL) ───────────────────────────────────
    "community_profile": Spec(
        "Community profile", AREA,
        "Who this community is, in the community's own terms."),
    "represented_identities": Spec(
        "Identities to represent", AREA,
        "Who should appear in examples so learners see themselves."),
    "verified_cultural_practices": Spec(
        "Verified cultural practices", AREA,
        "Practices confirmed with families, not assumed from the region."),

    # ── 15. Local Relevance (LOCAL) ──────────────────────────────────────────
    "local_markets": Spec(
        "Local markets", AREA,
        "Where number, measure and money are already used.",
        placeholder="weekly vegetable market on Tuesdays"),
    "local_phenomena": Spec(
        "Local phenomena", AREA,
        placeholder="the tank fills in July and dries by March"),
    "local_transport": Spec(
        "Local transport", AREA, placeholder="bullock carts, one bus at 7am"),
    "community_problems": Spec(
        "Community problems", AREA,
        "Real local problems a lesson could legitimately work on.",
        placeholder="the drinking-water queue at the single tap"),

    # ── 16. Family & Community Connection (LOCAL) ────────────────────────────
    "community_assets": Spec(
        "Community assets", AREA,
        placeholder="the panchayat library, the temple courtyard"),
    "local_role_models": Spec(
        "Local role models", AREA,
        placeholder="the village nurse; a woman who runs the seed shop"),

    # ── 17. Attendance & Continuity (LONGITUDINAL — measured only) ───────────
    "attendance_history": Spec(
        "Attendance history", AREA,
        "Measured, from a register — not recalled.",
        placeholder="82% average over the last term"),
    "missed_topics": Spec(
        "Missed topics", AREA,
        "Topics specific learners were absent for.",
        placeholder="six learners missed the place-value week in July"),
    "seasonal_absence": Spec(
        "Seasonal absence", AREA,
        placeholder="attendance drops through the cotton-picking weeks"),

    # ── 18. Learner Engagement Profile (LONGITUDINAL) ────────────────────────
    "engagement_observations": Spec(
        "Engagement observations", AREA,
        "Recorded observations across lessons, not an impression of one.",
        placeholder="paired slate work sustained attention in 4 of 5 lessons"),
    "participation_history": Spec(
        "Participation history", AREA,
        placeholder="logged over six weeks: the same nine answer aloud"),
    "intervention_history": Spec(
        "Intervention history", AREA,
        "What has already been tried, and what it did.",
        placeholder="remedial reading group since August; two moved up a band"),
}


def spec_for(field: str) -> Spec:
    """The Spec for a field, or a sensible text box for one not described here.

    A registry field with no Spec must still be collectable — otherwise adding a
    factor to `factors.py` silently makes part of the profile unreachable from
    the form, and the failure is invisible because the form still renders.
    """
    found = SPECS.get(field)
    if found:
        return found
    return Spec(field.replace("_", " ").capitalize(), TEXT)
