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
    parser.add_argument("--images", action="store_true", help="Also generate the per-bullet board-sketch (slower, costs more)")
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
        try:
            lesson = generate_lesson(
                topic=topic, subject=subject, grade=grade, subtopic=args.subtopic,
                class_size=class_size, profile=profile, weak_topics=weak_topics or None,
                context_note=args.context_note, with_images=args.images,
            )
        except Exception as e:
            print(f"    [ERROR] {e}\n")
            continue

        profile_block = format_profile_inputs(profile)
        rendered = profile_block + format_lesson(lesson, header)
        print(rendered)

        short_profile_name = profile_name.split(" (")[0]  # drop the descriptive tag — keep filenames short on Windows
        slug = re.sub(r"[^a-z0-9]+", "_", f"{short_profile_name}_{grade}_{subject}_{topic}".lower()).strip("_")
        (out_dir / f"{slug}.json").write_text(
            json.dumps({"profile_name": profile_name, "grade": grade, "subject": subject, "topic": topic,
                        "profile": profile.__dict__ if profile else None, "lesson": lesson},
                       indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        (out_dir / f"{slug}.md").write_text(rendered, encoding="utf-8")
        comparison_rows.append((profile_name, grade, subject, topic, profile, lesson))

    if comparison_rows:
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
