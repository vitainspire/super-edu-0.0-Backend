"""
Test harness for the Prep Material generator (frontend/app/api/smart-lesson/route.ts).

Ports that route's prompt-building logic 1:1 into Python so you can iterate on
different Teaching Profile x grade x topic combinations from the command line,
without going through the app UI or a live Supabase database (topicDefinitionId
grounding and marks-based weak-topic detection are optional/mockable here).

Usage
-----
List the built-in profile presets:
    python test_prep_material.py --list-profiles

Single run:
    python test_prep_material.py --grade 5 --subject Mathematics --topic "Fractions" --profile storyteller

Compare every preset against the same topic (great for eyeballing how much the
flow actually changes shape across profiles):
    python test_prep_material.py --grade 5 --subject Mathematics --topic "Fractions" --compare

Matrix run — every combination of grades x topics x profiles:
    python test_prep_material.py --grades 3,5,8 --subjects Mathematics,Science --topics "Fractions,Photosynthesis" --compare

Each run is printed to the console AND saved as a .json + .md file under --out-dir
(default ./prep_material_test_output) so you can diff outputs across runs later.

Custom profile (bypass the presets):
    python test_prep_material.py --grade 5 --topic Fractions --profile custom \\
        --roles Mentor,Guide --goals "Build confidence,Think critically" \\
        --activities Stories,Games --comfort "Speaking in front of the class" \\
        --emphasize stories,realLife --minimize reflection

Add --images to also generate the per-bullet board-sketch (slower, costs more —
off by default so you can iterate on text quickly).
"""

import argparse
import itertools
import json
import os
import random
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import requests

# Windows terminals default to cp1252 and choke on em-dashes / non-ASCII AI output
# (e.g. a Telugu/Hindi topic) — force UTF-8 so nothing crashes or prints garbled text.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

# ── Env loading — mirrors the real route: frontend/.env.local first (that's
# where Next.js actually reads OPENROUTER_API_KEY/MODEL from), backend/.env as
# a fallback in case only the backend was configured. ───────────────────────
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent

try:
    from dotenv import load_dotenv
    load_dotenv(_REPO_ROOT / "frontend" / ".env.local")
    load_dotenv(_REPO_ROOT / "backend" / ".env")  # won't override already-set vars
except ImportError:
    print("[warn] python-dotenv not installed — relying on already-exported env vars.", file=sys.stderr)

OPENROUTER_API_KEY = os.environ.get("OPENROUTER_API_KEY")
OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_IMAGE_MODEL = os.environ.get("OPENROUTER_IMAGE_MODEL", "google/gemini-2.5-flash-image")
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"


# ── Teaching Profile shape — mirrors frontend/lib/types.ts ──────────────────

FREQUENCIES = ("never", "sometimes", "often", "always")

PERSONALIZATION_KEYS = (
    "stories", "games", "handsOn", "criticalThinking", "creativity",
    "realLife", "localCulture", "reflection", "exploration",
)

PERSONALIZATION_LABELS = {
    "stories": "storytelling",
    "games": "games",
    "handsOn": "hands-on activities",
    "criticalThinking": "critical thinking",
    "creativity": "creativity",
    "realLife": "real-life connections",
    "localCulture": "local culture references",
    "reflection": "student reflection",
    "exploration": "independent exploration",
}


@dataclass
class TeachingProfile:
    # classroom
    language: list = field(default_factory=list)
    class_size: str = ""  # '' | '<20' | '20-40' | '>40'
    resources: list = field(default_factory=list)
    # teacherIdentity
    roles: list = field(default_factory=list)
    goals: list = field(default_factory=list)
    preferred_activities: list = field(default_factory=list)
    comfort_zones: list = field(default_factory=list)
    # personalization — every key defaults to 'sometimes', same as EMPTY_TEACHING_PROFILE
    personalization: dict = field(default_factory=lambda: {k: "sometimes" for k in PERSONALIZATION_KEYS})


def personalization_emphasis(personalization: dict) -> tuple[list, list]:
    """Mirrors lib/logic/teaching-profile.ts's personalizationEmphasis()."""
    emphasize, minimize = [], []
    for key in PERSONALIZATION_KEYS:
        value = personalization.get(key, "sometimes")
        if value in ("often", "always"):
            emphasize.append(PERSONALIZATION_LABELS[key])
        elif value == "never":
            minimize.append(PERSONALIZATION_LABELS[key])
    return emphasize, minimize


def _freq_map(**overrides) -> dict:
    p = {k: "sometimes" for k in PERSONALIZATION_KEYS}
    p.update(overrides)
    return p


# ── Built-in presets — pick a few distinct, recognisable teacher archetypes ──

PRESET_PROFILES: dict[str, Optional[TeachingProfile]] = {
    "none": None,  # no profile on file at all — the real "before onboarding" state

    "storyteller": TeachingProfile(
        roles=["Mentor", "Guide"],
        goals=["Stay curious", "Apply learning to real life"],
        preferred_activities=["Stories", "Discussions"],
        comfort_zones=["Speaking in front of the class", "Improvising activities"],
        resources=["Chalkboard", "Worksheets"],
        class_size="20-40",
        language=["English"],
        personalization=_freq_map(stories="always", games="never", realLife="often", reflection="often"),
    ),

    "hands_on": TeachingProfile(
        roles=["Guide", "Educator"],
        goals=["Understand deeply", "Work together"],
        preferred_activities=["Group Activities", "Experiments", "Real-life Examples"],
        comfort_zones=["Moving around the classroom", "Improvising activities"],
        resources=["Activity Materials", "Outdoor Space", "Science Kit"],
        class_size="<20",
        language=["English"],
        personalization=_freq_map(handsOn="always", stories="never", exploration="often"),
    ),

    "playful_games": TeachingProfile(
        roles=["Influencer", "Counselor"],
        goals=["Stay curious", "Build confidence", "Be creative"],
        preferred_activities=["Games", "Role Play", "Art & Creativity"],
        comfort_zones=["Using technology", "Drawing on the board"],
        resources=["Whiteboard", "Projector", "Internet"],
        class_size=">40",
        language=["Bilingual"],
        personalization=_freq_map(games="always", creativity="often", criticalThinking="sometimes"),
    ),

    "critical_thinker": TeachingProfile(
        roles=["Educator"],
        goals=["Think critically", "Communicate better"],
        preferred_activities=["Discussions", "Demonstrations"],
        comfort_zones=["Speaking in front of the class", "Group discussions"],
        resources=["Chalkboard", "Whiteboard"],
        class_size="20-40",
        language=["English"],
        personalization=_freq_map(criticalThinking="always", reflection="often", games="never", stories="never"),
    ),

    "quiet_reflective": TeachingProfile(
        roles=["Counselor"],
        goals=["Discover their interests", "Understand deeply"],
        preferred_activities=["Discussions"],
        comfort_zones=["Group discussions"],
        resources=["Worksheets"],
        class_size="<20",
        language=["Telugu"],
        personalization=_freq_map(reflection="always", exploration="often", games="never", handsOn="never"),
    ),
}


def list_profiles() -> None:
    print("Available --profile presets:\n")
    for name, p in PRESET_PROFILES.items():
        if p is None:
            print(f"  {name:<18} (no Teaching Profile on file — the pre-onboarding fallback)")
            continue
        emph, mini = personalization_emphasis(p.personalization)
        print(f"  {name:<18} roles={p.roles} activities={p.preferred_activities}")
        print(f"  {'':<18} emphasize={emph} minimize={mini}")
    print("\n  custom             build one from --roles/--goals/--activities/--comfort/--emphasize/--minimize")


# ── Real wizard option pools — verbatim from frontend/app/(dashboard)/profile/
# teaching/page.tsx, so generated combos are things a teacher could actually
# select, not invented values. ───────────────────────────────────────────────

LANGUAGE_OPTIONS = ["English", "Telugu", "Hindi", "Bilingual"]
CLASS_SIZE_OPTIONS = ["<20", "20-40", ">40"]
RESOURCE_OPTIONS = [
    "Chalkboard", "Whiteboard", "Projector", "Internet",
    "Worksheets", "Activity Materials", "Outdoor Space", "Science Kit",
]
ROLE_OPTIONS = ["Educator", "Mentor", "Guide", "Influencer", "Counselor"]  # choose up to 2
GOAL_OPTIONS = [
    "Understand deeply", "Stay curious", "Think critically", "Build confidence",
    "Communicate better", "Work together", "Be creative",
    "Apply learning to real life", "Discover their interests",
]  # choose up to 3
ACTIVITY_OPTIONS = [
    "Stories", "Games", "Group Activities", "Discussions", "Demonstrations",
    "Real-life Examples", "Art & Creativity", "Experiments", "Role Play",
]  # choose up to 4
COMFORT_OPTIONS = [
    "Speaking in front of the class", "Group discussions", "Moving around the classroom",
    "Improvising activities", "Drawing on the board", "Using technology",
]

_CLASS_SIZE_TO_NUMBER = {"<20": 18, "20-40": 32, ">40": 48}


def generate_combo_profiles(n: int, seed: int = 42) -> dict:
    """Builds n realistic Teaching Profiles by sampling the REAL wizard option
    pools (same lists a teacher actually picks from), instead of hand-picked
    archetypes. Each combo also gets a guaranteed, visible personalization
    signature (2-3 emphasized + 1-2 minimized dimensions) — fully random
    'sometimes' everywhere would produce a bland, uninformative profile and
    defeat the point of comparing outputs for fine-tuning."""
    rng = random.Random(seed)
    profiles = {}
    for i in range(1, n + 1):
        roles = rng.sample(ROLE_OPTIONS, k=rng.choice([1, 2]))
        goals = rng.sample(GOAL_OPTIONS, k=rng.choice([2, 3]))
        activities = rng.sample(ACTIVITY_OPTIONS, k=rng.choice([2, 3, 4]))
        comfort = rng.sample(COMFORT_OPTIONS, k=rng.choice([1, 2, 3]))
        resources = rng.sample(RESOURCE_OPTIONS, k=rng.choice([2, 3, 4]))
        language = [rng.choice(LANGUAGE_OPTIONS)]
        class_size = rng.choice(CLASS_SIZE_OPTIONS)

        shuffled_keys = list(PERSONALIZATION_KEYS)
        rng.shuffle(shuffled_keys)
        num_emph = rng.choice([2, 3])
        num_min = rng.choice([1, 2])
        emphasized = shuffled_keys[:num_emph]
        minimized = shuffled_keys[num_emph:num_emph + num_min]
        personalization = {k: "sometimes" for k in PERSONALIZATION_KEYS}
        for k in emphasized:
            personalization[k] = rng.choice(["often", "often", "always"])  # 'often' more common than 'always'
        for k in minimized:
            personalization[k] = "never"

        tag = f"emphasizes {'+'.join(PERSONALIZATION_LABELS[k] for k in emphasized)}, minimizes {'+'.join(PERSONALIZATION_LABELS[k] for k in minimized)}"
        profiles[f"combo_{i} ({tag})"] = TeachingProfile(
            roles=roles, goals=goals, preferred_activities=activities, comfort_zones=comfort,
            resources=resources, language=language, class_size=class_size, personalization=personalization,
        )
    return profiles


# ── Prompt construction — ports the TS route's groundingContext /
# preferencesContext / weakTopicsContext / contextNoteLine / systemPrompt /
# userPrompt verbatim. ───────────────────────────────────────────────────────

@dataclass
class Grounding:
    chapter_title: Optional[str] = None
    page_start: Optional[int] = None
    page_end: Optional[int] = None
    exercises: list = field(default_factory=list)   # [{text, type}]
    sidebars: list = field(default_factory=list)    # [str]


def build_grounding_context(grounding: Optional[Grounding]) -> str:
    if not grounding:
        return ("No textbook extraction is available — use standard grade-appropriate concepts, nothing exotic. "
                "This says nothing about materials — the \"materials\" list is governed entirely by the teacher's "
                "profile below (or, only if no profile is on file, generic locally-available items like slate/chalk).")
    lines = ["This topic comes from an actual textbook chapter that has already been analysed. "
             "Ground the Concept bullets and flow in this REAL content instead of inventing generic material:"]
    if grounding.chapter_title:
        pages = f" (pages {grounding.page_start}-{grounding.page_end or grounding.page_start})" if grounding.page_start else ""
        lines.append(f'Chapter: "{grounding.chapter_title}"{pages}')
    if grounding.exercises:
        ex_lines = "\n".join(f"- [{e['type']}] {e['text']}" for e in grounding.exercises)
        lines.append(f"\nActual exercises/activities in the textbook for this topic:\n{ex_lines}")
    if grounding.sidebars:
        sb_lines = "\n".join(f"- {s}" for s in grounding.sidebars)
        lines.append(f"\nActual sidebar notes/tips printed alongside this topic:\n{sb_lines}")
    return "\n".join(lines)


def build_preferences_context(profile: Optional[TeachingProfile]) -> str:
    if profile is None:
        return "No Teaching Profile on file yet — default to a simple, universally comfortable activity (storytelling + pair work)."

    emphasize, minimize = personalization_emphasis(profile.personalization)
    parts = []
    if profile.roles: parts.append(f"Sees themselves as: {', '.join(profile.roles)}.")
    if profile.goals: parts.append(f"Wants students to: {', '.join(profile.goals)}.")
    if profile.preferred_activities: parts.append(f"Enjoys using: {', '.join(profile.preferred_activities)}.")
    if profile.comfort_zones: parts.append(f"Comfortable with: {', '.join(profile.comfort_zones)}.")
    if profile.class_size: parts.append(f"Typical class size: {profile.class_size}.")
    if profile.resources: parts.append(f"Classroom has: {', '.join(profile.resources)}.")
    if profile.language: parts.append(f"Classroom language: {', '.join(profile.language)}.")
    if emphasize: parts.append(f"Lean into: {', '.join(emphasize)}.")
    if minimize: parts.append(f"Minimize: {', '.join(minimize)}.")
    lines = " ".join(parts)

    if profile.resources:
        resource_rule = (f'"materials" must be chosen ONLY from this exact list: {", ".join(profile.resources)} — '
                         f'never add chalk, slate, paper, or any other item not on this list, even if it seems '
                         f'like a harmless default.')
    else:
        resource_rule = 'No resources were listed for this classroom — use only the single most generic, universally-available item (e.g. chalkboard) and nothing else.'

    return (f'This teacher\'s profile — {lines} The flow MUST reflect this profile — its shape, order, and stage '
            f'count, not just a swapped noun; it is the single biggest personalization signal you have. '
            f'{resource_rule} Never suggest something outside the teacher\'s comfort zone.')


# ── Low-resource activity bank + new "story" template ───────────────────────
# Curated by hand for government/NGO schools with minimal supplies. Kept as
# plain reference text (NOT a structured/queryable library) — the model picks
# and adapts an activity using its own judgment, the code never filters it.

LOW_RESOURCE_PRINCIPLES = """\
This school has minimal resources. Every activity must work with:
- No printers, no projectors, no smart boards, no photocopies.
- Only chalk, blackboard, notebooks/paper, or everyday found objects: stones, sticks, bottle caps, old newspapers -- or the students' own bodies as the "material".
- 5 to 15 minutes to run.
- A class of 30 to 60 students.
- Something a single teacher can set up and facilitate alone, with no prep the night before."""

ACTIVITY_BANK = """\
Pick ONE activity for "interactiveExploration" and a DIFFERENT one for "challenge" from this bank -- adapt the specifics (numbers, wording) to the actual topic, don't invent a new activity from scratch unless truly nothing here fits.

MOVEMENT (best for: place value, number line, comparing numbers, fractions, geometry)
- Human Number Machine: each student holds a digit card, the group arranges itself into a called number; swap two students and ask what changed.
- Living Number Line: a line taped/drawn on the floor; students stand where their number card belongs.
- Skip Counting Jump Path: numbers marked on the floor; students hop by 2s/5s/10s, or avoid a rule ("don't land on multiples of 4").
- Human Bar Graph: students physically stand in columns by their answer to a question; the class becomes the graph.
- Human Calculator: some students are numbers, one is an operator (+/-); they physically act out the calculation.
- Freeze Frame: students form a math symbol or relationship with their bodies (e.g. two numbers and a student as ">" between them).
- Human Building Blocks: students organize themselves to "build" a number the teacher calls out.
- Four Corners: label room corners Agree/Disagree/Not Sure; students move to the corner matching their view on a statement.
- Pattern Dance: teacher claps/steps a pattern, students continue it, then invent their own.
- Floor Is Lava (Math version): number cards on the floor; students may only step on ones matching a rule.

MYSTERY (best for: place value, number properties, estimation)
- Secret Number Interview: one student secretly picks a number; others ask only yes/no questions to guess it.
- Mystery Bag: students feel objects without looking and classify them (longer/shorter, more/less, shape).
- Missing Digit Mystery: a partly-hidden number with clues ("greater than 500, even, not divisible by 3") to deduce the missing digit.
- Error Detective: the teacher deliberately makes a mistake out loud; students catch and correct it.
- Guess My Rule: teacher gives a sequence (2, 4, 8, 16...); students guess the rule behind it.
- Classroom CSI: students search for planted "evidence" of an error somewhere in the room.

ROLE PLAY (best for: fractions, money, reading numbers, decimals)
- Fraction Pizza Shop: paper "pizzas"; the teacher orders a fraction amount, students cut and serve it correctly.
- Fraction Chef: students follow a recipe with fraction amounts; teacher gives an intentionally wrong measurement for them to catch.
- Math Restaurant: a menu with prices; students rotate through customer/waiter/cashier roles totalling bills and change.
- Decimal Money Market: a pretend market with decimal prices; students calculate change with play money.
- Become the Teacher: a student teaches the class for two minutes.
- Character Roleplay: a student becomes a themed character (e.g. "Captain Decimal") to narrate a concept.

BUILD & CREATE (best for: shapes, geometry, revision)
- Geometry Architects: using sticks/straws, groups build the strongest triangle or tallest structure, then discuss why it holds.
- Build the Tallest Tower: each correct answer earns a "block" (stone/bottle cap); groups race to build the tallest tower.
- Math Art Gallery: students create art from only basic shapes, then label every shape used.
- Build From Waste: build shapes or models from bottle caps, sticks, or scrap paper.

GAMES (best for: revision and quick assessment)
- Number Auction: students "bid" with play points on mystery number clue cards, then decide if it was worth it.
- Lucky Ticket: a few random students get a bonus challenge question.
- Spin a hand-drawn wheel: it decides HOW to answer (explain out loud / draw it / act it out / solve it).
- Giant Dice Adventure: roll a die, move that many steps on a chalk-drawn floor board; each square is a mini-challenge.

THINKING (best for: reflection and conceptual understanding -- use sparingly if this teacher's profile says to minimize reflection)
- Teach the Teddy: a student explains the idea to a puppet/toy; if it "doesn't understand," they explain differently.
- Hot Seat: one student faces away from the board; the class gives clues about a written number/word for them to guess.
- Whisper Relay: a math statement is whispered student-to-student down a line; compare what arrives at the end.
- Comic Strip: students sketch the day's idea as a 3-4 panel comic on paper."""


def build_story_prompts(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
) -> tuple[str, str]:
    """New 5-part template: Concept -> Real-Life Connection -> Interactive
    Exploration -> Challenge -> Level Set. Activities are picked (not invented)
    from ACTIVITY_BANK; materials default to LOW_RESOURCE_PRINCIPLES unless the
    teacher's own profile lists richer resources (build_preferences_context
    already carries that constraint over from the classic prompt)."""
    preferences_context = build_preferences_context(profile)
    weak_topics_context = (
        f"This class is weak on: {', '.join(weak_topics)}. If it fits naturally, let the real-life "
        f"connection or challenge also reinforce one of these -- but never mention them explicitly or call it review."
        if weak_topics else ""
    )
    context_note_line = f"Teacher's note for today: {context_note.strip()}" if context_note and context_note.strip() else ""
    materials_rule = (
        "the teacher's listed classroom resources above"
        if (profile and profile.resources)
        else "chalk, blackboard, notebooks, or everyday found objects -- nothing else"
    )

    system_prompt = (
        'You are an expert teacher-trainer designing a classroom-ready lesson for a resource-constrained '
        'school (government or NGO-run, India). A teacher opens this five minutes before class and follows '
        'it directly -- every field must be concrete and short enough to scan in seconds, never an essay.\n\n'
        f'{LOW_RESOURCE_PRINCIPLES}\n\n'
        f'{ACTIVITY_BANK}\n\n'
        'Pedagogy: never explain the concept and then test it. Instead, let understanding emerge through '
        'the activity and a few open questions -- the teacher guides, the students discover. Questions inside '
        'an activity are wondered aloud WITH the class ("What changed?", "Is it still worth the same?"), never '
        'a stop-and-answer quiz. The lesson must open with a real-life scenario the class recognizes (not '
        '"today we will learn X"), move through ONE chosen activity to explore the idea, apply it again with a '
        'second, different chosen activity as a playful challenge, then return to the SAME opening scenario so '
        'students can now solve it -- math must feel connected back to life, not abandoned once the activity ends.'
    )

    materials_example = (
        "Example of the materials boundary (illustration only, not this teacher's actual list):\n"
        '  BAD: a step says "project the diagram" or "look it up online" -- invents equipment not on the list.\n'
        '  GOOD: a step says "draw the diagram on the chalkboard" or "students sketch it in their notebooks."'
    )
    reminder_block = (
        "REMINDER before you write the JSON -- the rules most likely to get dropped:\n"
        f"- materialsUsed must contain nothing beyond {materials_rule}. Do not invent a projector, printer, internet, "
        "or any item not on that list, even implicitly inside a step's wording.\n"
        '- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite" anywhere in the output.\n'
        "- concept is EXACTLY 2 or 3 bullets -- not 1, not 4."
    )

    subtopic_line = f"\nSubtopic (focus specifically on this): {subtopic}" if subtopic else ""
    user_prompt = f"""Write a Prep Sheet for:

Topic: {topic}{subtopic_line}
Subject: {subject}
Grade: {grade}
Class size: {class_size if class_size else 'unknown'}

{preferences_context}
{weak_topics_context}
{context_note_line}

{materials_example}

{reminder_block}

Return ONLY valid JSON (no markdown, no extra text), matching this exact shape:
{{
  "planningNote": "1-2 sentences of YOUR OWN reasoning, written first, before anything else: given this profile, what angle/shape should this lesson take, and which two activities fit best and why? Internal use only -- never shown to the teacher.",
  "concept": ["2 to 3 short bullets introducing the idea in the simplest possible way -- no paragraphs"],
  "realLifeConnection": "A short (2-3 sentence) scenario from the child's world that makes them curious about this topic BEFORE any teaching happens -- a specific, concrete situation, not an abstract prompt like 'today we will learn...'.",
  "interactiveExploration": {{
    "activity": "the exact name of ONE activity chosen from the bank above",
    "steps": ["2 to 4 short steps describing exactly how this activity plays out for THIS topic -- adapt the numbers/wording, don't just restate the generic activity"],
    "guidingQuestions": ["1 to 3 questions the teacher asks mid-activity so the concept emerges from discussion, e.g. 'What changed?', 'Is it still worth the same?'"]
  }},
  "challenge": {{
    "activity": "the exact name of a DIFFERENT activity chosen from the bank above, used to apply/stretch the idea",
    "steps": ["2 to 3 short steps for how this plays out for THIS topic"]
  }},
  "materialsUsed": ["every item actually referenced across interactiveExploration/challenge steps -- nothing invented, nothing unused"],
  "levelSet": {{
    "returnToScenario": "One line bringing back the exact opening real-life scenario",
    "questions": ["2 to 3 short questions that let students show they can now read/write/compare/explain the idea, tied to that scenario"],
    "extendPrompt": "One open-ended line inviting students to think of another place this idea shows up in their own life"
  }}
}}

Rules:
- planningNote is written FIRST, before concept -- think it through, then commit.
- concept: exactly 2 or 3 bullets, one short sentence each.
- realLifeConnection must be something a child in this context would actually recognize (a market, a bus, a cricket match, a school event) -- not a generic word problem.
- interactiveExploration and challenge MUST use two DIFFERENT named activities from the bank, each adapted with topic-specific details (real numbers/words for this topic, not placeholders).
- Every step in interactiveExploration/challenge must only need {materials_rule}. materialsUsed must list exactly what was actually used -- nothing more.
- levelSet must reference the SAME scenario from realLifeConnection, not a new one.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- Everything must be scannable in under 2 minutes total."""

    return system_prompt, user_prompt


def build_prompts(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
    grounding: Optional[Grounding] = None,
) -> tuple[str, str]:
    grounding_context = build_grounding_context(grounding)
    preferences_context = build_preferences_context(profile)
    weak_topics_context = (
        f"This class is weak on: {', '.join(weak_topics)}. If it fits naturally, let the flow's "
        f"real-world framing also reinforce one of these — but never mention them explicitly or call it review."
        if weak_topics else ""
    )
    context_note_line = f"Teacher's note for today: {context_note.strip()}" if context_note and context_note.strip() else ""

    system_prompt = (
        'You are an expert classroom teacher and instructional designer. You write extremely concise, '
        'classroom-ready prep sheets — never essays, never quiz questions, never generic filler. A teacher '
        'opens this five minutes before class and scans it; every field is short enough to read in seconds.\n\n'
        'Core belief: the teacher isn\'t trying to "engage" students — they\'re creating one concrete '
        'experience through which understanding naturally happens. Every "detail" in the flow must BE that '
        'experience (a scene, an activity, a task), not a description of teaching technique, and it must '
        'contain zero questions like "What is X?".\n\n'
        'Two different teachers with different profiles teaching the same topic should produce lessons that '
        'feel designed by two different people — not the same activity with one noun swapped. The teacher\'s '
        'roles, goals, and preferred activities should change the actual sequence and shape of the lesson '
        '(what happens first, how it\'s framed, how it builds), not just decorate a fixed structure with a '
        'matching word. A storyteller-teacher\'s lesson doesn\'t just "add a story" to the same three steps — '
        'it moves through the topic AS a story. A debate-lover\'s lesson moves through it as a disagreement to '
        'resolve. A puzzle-lover\'s lesson moves through it as a mystery to crack.\n\n'
        'Materials are load-bearing, not decorative. "materials" must be a strict subset of what the teacher\'s '
        'classroom actually has (never invent equipment they don\'t have) — and the flow must visibly follow '
        'from what\'s on that list: a projector/internet available should get used for something a chalkboard '
        'can\'t do; an outdoor space available should get used for something a desk can\'t do; if the profile '
        'says to minimize hands-on activities, no flow stage may involve physical manipulation, building, '
        'moving materials, or walking around — use verbal, visual, or written stages instead. If you remove a '
        'resource from the list, the flow you\'d write should visibly change.\n\n'
        'Vary the shape of the lesson itself. Do not default to a fixed "Hook → Teach → Activity → Wrap-up" '
        'template every time — sometimes 3 stages fit better, sometimes 5; sometimes the right opening is a '
        'story, sometimes a demonstration, sometimes a challenge, sometimes a puzzle. Choose stage count, '
        'order, and names based on what this specific teacher profile would actually do, not a template.\n\n'
        'Talking points are not a quiz round. Students should never feel checked, tested, or called on to '
        'produce a correct answer — that turns the lesson into an evaluation, which is exactly what this is '
        'not. Instead, each talking point is a moment where the teacher wonders something out loud WITH the '
        'class, mid-activity, the way a curious person thinks aloud ("Hmm, I wonder if...", "Notice that...", '
        '"What would happen if..."). If the class is quiet or unsure, the teacher\'s next move is to keep '
        'exploring together, not to mark the silence as a wrong answer or supply "the correct answer" like an '
        'answer key.'
    )

    subtopic_line = f"\nSubtopic (focus specifically on this): {subtopic}" if subtopic else ""
    user_prompt = f"""Write a Prep Sheet for:

Topic: {topic}{subtopic_line}
Subject: {subject}
Grade: {grade}
Class size: {class_size if class_size else 'unknown'}

{grounding_context}

{preferences_context}
{weak_topics_context}
{context_note_line}

Return ONLY valid JSON (no markdown, no extra text), matching this exact shape:
{{
  "goal": "One sentence: what students should understand by the end of this lesson.",
  "materials": ["only items drawn from the teacher's actual classroom resources above (or generic locally-available items like slate/chalk/pebbles if no profile is on file) — nothing invented, nothing the flow doesn't actually use"],
  "concept": [
    {{ "text": "one syllabus-aligned idea, no fluff, one short sentence" }},
    {{ "text": "..." }},
    {{ "text": "..." }}
  ],
  "flow": [
    {{ "label": "a stage name YOU choose to fit this teacher and this lesson (not always Hook/Teach/Activity/Wrap-up)", "minutes": 2, "detail": "direct instructions to the teacher for this stage — what THEY do and say, one to two short sentences" }},
    {{ "label": "...", "minutes": 8, "detail": "..." }},
    {{ "label": "...", "minutes": 10, "detail": "..." }}
  ],
  "talkingPoints": [
    {{ "say": "something to wonder aloud WITH the class mid-activity — curious, informal, never a stop-and-answer quiz question", "keepGoing": "how to keep the moment alive if the class is quiet or unsure — never framed as marking their answer right or wrong" }},
    {{ "say": "...", "keepGoing": "..." }}
  ],
  "differentiation": {{
    "ifStruggling": "a concrete simpler version of the SAME flow activity, using only the materials listed, for students who are lost",
    "ifAhead": "a concrete harder extension for students who finish early"
  }},
  "watchFor": "the single biggest mistake or misconception to watch for while students do the flow activity"
}}

Rules:
- "flow" has 3 to 5 stages — choose the count, order, and labels to fit this profile; do not always produce the same stage names or the same number of stages across different teachers/topics.
- Every flow stage's "detail" must only require items in "materials". No stage may need equipment not listed there.
- If the profile says to minimize hands-on activities, zero flow stages may involve physical manipulation, building, or moving around the room.
- Exactly 3 concept bullets. Each MUST include at least one (up to two) of these extra keys — every bullet is expandable in the UI, so never leave one with none: "deeperExplanation", "misconception", "realLifeExample", "visualDemo". Pick whichever genuinely fits that specific bullet best.
- "talkingPoints": exactly 2 or 3. Each "say" is woven into the flow's activity, phrased like the teacher wondering aloud with the class ("I wonder if...", "Notice how...", "What do you think happens if...") — never a direct interrogation like "What is X?" and never something that singles out one student to answer correctly. "keepGoing" is a way to sustain the moment, not a marking scheme.
- "watchFor" is specific to the flow's activity, not a restatement of a concept bullet's misconception.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- Everything must be scannable in under 2 minutes total — short sentences, no paragraphs."""

    return system_prompt, user_prompt


# ── OpenRouter calls ─────────────────────────────────────────────────────────

def call_openrouter_text(system_prompt: str, user_prompt: str) -> str:
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set — check frontend/.env.local or backend/.env")
    resp = requests.post(
        _OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": "EduTeach Prep Material (test harness)",
        },
        json={
            "model": OPENROUTER_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": 0.75,
            "max_tokens": 1200,
        },
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def call_openrouter_messages(messages: list) -> str:
    """Same as call_openrouter_text but takes a full message list — used by the
    hierarchical pipeline, where each stage's user turn is appended to a growing
    conversation so the model has native, verbatim access to what it said in
    earlier stages instead of us re-summarizing between calls."""
    if not OPENROUTER_API_KEY:
        raise RuntimeError("OPENROUTER_API_KEY not set — check frontend/.env.local or backend/.env")
    resp = requests.post(
        _OPENROUTER_URL,
        headers={
            "Authorization": f"Bearer {OPENROUTER_API_KEY}",
            "Content-Type": "application/json",
            "X-Title": "EduTeach Prep Material (test harness)",
        },
        json={"model": OPENROUTER_MODEL, "messages": messages, "temperature": 0.75, "max_tokens": 1200},
        timeout=90,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"]


def generate_illustration(prompt: str, timeout_s: int = 25) -> Optional[str]:
    """Mirrors frontend/lib/ai.ts's generateIllustration() — returns an image URL or None."""
    if not OPENROUTER_API_KEY:
        return None
    try:
        resp = requests.post(
            _OPENROUTER_URL,
            headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"},
            json={
                "model": OPENROUTER_IMAGE_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "modalities": ["image", "text"],
            },
            timeout=timeout_s,
        )
        if not resp.ok:
            return None
        data = resp.json()
        url = data.get("choices", [{}])[0].get("message", {}).get("images", [{}])[0].get("image_url", {}).get("url")
        return url if isinstance(url, str) and url else None
    except Exception:
        return None


def _extract_json(raw: str) -> dict:
    cleaned = re.sub(r"^```json\s*", "", raw.strip(), flags=re.I)
    cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.I).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if match:
            return json.loads(match.group(0))
        raise


def generate_lesson(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
    grounding: Optional[Grounding] = None, with_images: bool = False,
) -> dict:
    system_prompt, user_prompt = build_prompts(
        topic, subject, grade, subtopic, class_size, profile, weak_topics, context_note, grounding,
    )
    raw = call_openrouter_text(system_prompt, user_prompt)
    lesson = _extract_json(raw)

    if with_images:
        for bullet in lesson.get("concept", []):
            visual_subject = (bullet.get("visualDemo") or "").strip() or bullet.get("text", "")
            lesson_context = f"{topic}{f' — {subtopic}' if subtopic else ''} (Grade {grade} {subject})"
            img_prompt = (
                f"A simple black-and-white line diagram, sketch-style, that a teacher could redraw by hand on a "
                f"classroom blackboard with chalk. Depicts: {visual_subject}. Context: {lesson_context}. Bold clean "
                f"outlines only, no shading, no color, no gradients, minimal or no text — this must be simple enough "
                f"to copy by hand in under a minute."
            )
            url = generate_illustration(img_prompt)
            bullet["image"] = {"url": url} if url else None

    return lesson


BANNED_WORDS = ["quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite"]


def sanitize_lesson(lesson: dict, resources: list) -> dict:
    """Tier 2, silent fix — purely mechanical, no LLM call. Drops any
    materialsUsed entry that isn't actually in the profile's resource list,
    rather than failing or leaving an invented item in place."""
    if resources and isinstance(lesson.get("materialsUsed"), list):
        allowed = {r.strip().lower() for r in resources}
        lesson = {**lesson, "materialsUsed": [m for m in lesson["materialsUsed"] if m.strip().lower() in allowed]}
    return lesson


def validate_lesson(lesson: dict, resources: list) -> list:
    """Tier 2 — deterministic checks for the rules a fast model is most likely
    to quietly drop under instruction competition. No LLM involved; returns a
    list of human-readable violations (empty = everything passed)."""
    issues = []

    concept = lesson.get("concept")
    if not isinstance(concept, list) or not (2 <= len(concept) <= 3):
        issues.append(f"concept must have 2-3 bullets, got {len(concept) if isinstance(concept, list) else 'missing'}")

    exp = lesson.get("interactiveExploration") or {}
    steps = exp.get("steps")
    if not isinstance(steps, list) or not (2 <= len(steps) <= 4):
        issues.append(f"interactiveExploration.steps must have 2-4 items, got {len(steps) if isinstance(steps, list) else 'missing'}")
    guiding_qs = exp.get("guidingQuestions")
    if not isinstance(guiding_qs, list) or not (1 <= len(guiding_qs) <= 3):
        issues.append(f"interactiveExploration.guidingQuestions must have 1-3 items, got {len(guiding_qs) if isinstance(guiding_qs, list) else 'missing'}")

    chal = lesson.get("challenge") or {}
    chal_steps = chal.get("steps")
    if not isinstance(chal_steps, list) or not (2 <= len(chal_steps) <= 3):
        issues.append(f"challenge.steps must have 2-3 items, got {len(chal_steps) if isinstance(chal_steps, list) else 'missing'}")

    if exp.get("activity") and chal.get("activity") and str(exp["activity"]).strip().lower() == str(chal["activity"]).strip().lower():
        issues.append("interactiveExploration.activity and challenge.activity must be different activities")

    level = lesson.get("levelSet") or {}
    level_qs = level.get("questions")
    if not isinstance(level_qs, list) or not (2 <= len(level_qs) <= 3):
        issues.append(f"levelSet.questions must have 2-3 items, got {len(level_qs) if isinstance(level_qs, list) else 'missing'}")

    text_blob = " ".join(str(x) for x in [
        *(concept if isinstance(concept, list) else []),
        lesson.get("realLifeConnection", ""),
        exp.get("activity", ""), *(steps if isinstance(steps, list) else []), *(guiding_qs if isinstance(guiding_qs, list) else []),
        chal.get("activity", ""), *(chal_steps if isinstance(chal_steps, list) else []),
        level.get("returnToScenario", ""), *(level_qs if isinstance(level_qs, list) else []), level.get("extendPrompt", ""),
    ]).lower()
    for w in BANNED_WORDS:
        if re.search(rf"\b{w}\b", text_blob):
            issues.append(f'contains banned word "{w}"')

    materials_used = lesson.get("materialsUsed")
    if resources and isinstance(materials_used, list):
        allowed = {r.strip().lower() for r in resources}
        invented = [m for m in materials_used if m.strip().lower() not in allowed]
        if invented:
            issues.append(f"materialsUsed includes items not in the resource list: {', '.join(invented)}")

    return issues


def repair_lesson(lesson: dict, issues: list) -> dict:
    """Tier 2 escalation — only reached when validate_lesson() still finds real
    violations after sanitize_lesson()'s silent fixes. One small, targeted call:
    hands the same lesson back with the specific violations, asking for the
    minimal edits needed to fix them — not a full regeneration."""
    system_prompt = (
        "You are given a lesson JSON and a list of specific rule violations found in it. Return the SAME "
        "JSON with ONLY the minimal edits needed to fix each listed violation — do not rewrite fields that "
        "weren't flagged, do not change the chosen activities' names, do not add commentary."
    )
    user_prompt = (
        f"Lesson JSON:\n{json.dumps(lesson, ensure_ascii=False)}\n\n"
        f"Violations to fix:\n" + "\n".join(f"- {i}" for i in issues) +
        "\n\nReturn ONLY the corrected JSON, same shape, no markdown."
    )
    try:
        raw = call_openrouter_text(system_prompt, user_prompt)
        return _extract_json(raw)
    except Exception:
        return lesson   # repair call failed/malformed — fall back to the original rather than crash


def generate_story_lesson(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
) -> dict:
    """Same idea as generate_lesson(), but for the new low-resource / activity-bank
    / 5-part story template (build_story_prompts), plus Tier 2 validation/repair.
    No image generation — the new schema has no per-bullet visualDemo field to
    hang a sketch off of. Returns {"lesson": ..., "validation": {...}}."""
    system_prompt, user_prompt = build_story_prompts(
        topic, subject, grade, subtopic, class_size, profile, weak_topics, context_note,
    )
    raw = call_openrouter_text(system_prompt, user_prompt)
    lesson = _extract_json(raw)

    resources = profile.resources if profile else []
    lesson = sanitize_lesson(lesson, resources)
    initial_issues = validate_lesson(lesson, resources)
    remaining_issues = initial_issues
    if initial_issues:
        lesson = sanitize_lesson(repair_lesson(lesson, initial_issues), resources)
        remaining_issues = validate_lesson(lesson, resources)

    return {"lesson": lesson, "validation": {"initial_issues": initial_issues, "remaining_issues": remaining_issues}}


# ── v2 template: Refresher -> Concept -> Explore (creative + image) -> Challenge ──
# (rotated official activity) -> Level Set. Every section is <=3 short bullets,
# each an {"text", "detail"?} pair -- the detail is only ever populated when it's
# genuinely useful (an expand-on-tap "+" in the UI), never padding to hit a count.

V2_BULLET_SHAPE = '{"text": "one short sentence", "detail": "optional -- a deeper explanation, a misconception to watch for, or a vivid example; omit entirely if it would just be padding"}'


def build_v2_prompts(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
    previous_topic: Optional[str] = None, avoid_activities: Optional[list] = None,
) -> tuple[str, str]:
    preferences_context = build_preferences_context(profile)
    weak_topics_context = (
        f"This class is weak on: {', '.join(weak_topics)}. If it fits naturally, let Explore or Challenge "
        f"also reinforce one of these -- but never mention them explicitly or call it review."
        if weak_topics else ""
    )
    context_note_line = f"Teacher's note for today: {context_note.strip()}" if context_note and context_note.strip() else ""
    materials_rule = (
        "the teacher's listed classroom resources above"
        if (profile and profile.resources)
        else "chalk, blackboard, notebooks, or everyday found objects -- nothing else"
    )
    previous_topic_line = (
        f'The topic studied immediately before this one was: "{previous_topic}". Build previousTopicRefresher '
        f'around this exact topic -- a short, warm reminder of it, ending with a one-line bridge into today\'s topic.'
        if previous_topic else
        'This is the first topic in this syllabus -- there is no previous topic. Set "previousTopicRefresher" to null.'
    )
    avoid_line = (
        f'Do NOT pick any of these activities for the Challenge -- they were used recently for this class '
        f'and must not repeat: {", ".join(avoid_activities)}. Pick a genuinely DIFFERENT one from the bank below.'
        if avoid_activities else ''
    )

    system_prompt = (
        'You are an expert teacher-trainer designing a classroom-ready lesson for a resource-constrained school '
        '(government or NGO-run, India). A teacher opens this five minutes before class and follows it directly.\n\n'
        f'{LOW_RESOURCE_PRINCIPLES}\n\n'
        f'{ACTIVITY_BANK}\n\n'
        'IMPORTANT -- the bank above is used differently in this template than its own header text says:\n'
        '- "explore" is NOT limited to the bank. Invent a vivid, highly creative real-life scenario and activity '
        'that fits this specific topic and teacher profile -- it should feel like a mini-story the class steps '
        'into, not a generic exercise. Still respect the low-resource rules and the profile\'s comfort zones.\n'
        '- "challenge" MUST pick exactly one activity, by its exact name, from the bank above -- this is the one '
        'structured, rotated part of the lesson. Never reuse Explore\'s activity for the Challenge.\n\n'
        'Pedagogy: never explain the concept and then test it -- understanding emerges through Explore and '
        'Challenge, the teacher guides and the students discover. Every section is at most 3 short bullets '
        f'({V2_BULLET_SHAPE}) -- never more, never padded just to reach 3.'
    )

    subtopic_line = f"\nSubtopic (focus specifically on this): {subtopic}" if subtopic else ""
    user_prompt = f"""Write a Prep Sheet for:

Topic: {topic}{subtopic_line}
Subject: {subject}
Grade: {grade}
Class size: {class_size if class_size else 'unknown'}

{previous_topic_line}

{preferences_context}
{weak_topics_context}
{context_note_line}
{avoid_line}

Return ONLY valid JSON (no markdown, no extra text), matching this exact shape:
{{
  "planningNote": "1-2 sentences of YOUR OWN reasoning, written first: given this profile and the previous topic, what should this lesson's Explore scenario be, and which Challenge activity fits (and isn't in the avoid-list)?",
  "previousTopicRefresher": {{
    "previousTopic": "the exact previous topic name given above, or null if none",
    "recap": [{V2_BULLET_SHAPE}, "... up to 3"]
  }},
  "concept": [{V2_BULLET_SHAPE}, "... up to 3 total"],
  "explore": {{
    "scenario": "2-3 sentences: a vivid, specific real-life scene the class recognizes, that this creative activity happens inside of -- not an abstract 'today we will learn X'.",
    "points": [{V2_BULLET_SHAPE}, "... up to 3 total -- the creative activity itself, how it plays out"],
    "imageFocus": "one short phrase describing the single most useful thing to sketch on the board for this Explore activity"
  }},
  "challenge": {{
    "activity": "the exact name of ONE activity from the bank above, not Explore's activity, not in the avoid-list",
    "points": [{V2_BULLET_SHAPE}, "... up to 3 total -- how it plays out for this topic"]
  }},
  "materialsUsed": ["every item actually referenced across explore/challenge -- nothing invented, nothing unused"],
  "levelSet": {{
    "points": [{V2_BULLET_SHAPE}, "... up to 3 total -- recap tied back to the Explore scenario"],
    "extendPrompt": "one open-ended line inviting another real-life connection"
  }}
}}

Rules:
- Every bullet list (recap, concept, explore.points, challenge.points, levelSet.points) has AT MOST 3 items.
- "detail" is only included on a bullet when it's genuinely useful -- never on every bullet just to fill space.
- explore.scenario and levelSet must connect to the SAME real-life idea -- levelSet returns to it, doesn't introduce a new one.
- Every step in explore/challenge must only need {materials_rule}.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- Everything must be scannable in under 2 minutes total."""

    return system_prompt, user_prompt


def generate_v2_lesson(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
    previous_topic: Optional[str] = None, avoid_activities: Optional[list] = None,
    with_images: bool = False,
) -> dict:
    system_prompt, user_prompt = build_v2_prompts(
        topic, subject, grade, subtopic, class_size, profile, weak_topics, context_note,
        previous_topic, avoid_activities,
    )
    raw = call_openrouter_text(system_prompt, user_prompt)
    lesson = _extract_json(raw)

    resources = profile.resources if profile else []
    lesson = sanitize_lesson(lesson, resources)
    initial_issues = validate_v2_lesson(lesson, resources, avoid_activities)
    remaining_issues = initial_issues
    if initial_issues:
        lesson = sanitize_lesson(repair_lesson(lesson, initial_issues), resources)
        remaining_issues = validate_v2_lesson(lesson, resources, avoid_activities)

    if with_images:
        focus = (lesson.get("explore") or {}).get("imageFocus") or (lesson.get("explore") or {}).get("scenario", "")
        if focus:
            lesson_context = f"{topic}{f' — {subtopic}' if subtopic else ''} (Grade {grade} {subject})"
            img_prompt = (
                f"A simple black-and-white line diagram, sketch-style, that a teacher could redraw by hand on a "
                f"classroom blackboard with chalk. Depicts: {focus}. Context: {lesson_context}. Bold clean outlines "
                f"only, no shading, no color, no gradients, minimal or no text -- simple enough to copy by hand "
                f"in under a minute."
            )
            url = generate_illustration(img_prompt)
            if lesson.get("explore") is not None:
                lesson["explore"]["image"] = {"url": url} if url else None

    return {"lesson": lesson, "validation": {"initial_issues": initial_issues, "remaining_issues": remaining_issues}}


def _v2_bullets_ok(bullets) -> bool:
    return isinstance(bullets, list) and 1 <= len(bullets) <= 3 and all(isinstance(b, dict) and b.get("text") for b in bullets)


def validate_v2_lesson(lesson: dict, resources: list, avoid_activities: Optional[list] = None) -> list:
    issues = []

    refresher = lesson.get("previousTopicRefresher")
    if refresher is not None:
        if not isinstance(refresher, dict) or not _v2_bullets_ok(refresher.get("recap")):
            issues.append("previousTopicRefresher.recap must be 1-3 {text, detail?} bullets (or the whole field null)")

    if not _v2_bullets_ok(lesson.get("concept")):
        issues.append(f"concept must be 1-3 {{text, detail?}} bullets, got {lesson.get('concept')!r}")

    explore = lesson.get("explore") or {}
    if not _v2_bullets_ok(explore.get("points")):
        issues.append("explore.points must be 1-3 {text, detail?} bullets")
    if not explore.get("scenario"):
        issues.append("explore.scenario is missing")

    challenge = lesson.get("challenge") or {}
    if not _v2_bullets_ok(challenge.get("points")):
        issues.append("challenge.points must be 1-3 {text, detail?} bullets")
    if not challenge.get("activity"):
        issues.append("challenge.activity is missing")
    elif explore.get("activity") and challenge["activity"].strip().lower() == str(explore.get("activity", "")).strip().lower():
        issues.append("challenge.activity must not be the same as explore's activity")
    elif avoid_activities and challenge["activity"].strip().lower() in {a.strip().lower() for a in avoid_activities}:
        issues.append(f"challenge.activity \"{challenge['activity']}\" is in the avoid-list (used recently) and must be different")

    level = lesson.get("levelSet") or {}
    if not _v2_bullets_ok(level.get("points")):
        issues.append("levelSet.points must be 1-3 {text, detail?} bullets")

    text_blob = json.dumps(lesson, ensure_ascii=False).lower()
    for w in BANNED_WORDS:
        if re.search(rf"\b{w}\b", text_blob):
            issues.append(f'contains banned word "{w}"')

    materials_used = lesson.get("materialsUsed")
    if resources and isinstance(materials_used, list):
        allowed = {r.strip().lower() for r in resources}
        invented = [m for m in materials_used if m.strip().lower() not in allowed]
        if invented:
            issues.append(f"materialsUsed includes items not in the resource list: {', '.join(invented)}")

    return issues


# ── Hierarchical pipeline ────────────────────────────────────────────────────
# Instead of one prompt carrying every preference at once (roles + goals +
# activities + comfort zones + resources + personalization + language, all
# competing for attention), this runs the SAME conversation across 4 turns:
#   1. ROLE        -- how this teacher's self-identity shapes the angle
#   2. GOALS        -- what they want students to walk away with (+ opening scenario)
#   3. OTHER PREFS  -- activities/comfort/resources/personalization/language -> 2 concrete activity picks
#   4. MERGE        -- finalize everything above into the exact SmartLesson JSON
# Each stage's assistant reply is appended to the message list before the next
# stage's user turn, so the model has native, verbatim access to what it
# already committed to -- no lossy re-summarization by our code in between.

def _hier_system_prompt() -> str:
    return (
        'You are an expert teacher-trainer designing a classroom-ready lesson for a resource-constrained '
        'school (government or NGO-run, India), working through this DELIBERATELY IN STAGES across this '
        'conversation so nothing gets lost: first the teacher\'s ROLE identity, then their GOALS for '
        'students, then their remaining preferences and constraints, and finally a single merged prep sheet. '
        'Build on what you said in each earlier turn -- do not contradict or restart from scratch.\n\n'
        f'{LOW_RESOURCE_PRINCIPLES}\n\n{ACTIVITY_BANK}'
    )


def _hier_role_turn(topic: str, subject: str, grade: str, profile: Optional[TeachingProfile]) -> str:
    roles = ', '.join(profile.roles) if profile and profile.roles else '(no specific role set -- assume a generic, well-rounded classroom teacher)'
    return (
        f"STAGE 1 of 4 -- ROLE.\n"
        f"Topic: {topic} ({subject}, Grade {grade}).\n"
        f"This teacher sees themselves as: {roles}.\n\n"
        f"In 3-4 sentences, describe the TEACHING ANGLE this specific role identity brings to THIS topic -- "
        f"what kind of experience they'd create, their tone/energy, what pedagogical move feels natural to "
        f"them (e.g. a Mentor might build trust before content; a Guide might set up a puzzle and let "
        f"students find their way; an Influencer might connect it to something socially relevant students "
        f"already care about). Do NOT describe specific activities or lesson content yet -- only the angle."
    )


def _hier_goal_turn(profile: Optional[TeachingProfile]) -> str:
    goals = ', '.join(profile.goals) if profile and profile.goals else '(no specific goals set -- assume "understand deeply" and "stay curious")'
    return (
        f"STAGE 2 of 4 -- GOALS.\n"
        f"This teacher wants students to: {goals}.\n\n"
        f"Building on the teaching angle above, write:\n"
        f"1. One sentence refining how the lesson should FEEL so it concretely serves these goals (not just "
        f"decorated with them).\n"
        f"2. A real-life scenario (2-3 sentences) from a child's world that opens the lesson with curiosity "
        f"and sets up achieving these goals -- specific and concrete (a market, a bus, a cricket match, a "
        f"school event), never an abstract 'today we will learn...'.\n"
        f"Do NOT describe specific activities yet -- only the framing and opening scenario."
    )


def _hier_other_prefs_turn(profile: Optional[TeachingProfile]) -> str:
    prefs = build_preferences_context(profile)
    return (
        f"STAGE 3 of 4 -- REMAINING PREFERENCES & CONSTRAINTS.\n"
        f"{prefs}\n\n"
        f"Building on everything above (the angle, the goals, the opening scenario), pick TWO DIFFERENT "
        f"activities from the bank given at the start of this conversation -- one to be the main interactive "
        f"exploration, a different one to be a later challenge. For each: name it exactly as it appears in "
        f"the bank, justify the pick in one sentence against this teacher's preferences/constraints above, "
        f"and sketch (2-3 sentences) how you'd adapt it to THIS specific topic. Respect the low-resource "
        f"rules and this teacher's comfort zones and available resources. Do not write final step-by-step "
        f"instructions yet -- just the two picks and how each would work."
    )


def _hier_merge_turn(
    topic: str, subject: str, grade: str, subtopic: Optional[str], class_size: Optional[int],
    weak_topics: Optional[list], context_note: Optional[str], grounding: Optional[Grounding],
) -> str:
    grounding_context = build_grounding_context(grounding)
    weak_topics_context = (
        f"This class is weak on: {', '.join(weak_topics)}. If it fits naturally, let the real-life "
        f"connection or challenge also reinforce one of these -- but never mention them explicitly or call it review."
        if weak_topics else ""
    )
    context_note_line = f"Teacher's note for today: {context_note.strip()}" if context_note and context_note.strip() else ""
    subtopic_line = f"\nSubtopic (focus specifically on this): {subtopic}" if subtopic else ""

    return f"""STAGE 4 of 4 -- MERGE & FINALIZE.

Topic: {topic}{subtopic_line}
Subject: {subject}
Grade: {grade}
Class size: {class_size if class_size else 'unknown'}

{grounding_context}
{weak_topics_context}
{context_note_line}

Now merge everything from this conversation -- the role angle, the goals framing + opening scenario, and the two chosen activities -- into ONE final Prep Sheet. Stay faithful to every earlier stage; do not introduce a new angle, new scenario, or new activities not already discussed above.

Return ONLY valid JSON (no markdown, no extra text), matching this exact shape:
{{
  "concept": ["2 to 3 short bullets introducing the idea in the simplest possible way -- no paragraphs"],
  "realLifeConnection": "The SAME opening scenario established in stage 2 -- do not change it now.",
  "interactiveExploration": {{
    "activity": "the FIRST activity chosen in stage 3, named exactly as in the bank",
    "steps": ["2 to 4 short steps describing exactly how this activity plays out for THIS topic -- adapt the numbers/wording"],
    "guidingQuestions": ["1 to 3 questions the teacher asks mid-activity so the concept emerges from discussion"]
  }},
  "challenge": {{
    "activity": "the SECOND (different) activity chosen in stage 3, named exactly as in the bank",
    "steps": ["2 to 3 short steps for how this plays out for THIS topic"]
  }},
  "levelSet": {{
    "returnToScenario": "One line bringing back the exact opening real-life scenario",
    "questions": ["2 to 3 short questions that let students show they can now read/write/compare/explain the idea, tied to that scenario"],
    "extendPrompt": "One open-ended line inviting students to think of another place this idea shows up in their own life"
  }}
}}

Rules:
- concept: exactly 2 or 3 bullets, one short sentence each.
- interactiveExploration and challenge MUST use the two DIFFERENT activities already chosen in stage 3 above, now with full step-by-step detail and topic-specific numbers/wording -- do not swap or invent new ones.
- levelSet must reference the SAME scenario from realLifeConnection, not a new one.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- Everything must be scannable in under 2 minutes total."""


def generate_hierarchical_lesson(
    topic: str, subject: str, grade: str, subtopic: Optional[str] = None,
    class_size: Optional[int] = None, profile: Optional[TeachingProfile] = None,
    weak_topics: Optional[list] = None, context_note: Optional[str] = None,
    grounding: Optional[Grounding] = None,
) -> dict:
    """Drives the 4-turn conversation and returns {"stages": {...}, "lesson": {...}}
    so both the intermediate reasoning and the final JSON can be inspected."""
    messages = [{"role": "system", "content": _hier_system_prompt()}]
    stages = {}

    messages.append({"role": "user", "content": _hier_role_turn(topic, subject, grade, profile)})
    stages["role"] = call_openrouter_messages(messages)
    messages.append({"role": "assistant", "content": stages["role"]})

    messages.append({"role": "user", "content": _hier_goal_turn(profile)})
    stages["goals"] = call_openrouter_messages(messages)
    messages.append({"role": "assistant", "content": stages["goals"]})

    messages.append({"role": "user", "content": _hier_other_prefs_turn(profile)})
    stages["other"] = call_openrouter_messages(messages)
    messages.append({"role": "assistant", "content": stages["other"]})

    messages.append({"role": "user", "content": _hier_merge_turn(
        topic, subject, grade, subtopic, class_size, weak_topics, context_note, grounding,
    )})
    final_raw = call_openrouter_messages(messages)
    lesson = _extract_json(final_raw)

    return {"stages": stages, "lesson": lesson}


# ── Pretty-printing ──────────────────────────────────────────────────────────

def format_profile_inputs(profile: Optional[TeachingProfile]) -> str:
    """Echoes the exact Teaching Profile inputs used, plus the derived
    preferencesContext string actually injected into the prompt — so the
    output can be traced back to precisely what the model was told."""
    lines = ["PROFILE INPUTS", "-" * 40]
    if profile is None:
        lines.append("  (none on file — pre-onboarding fallback)")
        lines.append("")
        return "\n".join(lines)

    lines.append(f"  Roles:              {', '.join(profile.roles) or '(none)'}")
    lines.append(f"  Goals:              {', '.join(profile.goals) or '(none)'}")
    lines.append(f"  Preferred activities:{' ' if profile.preferred_activities else ' '}{', '.join(profile.preferred_activities) or '(none)'}")
    lines.append(f"  Comfort zones:      {', '.join(profile.comfort_zones) or '(none)'}")
    lines.append(f"  Resources:          {', '.join(profile.resources) or '(none)'}")
    lines.append(f"  Language:           {', '.join(profile.language) or '(none)'}")
    lines.append(f"  Class size:         {profile.class_size or '(none)'}")
    lines.append(f"  Personalization:    " + ", ".join(f"{k}={v}" for k, v in profile.personalization.items()))
    emph, mini = personalization_emphasis(profile.personalization)
    lines.append(f"  -> emphasize:       {', '.join(emph) or '(none)'}")
    lines.append(f"  -> minimize:        {', '.join(mini) or '(none)'}")
    lines.append("")
    lines.append("  Exact preferencesContext sent to the model:")
    for wrapped in _wrap(build_preferences_context(profile), 74):
        lines.append(f"    {wrapped}")
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, width: int) -> list:
    words = text.split()
    lines, cur = [], ""
    for w in words:
        if len(cur) + len(w) + 1 > width:
            lines.append(cur)
            cur = w
        else:
            cur = f"{cur} {w}".strip()
    if cur:
        lines.append(cur)
    return lines or [""]


def format_lesson(lesson: dict, header: str) -> str:
    lines = ["=" * 78, header, "=" * 78, ""]

    lines.append("TODAY'S GOAL")
    lines.append("-" * 40)
    lines.append(f"  {lesson.get('goal', '')}")
    lines.append("")

    lines.append("MATERIALS")
    lines.append("-" * 40)
    for m in lesson.get("materials", []):
        lines.append(f"  [x] {m}")
    lines.append("")

    lines.append("CONCEPT")
    lines.append("-" * 40)
    for i, b in enumerate(lesson.get("concept", []), 1):
        lines.append(f"  {i}. {b.get('text', '')}")
        for key, label in (("deeperExplanation", "Deeper"), ("misconception", "Watch for"),
                           ("realLifeExample", "Example"), ("visualDemo", "Show")):
            if b.get(key):
                lines.append(f"       [{label}] {b[key]}")
        if b.get("image") and b["image"].get("url"):
            lines.append(f"       [Image] {b['image']['url']}")
    lines.append("")

    lines.append("FLOW")
    lines.append("-" * 40)
    for step in lesson.get("flow", []):
        minutes = f" ({step['minutes']} min)" if step.get("minutes") else ""
        lines.append(f"  {step.get('label', '')}{minutes}")
        lines.append(f"       {step.get('detail', '')}")
    lines.append("")

    lines.append("TALKING POINTS")
    lines.append("-" * 40)
    for tp in lesson.get("talkingPoints", []):
        lines.append(f"  * {tp.get('say', '')}")
        if tp.get("keepGoing"):
            lines.append(f"       [Keep it going] {tp['keepGoing']}")
    lines.append("")

    lines.append("WATCH OUT")
    lines.append("-" * 40)
    lines.append(f"  {lesson.get('watchFor', '')}")
    lines.append("")

    diff = lesson.get("differentiation") or {}
    lines.append("IF THEY'RE STUCK")
    lines.append("-" * 40)
    lines.append(f"  {diff.get('ifStruggling', '')}")
    lines.append("")

    lines.append("EXTENSION (if ahead)")
    lines.append("-" * 40)
    lines.append(f"  {diff.get('ifAhead', '')}")
    lines.append("")

    return "\n".join(lines)


def format_story_lesson(lesson: dict, header: str, validation: Optional[dict] = None) -> str:
    lines = ["=" * 78, header, "=" * 78, ""]

    if lesson.get("planningNote"):
        lines.append("PLANNING NOTE (internal only — never shown to the teacher)")
        lines.append("-" * 40)
        lines.append(f"  {lesson['planningNote']}")
        lines.append("")

    lines.append("CONCEPT")
    lines.append("-" * 40)
    for i, c in enumerate(lesson.get("concept", []), 1):
        lines.append(f"  {i}. {c}")
    lines.append("")

    lines.append("LET'S IMAGINE... (real-life connection)")
    lines.append("-" * 40)
    lines.append(f"  {lesson.get('realLifeConnection', '')}")
    lines.append("")

    exp = lesson.get("interactiveExploration") or {}
    lines.append(f"INTERACTIVE EXPLORATION -- Activity: {exp.get('activity', '')}")
    lines.append("-" * 40)
    for i, s in enumerate(exp.get("steps", []), 1):
        lines.append(f"  {i}. {s}")
    for q in exp.get("guidingQuestions", []):
        lines.append(f"       [Ask] {q}")
    lines.append("")

    chal = lesson.get("challenge") or {}
    lines.append(f"CHALLENGE -- Activity: {chal.get('activity', '')}")
    lines.append("-" * 40)
    for i, s in enumerate(chal.get("steps", []), 1):
        lines.append(f"  {i}. {s}")
    lines.append("")

    if lesson.get("materialsUsed"):
        lines.append("MATERIALS USED")
        lines.append("-" * 40)
        for m in lesson["materialsUsed"]:
            lines.append(f"  [x] {m}")
        lines.append("")

    level = lesson.get("levelSet") or {}
    lines.append("LEVEL SET (recap)")
    lines.append("-" * 40)
    lines.append(f"  {level.get('returnToScenario', '')}")
    for q in level.get("questions", []):
        lines.append(f"       - {q}")
    if level.get("extendPrompt"):
        lines.append(f"       [Extend] {level['extendPrompt']}")
    lines.append("")

    if validation is not None:
        lines.append("TIER 2 VALIDATION")
        lines.append("-" * 40)
        if validation["initial_issues"]:
            lines.append(f"  Initial violations found ({len(validation['initial_issues'])}):")
            for i in validation["initial_issues"]:
                lines.append(f"    - {i}")
            if validation["remaining_issues"]:
                lines.append(f"  After repair call, still remaining ({len(validation['remaining_issues'])}):")
                for i in validation["remaining_issues"]:
                    lines.append(f"    - {i}")
            else:
                lines.append("  All fixed by the repair call.")
        else:
            lines.append("  Clean on first pass — no violations, no repair call needed.")
        lines.append("")

    return "\n".join(lines)


def format_hierarchical_stages(stages: dict) -> str:
    """Shows the intermediate reasoning from each of the 3 preference stages,
    before the final merged lesson (printed separately via format_story_lesson)
    -- so it's possible to see exactly what each stage contributed."""
    lines = ["", "HIERARCHICAL PIPELINE -- INTERMEDIATE STAGES", "=" * 78, ""]
    for key, title in (("role", "STAGE 1 -- ROLE"), ("goals", "STAGE 2 -- GOALS"), ("other", "STAGE 3 -- OTHER PREFERENCES -> ACTIVITY PICKS")):
        lines.append(title)
        lines.append("-" * 40)
        for wrapped in _wrap(stages.get(key, ""), 74):
            lines.append(f"  {wrapped}")
        lines.append("")
    return "\n".join(lines)


def _format_bullets(bullets, indent="  ") -> list:
    lines = []
    for b in bullets or []:
        lines.append(f"{indent}- {b.get('text', '')}")
        if b.get("detail"):
            lines.append(f"{indent}    [+] {b['detail']}")
    return lines


def format_v2_lesson(lesson: dict, header: str, validation: Optional[dict] = None) -> str:
    lines = ["=" * 78, header, "=" * 78, ""]

    if lesson.get("planningNote"):
        lines.append("PLANNING NOTE (internal only)")
        lines.append("-" * 40)
        lines.append(f"  {lesson['planningNote']}")
        lines.append("")

    refresher = lesson.get("previousTopicRefresher")
    lines.append("PREVIOUS TOPIC REFRESHER")
    lines.append("-" * 40)
    if refresher:
        lines.append(f"  Previous topic: {refresher.get('previousTopic', '')}")
        lines.extend(_format_bullets(refresher.get("recap")))
    else:
        lines.append("  (none -- first topic in the syllabus)")
    lines.append("")

    lines.append("CONCEPT")
    lines.append("-" * 40)
    lines.extend(_format_bullets(lesson.get("concept")))
    lines.append("")

    explore = lesson.get("explore") or {}
    lines.append("EXPLORE (creative + real-life connection)")
    lines.append("-" * 40)
    lines.append(f"  Scenario: {explore.get('scenario', '')}")
    lines.extend(_format_bullets(explore.get("points")))
    if explore.get("imageFocus"):
        lines.append(f"  [Image focus] {explore['imageFocus']}")
    if explore.get("image") and explore["image"].get("url"):
        lines.append(f"  [Image] {explore['image']['url']}")
    lines.append("")

    challenge = lesson.get("challenge") or {}
    lines.append(f"CHALLENGE -- Activity: {challenge.get('activity', '')} (from official bank, rotated)")
    lines.append("-" * 40)
    lines.extend(_format_bullets(challenge.get("points")))
    lines.append("")

    if lesson.get("materialsUsed"):
        lines.append("MATERIALS USED")
        lines.append("-" * 40)
        for m in lesson["materialsUsed"]:
            lines.append(f"  [x] {m}")
        lines.append("")

    level = lesson.get("levelSet") or {}
    lines.append("LEVEL SET")
    lines.append("-" * 40)
    lines.extend(_format_bullets(level.get("points")))
    if level.get("extendPrompt"):
        lines.append(f"  [Extend] {level['extendPrompt']}")
    lines.append("")

    if validation is not None:
        lines.append("TIER 2 VALIDATION")
        lines.append("-" * 40)
        if validation["initial_issues"]:
            lines.append(f"  Initial violations found ({len(validation['initial_issues'])}):")
            for i in validation["initial_issues"]:
                lines.append(f"    - {i}")
            if validation["remaining_issues"]:
                lines.append(f"  After repair call, still remaining ({len(validation['remaining_issues'])}):")
                for i in validation["remaining_issues"]:
                    lines.append(f"    - {i}")
            else:
                lines.append("  All fixed by the repair call.")
        else:
            lines.append("  Clean on first pass — no violations, no repair call needed.")
        lines.append("")

    return "\n".join(lines)


# ── Custom profile from CLI flags ────────────────────────────────────────────

def _split(val: Optional[str]) -> list:
    return [s.strip() for s in val.split(",")] if val else []


def build_custom_profile(args) -> TeachingProfile:
    personalization = {k: "sometimes" for k in PERSONALIZATION_KEYS}
    for key in _split(args.emphasize):
        if key in personalization:
            personalization[key] = "often"
    for key in _split(args.minimize):
        if key in personalization:
            personalization[key] = "never"
    return TeachingProfile(
        roles=_split(args.roles),
        goals=_split(args.goals),
        preferred_activities=_split(args.activities),
        comfort_zones=_split(args.comfort),
        resources=_split(args.resources),
        language=_split(args.language) or ["English"],
        class_size=args.class_size_bucket or "20-40",
        personalization=personalization,
    )


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Test the Prep Material generator across profile/grade/topic combinations.")
    parser.add_argument("--grade", default="5", help="Single grade (default: 5)")
    parser.add_argument("--grades", help="Comma-separated grades for a matrix run, e.g. 3,5,8")
    parser.add_argument("--subject", default="General", help="Single subject (default: General)")
    parser.add_argument("--subjects", help="Comma-separated subjects for a matrix run")
    parser.add_argument("--topic", help="Single topic")
    parser.add_argument("--topics", help="Comma-separated topics for a matrix run, e.g. \"Fractions,Photosynthesis\"")
    parser.add_argument("--subtopic", help="Optional subtopic (applies to all topics in a matrix run)")
    parser.add_argument("--profile", default="none", help="Preset name (see --list-profiles), or 'custom'")
    parser.add_argument("--compare", action="store_true", help="Run every preset profile against the given grade(s)/topic(s)")
    parser.add_argument("--generate", type=int, default=0,
                         help="Generate N realistic profiles sampled from the real wizard option pools "
                              "(overrides --profile/--compare) — for comparing outputs across diverse, "
                              "non-hand-picked combinations")
    parser.add_argument("--seed", type=int, default=42, help="Random seed for --generate (default: 42, reproducible)")
    parser.add_argument("--class-size", type=int, help="Numeric class size passed to the prompt (e.g. 32)")
    parser.add_argument("--context-note", help="Optional ephemeral 'anything for today?' note")
    parser.add_argument("--weak-topics", help="Comma-separated prior topics to mark as weak (silent gap-awareness test)")
    parser.add_argument("--images", action="store_true", help="Also generate images (per-bullet board-sketch for classic; one Explore sketch for v2)")
    parser.add_argument("--format", choices=["classic", "story", "hierarchical", "v2"], default="v2",
                         help="'v2' (default): Previous Topic Refresher -> Concept -> Explore (creative, free-form, "
                              "with an AI image) -> Challenge (official activity bank, rotated against --avoid-activities) "
                              "-> Level Set. Every section capped at 3 expandable {text, detail?} bullets. "
                              "'story': the prior single-prompt 5-part template (Concept -> Real-Life Connection -> "
                              "Interactive Exploration -> Challenge -> Level Set), currently still live in production. "
                              "'hierarchical': the 4-turn conversation experiment (role -> goals -> other prefs -> merge). "
                              "'classic': the original goal/materials/concept/flow/talkingPoints/differentiation/watchFor shape.")
    parser.add_argument("--previous-topic", help="v2 only: name of the topic studied immediately before this one (omit to simulate 'first topic in syllabus')")
    parser.add_argument("--avoid-activities", help="v2 only: comma-separated activity names to exclude from the Challenge pick (simulates rotation against recent generations)")
    parser.add_argument("--out-dir", default=str(_HERE / "prep_material_test_output"), help="Where to save .json/.md output")
    parser.add_argument("--list-profiles", action="store_true", help="List built-in profile presets and exit")

    # custom-profile-only flags
    parser.add_argument("--roles", help="Custom profile: comma-separated roles")
    parser.add_argument("--goals", help="Custom profile: comma-separated goals")
    parser.add_argument("--activities", help="Custom profile: comma-separated preferred activities")
    parser.add_argument("--comfort", help="Custom profile: comma-separated comfort zones")
    parser.add_argument("--resources", help="Custom profile: comma-separated classroom resources")
    parser.add_argument("--language", help="Custom profile: comma-separated classroom language(s)")
    parser.add_argument("--class-size-bucket", choices=["<20", "20-40", ">40"], help="Custom profile: classroom.classSize bucket")
    parser.add_argument("--emphasize", help="Custom profile: comma-separated personalization keys to set to 'often' "
                                             f"(one of: {', '.join(PERSONALIZATION_KEYS)})")
    parser.add_argument("--minimize", help="Custom profile: comma-separated personalization keys to set to 'never'")

    args = parser.parse_args()

    if args.list_profiles:
        list_profiles()
        return

    if not args.topic and not args.topics:
        parser.error("Provide --topic or --topics")

    grades = _split(args.grades) or [args.grade]
    subjects = _split(args.subjects) or [args.subject]
    topics = _split(args.topics) or [args.topic]
    weak_topics = _split(args.weak_topics)

    if args.generate > 0:
        profiles = generate_combo_profiles(args.generate, seed=args.seed)
    elif args.profile == "custom":
        profiles = {"custom": build_custom_profile(args)}
    elif args.compare:
        profiles = PRESET_PROFILES
    else:
        if args.profile not in PRESET_PROFILES:
            parser.error(f"Unknown profile '{args.profile}'. Run --list-profiles to see options, or use --profile custom.")
        profiles = {args.profile: PRESET_PROFILES[args.profile]}

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    combos = list(itertools.product(grades, subjects, topics, profiles.items()))
    print(f"Running {len(combos)} combination(s) — model: {OPENROUTER_MODEL}\n")

    comparison_rows = []  # (profile_name, grade, subject, topic, profile, lesson) for the combined comparison file

    for grade, subject, topic, (profile_name, profile) in combos:
        header = f"Profile: {profile_name}  |  Grade: {grade}  |  Subject: {subject}  |  Topic: {topic}"
        if args.subtopic:
            header += f"  |  Subtopic: {args.subtopic}"
        print(f"--> Generating: {header}")

        class_size = args.class_size or (_CLASS_SIZE_TO_NUMBER.get(profile.class_size) if profile else None)
        stages = None
        validation = None
        try:
            if args.format == "v2":
                result = generate_v2_lesson(
                    topic=topic, subject=subject, grade=grade, subtopic=args.subtopic,
                    class_size=class_size, profile=profile, weak_topics=weak_topics or None,
                    context_note=args.context_note, previous_topic=args.previous_topic,
                    avoid_activities=_split(args.avoid_activities) or None, with_images=args.images,
                )
                lesson, validation = result["lesson"], result["validation"]
            elif args.format == "hierarchical":
                result = generate_hierarchical_lesson(
                    topic=topic, subject=subject, grade=grade, subtopic=args.subtopic,
                    class_size=class_size, profile=profile, weak_topics=weak_topics or None,
                    context_note=args.context_note,
                )
                stages, lesson = result["stages"], result["lesson"]
            elif args.format == "story":
                result = generate_story_lesson(
                    topic=topic, subject=subject, grade=grade, subtopic=args.subtopic,
                    class_size=class_size, profile=profile, weak_topics=weak_topics or None,
                    context_note=args.context_note,
                )
                lesson, validation = result["lesson"], result["validation"]
            else:
                lesson = generate_lesson(
                    topic=topic, subject=subject, grade=grade, subtopic=args.subtopic,
                    class_size=class_size, profile=profile, weak_topics=weak_topics or None,
                    context_note=args.context_note, with_images=args.images,
                )
        except Exception as e:
            print(f"    [ERROR] {e}\n")
            continue

        profile_block = format_profile_inputs(profile)
        if args.format == "classic":
            rendered = profile_block + format_lesson(lesson, header)
        elif args.format == "hierarchical":
            rendered = profile_block + format_hierarchical_stages(stages) + format_story_lesson(lesson, header)
        elif args.format == "v2":
            rendered = profile_block + format_v2_lesson(lesson, header, validation)
        else:
            rendered = profile_block + format_story_lesson(lesson, header, validation)
        print(rendered)

        short_profile_name = profile_name.split(" (")[0]  # drop the descriptive tag — keep filenames short on Windows
        slug = re.sub(r"[^a-z0-9]+", "_", f"{short_profile_name}_{grade}_{subject}_{topic}".lower()).strip("_")
        (out_dir / f"{slug}.json").write_text(
            json.dumps({"profile_name": profile_name, "grade": grade, "subject": subject, "topic": topic,
                        "profile": profile.__dict__ if profile else None, "stages": stages,
                        "validation": validation, "lesson": lesson},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (out_dir / f"{slug}.md").write_text(rendered, encoding="utf-8")
        comparison_rows.append((profile_name, grade, subject, topic, profile, lesson))

    if comparison_rows and args.format == "classic":
        write_comparison_file(comparison_rows, out_dir)

    print(f"\nSaved {len(combos)} result(s) to: {out_dir}")


def write_comparison_file(rows: list, out_dir: Path) -> None:
    """One combined file with every combo's inputs + Experience (the highest-
    signal field) side by side, so fine-tuning differences are scannable
    without opening each per-combo file."""
    lines = ["# Prep Material comparison", ""]
    for profile_name, grade, subject, topic, profile, lesson in rows:
        lines.append(f"## {profile_name}")
        lines.append(f"- Grade {grade} | {subject} | {topic}")
        if profile:
            emph, mini = personalization_emphasis(profile.personalization)
            lines.append(f"- Roles: {', '.join(profile.roles) or '(none)'} | Goals: {', '.join(profile.goals) or '(none)'}")
            lines.append(f"- Activities: {', '.join(profile.preferred_activities) or '(none)'} | Comfort: {', '.join(profile.comfort_zones) or '(none)'}")
            lines.append(f"- Resources: {', '.join(profile.resources) or '(none)'} | Language: {', '.join(profile.language) or '(none)'} | Class size: {profile.class_size or '(none)'}")
            lines.append(f"- Emphasize: {', '.join(emph) or '(none)'} | Minimize: {', '.join(mini) or '(none)'}")
        else:
            lines.append("- (no Teaching Profile on file)")
        lines.append("")
        lines.append(f"**Goal:** {lesson.get('goal', '')}")
        lines.append("")
        lines.append(f"**Materials:** {', '.join(lesson.get('materials', []))}")
        lines.append("")
        flow_summary = " -> ".join(
            f"{s.get('label', '')} ({s['detail'][:60]}{'...' if len(s.get('detail', '')) > 60 else ''})"
            for s in lesson.get("flow", [])
        )
        lines.append(f"**Flow:** {flow_summary}")
        lines.append("")
        concept_summary = " / ".join(b.get("text", "") for b in lesson.get("concept", []))
        lines.append(f"**Concept bullets:** {concept_summary}")
        lines.append("")
        lines.append("---")
        lines.append("")
    (out_dir / "comparison.md").write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
