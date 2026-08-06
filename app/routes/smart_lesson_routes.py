"""Ports frontend/app/api/smart-lesson/route.ts — the main Prep Sheet
generator. The single largest/most complex route in the migration: gathers
class context (interests, weak topics, teaching profile), two kinds of
grounding (ontology summary + verbatim textbook excerpt), the previous topic
for the refresher, recent-activity rotation, and post-class feedback, builds a
long structured prompt, retries across a primary/fallback model pair with
deterministic JSON-salvage on parse failure, runs a Tier-2 validate/repair
pass, then generates up to 4 board-sketch images in parallel.
"""
import asyncio
import json
import os
import re
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import require_user
from ..lib.ai import generate_illustration, illustration_key, store_illustration
from ..lib.logger import api_log, get_client_ip
from ..lib.prep_context import (
    fetch_feedback_context, fetch_grounding, fetch_recent_challenge_activities,
    gather_class_context, personalization_tier_line,
)
from ..lib.prompt_fragments import engagement_level_guidance
from ..lib.rate_limit import check_vision_rate_limit
from ..lib.refresher_topic import fetch_previous_topic
from ..lib.schemas import SmartLessonSchema
from ..lib.subject_prompts import resolve_subject_module
from ..lib.supabase_clients import create_admin_client
from ..lib.textbook_grounding import fetch_textbook_grounding, textbook_prompt_block

router = APIRouter()

# ── Low-resource activity bank + "v2" template ──────────────────────────────
# Curated by hand for government/NGO schools with minimal supplies. Kept as
# plain reference text (NOT a structured/queryable library) — the model picks
# and adapts an activity using its own judgment, this route never filters it.
# The activity bank + pedagogy are SUBJECT-ROUTED (see subject_prompts.py):
# the single generic bank was really a math bank, so non-math subjects were
# forced to improvise around math activities. Only "challenge" draws from the
# bank — "explore" is deliberately free-form.
LOW_RESOURCE_PRINCIPLES = """This school has minimal resources. Every activity must work with:
- No printers, no projectors, no smart boards, no photocopies, no internet, no electricity dependence.
- Only chalk, blackboard, notebooks/paper, or everyday found objects: stones, sticks, bottle caps, old newspapers — or the students' own bodies as the "material".
- 5 to 15 minutes to run, start to finish, including giving instructions.
- A class of 30 to 60 students on fixed benches. Students CAN stand, move to the board, or step into an aisle briefly, but do NOT assume desks/benches can be permanently rearranged into group pods or stations.
- Something a single teacher can set up and facilitate alone, with no prep the night before.

If an activity from the bank below normally uses something not on this list (printed cards, cut-outs, props), adapt it to run on chalk/paper/found objects instead of dropping it."""

_SDT_ENGAGEMENT = """ENGAGEMENT — design for SELF-DRIVEN participation (Self-Determination Theory). Students engage when three needs are met by the STRUCTURE of the activity itself, not by rewards or pressure:
- AUTONOMY: give real, small choices ("solve with stones OR draw it in the dust"; pick your role). Never use controlling language ("you must", "copy this down").
- COMPETENCE: pitch each task just hard enough to need thought but easy enough to succeed WITH A PARTNER; make progress visible and never label anyone as behind.
- RELATEDNESS: ground everything in THIS class's real world (their interests, the local market / festival / cricket match, home and community) and let them do it WITH a partner, not alone under the teacher's eye.
Shape the whole lesson as one motivating cycle: a CURIOSITY HOOK (a local puzzle they actually want to solve) → a PAIR-FIRST attempt → the concept emerges from what they tried → PLAY the challenge → a quick REFLECT ("what worked?") → ACT (apply it to a real textbook-style problem). Coercion, public shaming, and rote copying destroy motivation — design so that joining in is the EASIEST and most rewarding thing a student can do."""

_BULLET_SHAPE = '{"text": "a compact, informative headline — about 6 to 12 words that actually convey the point at a glance. Not a bare fragment (\'Rounding\'), not a full multi-clause sentence — a scannable line a teacher instantly understands (e.g. \'Round each number to the nearest ten first\', \'The class turns into a village market\')", "detail": "the deeper explanation of THIS bullet — one or two full sentences: the how/why, an example, what to watch for. The headline gives the point; detail adds the substance behind it. Include detail on almost every bullet, and never just reword the headline — \\"detail\\" must add information the headline didn\'t already give."}'

# The JSON-shape spec + numbered rules — pure literal text with two tokens
# (@@B@@ for a repeated bullet-shape spec, @@M@@ for the materials rule) filled
# in via .replace() rather than str.format()/an f-string, since the text below
# is itself full of literal { } braces (it's showing the model an example JSON
# shape) that an f-string would otherwise require escaping one-by-one.
_LESSON_SHAPE_AND_RULES = """Return ONLY valid JSON (no markdown, no extra text), matching this exact shape:
{
  "planningNote": "1-2 sentences of YOUR OWN reasoning, written first: given this class's interests, the teacher's profile, and the previous topic — which of their interests will Explore (and the refresher) be built around, what real-life scene does it become, and which Challenge activity fits (and isn't in the avoid-list)?",
  "objective": "a SINGLE short headline of the goal — 4 to 8 words, starting with a verb, NOT a full 'Students will be able to…' sentence and NOT a list. Good: 'Add two 2-digit numbers mentally'. Bad: 'Students will be able to mentally add two-digit numbers using strategies.'",
  "previousTopicRefresher": {
    "previousTopic": "the exact previous topic name given above, or null if none",
    "recap": [@@B@@, "... up to 3 total — a PAIR-START STUDENT ACTION, NOT a teacher question. First bullet: partners DO something together (hold, split, draw, guess) that quietly re-uses yesterday's skill; 'detail' gives the exact partner instruction and how they show their answer (fingers / stones / dust). Later bullets bridge into today. Never call it review — students should just feel they're playing, not being tested (e.g. text: 'Partners split one drawn ladoo into halves or quarters', detail: 'Say: "One of you draws the ladoo, the other decides halves or quarters — show me with a line." They write 1/2 and 1/4; you only name it aloud after they try.')"]
  },
  "concept": [@@B@@, "... up to 3 total — DISCOVERED, not lectured. Students find the idea in pairs BEFORE you formalize it: each 'detail' gives a peer task (Partner A does X, Partner B does Y, then switch) using real numbers, and only the LAST detail has the teacher name/confirm the idea on the board. Never open a concept bullet with 'Explain that…' — open with what the students do."],
  "explore": {
    "points": [@@B@@, "... EXACTLY 3 bullets in this order. (1) CURIOSITY HOOK — a vivid local scene built from the class's TOP interests that ends in ONE open-ended question students can't resist; 'text' is the scene, 'detail' is the teacher's opening SCRIPT in quotes ending with that question, plus one short instruction phrase in the classroom's home language (with its English in brackets). (2) PAIR-FIRST — students turn to a partner and try a guess BEFORE any explanation; 'detail' gives the exact prompt to say, the wait time, and that they answer in pairs (never cold-called alone). (3) The activity itself carrying ONE real micro-choice; 'detail' states the choice in the teacher's words (e.g. 'solve it with the stones OR draw it in the dust — your table decides') and how the concept emerges from what they tried."],
    "imageFocus": "one short phrase describing the single most useful thing to sketch on the board for this Explore activity"
  },
  "challenge": {
    "activity": "the exact name of ONE activity from the bank above, not Explore's activity, not in the avoid-list",
    "points": [@@B@@, "... EXACTLY 3 bullets as PLAY → REFLECT → ACT. (1) PLAY — how the activity runs, carrying the SECOND real micro-choice (a role, a material, or the order they go in); state it as the STUDENTS' decision, not an assignment — e.g. 'your group decides who is the number, who is the operator, who is the result', not 'Partner A is the number'. Rotate the social format vs Explore (if Explore was pairs, make this small groups or a whole-class relay). (2) REFLECT — a 30-second 'what worked for you?' turn-and-tell; 'detail' gives the exact question, which is about the easiest or most interesting bit, NEVER about mistakes. (3) ACT — apply the idea to ONE real textbook-style problem; 'detail' states the problem and its fully worked answer so the teacher never computes live."]
  },
  "sectionWatch": {
    "refresher": @@B@@ OR null if there is no refresher — the single most likely slip DURING the refresher; 'text' is the mistake as a short headline, 'detail' is the one-line fix the teacher says/does",
    "concept": @@B@@ — the single most likely misconception WHILE the concept is being discovered",
    "explore": @@B@@ — the single most likely thing that goes wrong DURING the Explore activity (a rule misunderstood, a step skipped), with the one-line fix in detail",
    "challenge": @@B@@ — the single most likely slip DURING the Challenge play, with the one-line fix in detail",
    "levelSet": @@B@@ — the single most likely wrong self-judgement at the close (e.g. thinking they've mastered it when they haven't), with the gentle one-line fix"
  },
  "materialsUsed": ["every item actually referenced across explore/challenge — nothing invented, nothing unused"],
  "levelSet": {
    "points": [@@B@@, "... EXACTLY 3 bullets, a WARM close students run themselves — NOT a test. (1) A topic-specific SELF-CHECK framed as 'Can you do this?' that partners tick together without the teacher (e.g. text: 'Tick with your partner: can you find the tenths place?', detail names the 2-3 concrete things they check — e.g. 'I found the ones place, I found the tenths place, I explained one price to my partner'). (2) A quick reflection on what was EASIEST or MOST INTERESTING today (never on errors). (3) A FINAL EITHER/OR autonomy choice that carries the idea home (e.g. 'A: draw one price you see at home tonight — OR — B: write a trick price for another pair'), framed as an invitation, not homework."]
  },
  "textbookImages": [
    { "section": "concept", "imageId": "the id of a REAL illustration listed under the textbook pages, e.g. img_c5ch3_04" },
    "... 0 to 3 items. Use these whenever a listed illustration genuinely fits a section — it is the picture the children already have in front of them, which no drawing can beat. Only ask for a drawing in "diagrams" for a section no listed picture suits. "section" must be one of "concept", "explore", "challenge", "refresher", "levelSet"; each id must be copied exactly from the list."
  ],
  "diagrams": [
    { "section": "concept", "focus": "describe ONE clean, concrete picture that makes this section's worked example intuitive using REAL relatable objects a child knows — name the exact objects, their quantities/values and arrangement (e.g. 'a roti cut into 4 equal parts with 3 parts shaded to show 3/4', or 'seven ₹1 coins grouped together and three loose ₹1 coins to show 7 + 3'). Prefer real things over abstract number lines or place-value grids. Keep it to a single idea, numbers exactly matching the example, at most a couple of short labels — never a busy multi-diagram scene." },
    "... 1 to 3 items total. ALWAYS include one for "concept" (the worked example should be seen, not only read). Add one for another section ("explore", "challenge", "refresher", "levelSet") ONLY where picturing its example genuinely makes it clearer — do not force it. "section" must be one of those exact keys; each "focus" must describe ONE clean concrete picture of THAT section's example with correct quantities — real objects over abstract diagrams, no number-line/text-banner collages. The server generates each image and attaches it to that section — do NOT describe or reference the image inside any bullet text."
  ],
  "timings": { "refresher": 3, "concept": 5, "explore": 10, "challenge": 12, "levelSet": 5 }
}

Rules:
- EVERYTHING is bullets. Every bullet list (recap, concept, explore.points, challenge.points, levelSet.points) has AT MOST 3 items.
- Each bullet's "text" is a compact 6-to-12-word headline that conveys the point. Bad (too terse): "Rounding". Bad (too long): "We often round numbers to the nearest ten before adding them together." Good: "Round each number to the nearest ten first" (with the why/example in "detail").
- "detail" carries the substance and should be present on almost every bullet (one or two sentences) — that's what appears when the teacher taps "+".
- BRIEF BUT DETAILED, ALWAYS: even the "detail" stays tight — at most 2 sentences, never a paragraph. Brief does not mean vague: pack the concrete how/number/script in, then stop. If an idea needs more than 2 sentences to land, it needs a DIAGRAM, not more text — request one in "diagrams" instead of writing longer.
- SHOW, DON'T JUST TELL: the Concept must always come with a diagram (see "diagrams"), and any section whose idea is easier to grasp as a picture should get one too. The sketch does the explaining the words don't have to.
- TEACH THE HOW, not just the what: for every explore and challenge bullet, the "detail" must give the facilitation — a short line the teacher SAYS (in quotes), how students respond (aloud / hands / pairs), a wait time where it matters, and the answer the teacher quietly listens for (per the "answers are for the teacher" rule — never announced as a verdict). A first-time teacher should not have to invent any of this.
- "objective" is ONE short verb-first headline (not a sentence, not a list). "sectionWatch" carries EXACTLY ONE watch-for per section (headline in text, one-line fix in detail) — each specific to THAT section's activity, never a generic repeat across sections. Set sectionWatch.refresher to null when there is no refresher.
- WORKED NUMBERS: whenever a bullet names a strategy or gives an example, its "detail" must SHOW the actual worked numbers, not just the label. Bad: "Break numbers apart to add." Good detail: "45 + 30 → 40 + 30 + 5 = 75." Any Challenge or example that has a computable answer MUST state that answer in its "detail" (e.g. "75 + 25 − 15 = 85"), so the teacher never has to compute it live while managing the class.
- CONSISTENCY: use ONE currency for the whole lesson — Indian rupees (₹) — never mix ₹ and $. Every concrete number the board sketch (imageFocus) shows must match the numbers used in explore's points, so a teacher copying the sketch and reading the cards sees the same values.
- GRADE-APPROPRIATE NUMBERS: the numbers in examples/Challenge must match this grade and topic — don't drop to trivially small numbers a much younger child would use. If the previous-topic refresher was about larger numbers, either bridge explicitly ("today we use the same idea on smaller numbers we can hold in our heads") or include at least one grade-level example, so the refresher and the activities don't feel mismatched.
- explore and levelSet must connect to the SAME real-life idea — levelSet returns to it, doesn't introduce a new one.
- STUDENT ACTION FIRST, EVERY SECTION: every section (refresher, concept, explore, challenge, levelSet) OPENS with something the students DO — pair up, try, decide, guess, build with objects — never with the teacher explaining. The teacher's naming/confirming of the idea comes only AFTER students have had a go. If a bullet's "text" starts with "Explain…", "Tell them…", or "Say that…", rewrite it to start with the student action.
- TWO NAMED INTERESTS: at least TWO of this class's ranked interests must appear by name in the sheet — in the hook and/or the choice options (e.g. a cricket price AND a festival sweet), not one generic "sweets". Weave them into the scene, don't just tack them on.
- HOOK, NEVER A DEFINITION: the lesson never opens by defining or stating the concept. explore's first bullet is always a curiosity hook (a puzzle/scene from the class's top interests ending in an open question); the concept emerges only after students have guessed in pairs.
- EXACTLY TWO real choices in the whole lesson — one in explore, one in challenge — each a genuine either/or the students decide (material, role, or order), phrased in the teacher's own words. Not more (it overwhelms), not zero (it removes ownership).
- ALWAYS PAIR OR GROUP, NEVER ALONE UNDER PRESSURE: students first try with a partner before answering the class; no cold-calling an individual to perform. A partner lowers the fear of being wrong.
- BILINGUAL SCAFFOLD: include exactly one short, key instruction phrase in the classroom's home/first language (with English in brackets) in explore — so the least-confident students still know what to do.
- WEAK-TOPIC SUPPORT IS INVISIBLE: if there are weak-area basics to shore up, fold them in as a natural warm-up step INSIDE the play — never as a separate 'revision' step and never named as something they're behind on. No student should be able to tell it's there for them.
- ACT ON PAST FEEDBACK, INVISIBLY: if a feedback note above says comprehension was a problem, add more worked examples and lean harder on the refresher/concept discovery steps rather than assuming mastery. If engagement was the problem, make Explore/Challenge more hands-on and choice-driven than usual. If pacing was the problem, break activities into smaller steps and keep each one shorter. Never mention "feedback" or "last time" to students — it only shapes how you build this lesson.
- REFLECT ON THE GOOD, NEVER THE BAD: every reflection asks what was easiest, what worked, or what was most interesting — never what they got wrong. Competence grows from noticing success.
- ANSWERS ARE FOR THE TEACHER, NOT A VERDICT: the "detail" must still tell the teacher the worked answer so they're confident — but the SCRIPT spoken to students stays invitational. Never write "the answer is X", "they should show X", or "they respond, saying it's X". Instead frame it as what to LISTEN/WATCH for and what partners DISCUSS. Bad: "They respond aloud, saying it's 100/100." Good: "Listen for partners landing on about 100/100 — if one says 99 and one says 101, ask how they counted. There's no single 'right way' to show it with fingers."
- KEEP EXAMPLES AT THE CLEAREST SCALE: choose the smallest, cleanest numbers that make the idea obvious — don't inflate them in a way that buries the concept under arithmetic. For decimals/place value, show hundredths as ₹0.50 = 50 hundredths (50 of 100 equal parts of 1 rupee), NOT ₹50 = 5000 hundredths. The number should illuminate the concept, not test stamina.
- DON'T REFERENCE THE IMAGE IN TEXT: never write "see the image / picture above / reference chart" in any "text" or "detail" — each bullet must stand on its own in words. The picture is generated and shown separately (request it in "diagrams"); the words carry what to say and do, the image does the visual work — the two never point at each other.
- NAME THE RESOURCE, not the category: say "use the stones / bottle caps / chalk lines", not "use the materials" — the teacher and students should know the exact object to pick up.
- Every step in explore/challenge must only need @@M@@. materialsUsed must list exactly what was actually used.
- "timings": rough whole-minute estimate per section (omit "refresher" if previousTopicRefresher is null). They should sum to roughly one class period (about 35 minutes).
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- The visible bullet lines together must be scannable in under 2 minutes; the details are there for when the teacher wants more.

Before returning: confirm "challenge.activity" is copied character-for-character from the bank (not from Explore, not from the avoid-list); confirm every bullet array has at most 3 items; confirm every material named in explore/challenge appears in "materialsUsed"; confirm one currency is used throughout and any computable answer is stated in its bullet's "detail"."""


# ── JSON salvage ─────────────────────────────────────────────────────────────
# gemini-2.5-flash, even in JSON mode, frequently drops the comma between two
# adjacent array/object elements. This walks the raw text tracking string
# state (respecting \" escapes) and inserts the missing comma ONLY at genuine
# structural boundaries. Tried before re-calling the model, so most malformed
# responses parse on the first attempt instead of costing a retry.
def _insert_missing_commas(s: str) -> str:
    out = []
    in_str = False
    for i, c in enumerate(s):
        out.append(c)
        if c == '"' and (i == 0 or s[i - 1] != "\\"):
            in_str = not in_str
        if in_str:
            continue
        if c in ("}", "]", '"'):
            j = i + 1
            while j < len(s) and s[j].isspace():
                j += 1
            nxt = s[j] if j < len(s) else ""
            if nxt in ("{", "[", '"'):
                out.append(",")
    return "".join(out)


def _strip_trailing_commas(s: str) -> str:
    return re.sub(r",(\s*[}\]])", r"\1", s)


def _looks_like_lesson(o) -> bool:
    if not isinstance(o, dict):
        return False
    return (
        isinstance(o.get("concept"), list)
        and isinstance(o.get("explore"), dict)
        and isinstance(o.get("challenge"), dict)
        and isinstance(o.get("levelSet"), dict)
    )


def _try_parse_lesson(raw: str) -> Optional[dict]:
    cleaned = re.sub(r"^```json\s*", "", raw, flags=re.I)
    cleaned = re.sub(r"```\s*$", "", cleaned, flags=re.I).strip()
    obj_match = re.search(r"\{[\s\S]*\}", cleaned)
    if obj_match:
        cleaned = obj_match.group(0)
    candidates = [
        cleaned,
        _insert_missing_commas(cleaned),
        _strip_trailing_commas(cleaned),
        _strip_trailing_commas(_insert_missing_commas(cleaned)),
    ]
    for c in candidates:
        try:
            parsed = json.loads(c)
        except Exception:
            continue
        if _looks_like_lesson(parsed):
            return parsed
    return None


async def _repair_raw_json(raw: str, api_key: str) -> str:
    """Last-resort salvage: hand the raw, unparseable text to the model and ask
    ONLY for valid JSON back. Fires only after every deterministic attempt AND
    the whole-call retries have failed."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "EduTeach Prep Material (json-fix)"},
                json={
                    "model": os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"),
                    "messages": [
                        {"role": "system", "content": "You are given text that is meant to be a single JSON object but has syntax errors. Return the SAME content as valid, parseable JSON — fix only the syntax (quotes, commas, escaping, braces). Do not change, translate, add, or remove any of the actual content. Return ONLY the JSON, no markdown."},
                        {"role": "user", "content": raw},
                    ],
                    "temperature": 0,
                    "max_tokens": 4000,
                    "response_format": {"type": "json_object"},
                },
            )
        if resp.status_code >= 400:
            return ""
        data = resp.json()
        return (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
    except Exception:
        return ""


# ── Tier 2: deterministic post-generation validation + targeted repair ──────
BANNED_WORDS = ["quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite"]


def _sanitize_lesson(lesson: dict, resources: list) -> dict:
    """Silent fix — drops any materialsUsed entry that isn't actually in the
    profile's resource list, rather than failing or leaving an invented item."""
    if not resources or not isinstance(lesson.get("materialsUsed"), list):
        return lesson
    allowed = {r.strip().lower() for r in resources}
    lesson = dict(lesson)
    lesson["materialsUsed"] = [m for m in lesson["materialsUsed"] if isinstance(m, str) and m.strip().lower() in allowed]
    return lesson


def _bullets_ok(bullets) -> bool:
    return (
        isinstance(bullets, list) and 1 <= len(bullets) <= 3
        and all(isinstance(b, dict) and isinstance(b.get("text"), str) and len(b["text"]) > 0 for b in bullets)
    )


def _validate_lesson(lesson: dict, resources: list, avoid_activities: list) -> list[str]:
    issues = []

    refresher = lesson.get("previousTopicRefresher")
    if refresher is not None and not _bullets_ok(refresher.get("recap")):
        issues.append("previousTopicRefresher.recap must be 1-3 {text, detail?} bullets (or the whole field null)")

    if not _bullets_ok(lesson.get("concept")):
        issues.append(f"concept must be 1-3 {{text, detail?}} bullets, got {json.dumps(lesson.get('concept'))}")

    explore = lesson.get("explore") or {"points": []}
    if not _bullets_ok(explore.get("points")):
        issues.append("explore.points must be 1-3 {text, detail?} bullets")

    challenge = lesson.get("challenge") or {"activity": "", "points": []}
    activity = challenge.get("activity") if isinstance(challenge.get("activity"), str) else ""
    if not activity:
        issues.append("challenge.activity is missing")
    if not _bullets_ok(challenge.get("points")):
        issues.append("challenge.points must be 1-3 {text, detail?} bullets")
    if activity and any(isinstance(a, str) and a.strip().lower() == activity.strip().lower() for a in avoid_activities):
        issues.append(f'challenge.activity "{activity}" was used recently for this class and must be different')

    level = lesson.get("levelSet") or {"points": []}
    if not _bullets_ok(level.get("points")):
        issues.append("levelSet.points must be 1-3 {text, detail?} bullets")

    text_blob = json.dumps(lesson).lower()
    for w in BANNED_WORDS:
        if re.search(rf"\b{w}\b", text_blob):
            issues.append(f'contains banned word "{w}"')

    if resources and isinstance(lesson.get("materialsUsed"), list):
        allowed = {r.strip().lower() for r in resources}
        invented = [m for m in lesson["materialsUsed"] if isinstance(m, str) and m.strip().lower() not in allowed]
        if invented:
            issues.append(f"materialsUsed includes items not in the resource list: {', '.join(invented)}")

    return issues


async def _repair_lesson(lesson: dict, issues: list[str], api_key: str) -> dict:
    """Escalation — only reached when _validate_lesson still finds real
    violations after _sanitize_lesson's silent fixes. Falls back to the
    original lesson if the repair call itself fails or returns malformed JSON."""
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "EduTeach Prep Material (repair)"},
                json={
                    "model": os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"),
                    "messages": [
                        {"role": "system", "content": "You are given a lesson JSON and a list of specific rule violations found in it. Return the SAME JSON with ONLY the minimal edits needed to fix each listed violation — do not rewrite fields that weren't flagged, do not change the chosen activity name unless it was flagged as invalid, do not add commentary."},
                        {"role": "user", "content": f"Lesson JSON:\n{json.dumps(lesson)}\n\nViolations to fix:\n" + "\n".join(f"- {i}" for i in issues) + "\n\nReturn ONLY the corrected JSON, same shape, no markdown."},
                    ],
                    "temperature": 0.3,
                    "max_tokens": 4000,
                },
            )
        if resp.status_code >= 400:
            return lesson
        data = resp.json()
        raw = (data.get("choices") or [{}])[0].get("message", {}).get("content", "")
        return _try_parse_lesson(raw) or lesson
    except Exception:
        return lesson


def _preferences_context(teaching_profile: Optional[dict]) -> str:
    if not teaching_profile:
        return "No Teaching Profile on file yet — default to a simple, universally comfortable activity (storytelling + pair work)."

    identity = teaching_profile.get("teacherIdentity") or {}
    classroom = teaching_profile.get("classroom") or {}
    roles = identity.get("roles") or []
    goals = identity.get("goals") or []
    preferred_activities = identity.get("preferredActivities") or []
    comfort_zones = identity.get("comfortZones") or []
    class_size = classroom.get("classSize")
    resources = classroom.get("resources") or []
    language = classroom.get("language") or []
    style_line = personalization_tier_line(teaching_profile.get("personalization"))

    line_parts = [
        f"Sees themselves as: {', '.join(roles)}." if roles else None,
        f"Wants students to: {', '.join(goals)}." if goals else None,
        f"Enjoys using: {', '.join(preferred_activities)}." if preferred_activities else None,
        f"Comfortable with: {', '.join(comfort_zones)}." if comfort_zones else None,
        f"Typical class size: {class_size}." if class_size else None,
        f"Classroom has: {', '.join(resources)}." if resources else None,
        f"Classroom language: {', '.join(language)}." if language else None,
        style_line or None,
    ]
    lines = " ".join(p for p in line_parts if p)

    resource_rule = (
        f'"materials" must be chosen ONLY from this exact list: {", ".join(resources)} — never add chalk, slate, paper, or any other item not on this list, even if it seems like a harmless default.'
        if resources else
        "No resources were listed for this classroom — use only the single most generic, universally-available item (e.g. chalkboard) and nothing else."
    )

    if class_size == ">40":
        class_size_rule = "LARGE CLASS (40+): favour whole-class and small-group formats, choral/hands-up responses, and pair work over many individual turns. Every step must be runnable by ONE teacher with a big group — no step that needs checking 40 notebooks one by one."
    elif class_size == "<20":
        class_size_rule = "SMALL CLASS (under 20): alongside the pair work you can use more individual turns and closer one-to-one moments."
    elif class_size == "20-40":
        class_size_rule = "MEDIUM CLASS (20–40): mix pair work with a few individual turns; keep grouping simple to manage."
    else:
        class_size_rule = ""

    comfort_has_groups = any(re.search(r"group", c, re.I) for c in comfort_zones)
    comfort_pair_rule = (
        "This teacher is NOT comfortable running group discussions. Keep ALL collaboration to QUIET PAIR work (turn-and-tell with one partner). Do NOT require whole-class group discussions or students presenting in front of the class — the mandatory pairing must stay low-key and easy to manage."
        if comfort_zones and not comfort_has_groups else ""
    )

    preferred_bias = (
        f"When picking the Challenge activity from the bank, prefer the one that best resembles the teacher's preferred activities ({', '.join(preferred_activities)}) wherever a sensible match exists — without breaking the rotation/avoid-list rule."
        if preferred_activities else ""
    )

    return (
        f"This teacher's profile — {lines} Explore and Challenge MUST reflect this profile, not just decorate a fixed structure. "
        f"{resource_rule} {class_size_rule} {comfort_pair_rule} {preferred_bias} Never suggest something outside the teacher's comfort zone."
    )


# Everything from prompt-building through a validated, sanitized lesson dict —
# no class_id, no DB writes, no images. Split out of the /smart-lesson route so
# the batch generator (app/lib/prep_batch_jobs.py) can produce the exact same
# lesson quality for a shared (grade, subject) batch, supplying its context a
# different way (pooled/grade-wide instead of one class's roster) without
# duplicating a single line of the prompt or the validate/repair logic.
async def generate_lesson_core(
    *, topic: str, subject: str, grade: str, subtopic: Optional[str],
    total_students: Optional[int], class_interests: list, weak_topics: list,
    teaching_profile: Optional[dict], grounding: Optional[dict], textbook: Optional[dict],
    previous_topic: Optional[str], avoid_activities: list, feedback_context: dict,
    context_note: Optional[str],
) -> dict:
    if grounding:
        chapter_line = ""
        if grounding.get("chapterTitle"):
            page_part = (
                f" (pages {grounding['pageStart']}-{grounding.get('pageEnd') or grounding['pageStart']})"
                if grounding.get("pageStart") else ""
            )
            chapter_line = f'Chapter: "{grounding["chapterTitle"]}"{page_part}'
        exercises_line = ""
        if grounding.get("exercises"):
            ex_list = "\n".join(f'- [{e["type"]}] {e["text"]}' for e in grounding["exercises"])
            exercises_line = f"\nActual exercises/activities in the textbook for this topic:\n{ex_list}"
        sidebars_line = ""
        if grounding.get("sidebars"):
            sb_list = "\n".join(f"- {s}" for s in grounding["sidebars"])
            sidebars_line = f"\nActual sidebar notes/tips printed alongside this topic:\n{sb_list}"
        grounding_context = (
            "This topic comes from an actual textbook chapter that has already been analysed. Ground the concept bullets and Explore/Challenge in this REAL content instead of inventing generic material:\n"
            f"{chapter_line}\n{exercises_line}\n{sidebars_line}"
        )
    else:
        grounding_context = "No textbook extraction is available — use standard grade-appropriate concepts, nothing exotic."

    preferences_context = _preferences_context(teaching_profile)

    weak_topics_context = (
        f"This class is weak on: {', '.join(weak_topics)}. If it fits naturally, let Explore or Challenge also reinforce one of these — but never mention them explicitly or call it review."
        if weak_topics else ""
    )

    context_note_line = f"Teacher's note for today: {context_note.strip()}" if context_note and context_note.strip() else ""

    feedback_context_line = " ".join(p for p in [
        f"From this class's last session on this exact topic: {feedback_context['topicInsight']}" if feedback_context.get("topicInsight") else "",
        f"This class's general tendencies from past feedback: {feedback_context['classProfile']}" if feedback_context.get("classProfile") else "",
    ] if p)

    materials_rule = (
        "the teacher's listed classroom resources above"
        if (teaching_profile or {}).get("classroom", {}).get("resources")
        else "chalk, blackboard, notebooks, or everyday found objects — nothing else"
    )

    interests_line = (
        f"THIS CLASS IS INTO: {', '.join(class_interests)}. This is your strongest engagement lever — build the Explore scenario, the refresher's examples, and (where natural) the Challenge around what these kids actually love. Make the examples situation-specific and concrete (\"counting runs in our cricket match\", \"sharing sweets at Diwali\", \"kites at Sankranti\"), not generic. Rotate which interest you lean on rather than forcing all of them in."
        if class_interests else ""
    )

    if previous_topic:
        previous_topic_line = f"""The topic this class covered just before this one was: "{previous_topic}". Include previousTopicRefresher ONLY IF a skill from "{previous_topic}" is genuinely used in today's topic ("{topic}") — i.e. a student who forgot it would actually struggle today. If the two topics are unrelated (different strands that merely sit near each other in the syllabus), set "previousTopicRefresher" to null and omit the "refresher" timing. Do NOT manufacture a bridge between unrelated topics — a lesson with no refresher is much better than a recap that pretends one topic leads into another.
When you DO include it: open it as a PAIR-START STUDENT ACTION — partners DO something together (hold, split, draw, guess with objects) that quietly re-uses the old skill BEFORE anyone explains, ending with a one-line bridge into today. Never a teacher quiz-question. Make each recap bullet a SITUATION-SPECIFIC example, not an abstract restatement — set it in something this class cares about (see their interests above) so it reads like "partners split the scoreboard runs in half" rather than "remember place value"."""
    else:
        previous_topic_line = 'No topic that leads into this one is on record for this class. Set "previousTopicRefresher" to null and omit the "refresher" timing.'

    avoid_line = (
        f"Do NOT pick any of these activities for the Challenge — they were used recently for this class and must not repeat: {', '.join(avoid_activities)}. Pick a genuinely DIFFERENT one from the bank below."
        if avoid_activities else ""
    )

    subject_module = resolve_subject_module(subject)
    engagement_guidance = engagement_level_guidance(grade)

    system_prompt = f"""You are an expert teacher-trainer designing a classroom-ready lesson for a resource-constrained school (government or NGO-run, India). A teacher opens this five minutes before class and follows it directly.

{LOW_RESOURCE_PRINCIPLES}

{engagement_guidance}

{_SDT_ENGAGEMENT}

{subject_module.bank}

IMPORTANT — the bank above is used differently in this template than its own header text says:
- "explore" is NOT limited to the bank. Invent a vivid, highly creative real-life scenario and activity that fits this specific topic and teacher profile — it should feel like a mini-story the class steps into, not a generic exercise. Still respect the low-resource rules and the profile's comfort zones.
- "challenge" MUST pick exactly one activity, by its exact name, from the bank above — this is the one structured, rotated part of the lesson. Never reuse Explore's activity for the Challenge.

If instructions below conflict, resolve in this order: (1) the low-resource constraints and any teacher materials list always win, (2) the teacher's profile and comfort zones, (3) everything else.

FORMAT — this is strict and applies to EVERY section:
- The ENTIRE lesson is bullet points. There are no paragraphs anywhere. Every field of content is an array of {_BULLET_SHAPE}.
- Each bullet's "text" is a compact, informative headline — about 6 to 12 words that convey the actual point at a glance. Not a cryptic 2-3 word fragment, and not a full multi-clause sentence — a line a teacher reads and immediately gets.
- The real explanation ALWAYS goes in that bullet's "detail" (one or two sentences), never in "text". The teacher reads the headline to scan, then taps "+" to get the detail.
- If a "text" line reads like a full sentence, it is too long — cut it down to the headline and move the sentence into "detail".

{subject_module.pedagogy}"""

    header = f"""Write a Prep Sheet for:

Topic: {topic}{f'{chr(10)}Subtopic (focus specifically on this): {subtopic}' if subtopic else ''}
Subject: {subject}
Grade: {grade}
Class size: {total_students or 'unknown'}

{interests_line}

{previous_topic_line}

{textbook_prompt_block(textbook) if textbook else ''}

{grounding_context}

{preferences_context}
{weak_topics_context}
{context_note_line}
{feedback_context_line}
{avoid_line}

This is a TEACHER'S prep sheet, not a student handout. The visible headline says WHAT happens; every "detail" must tell the teacher HOW to run it — what to actually SAY (a short script line in quotes), how long to wait, whether students answer aloud/in pairs/by hands, the answer the teacher should quietly listen for (as a check, never announced to students as a verdict), and what to do if they struggle. A first-time teacher should be able to follow it without improvising."""

    user_prompt = header + "\n\n" + _LESSON_SHAPE_AND_RULES.replace("@@B@@", _BULLET_SHAPE).replace("@@M@@", materials_rule)

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI not configured")

    # gemini-2.5-flash still emits malformed JSON on a meaningful fraction of
    # calls even with JSON mode on. Rather than surfacing "Failed to generate",
    # ask for JSON mode AND retry the whole call a couple of times — a fresh
    # sample almost always parses. One shot on the primary, then a DIFFERENT
    # provider (strong at multilingual structured JSON) for the rest, since a
    # 200 response with a near-empty/truncated body doesn't trigger OpenRouter's
    # own failover (that only fires on request-level 5xx/provider-down).
    primary_model = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
    fallback_model = os.environ.get("OPENROUTER_LESSON_FALLBACK_MODEL", "openai/gpt-4o-mini")
    attempt_models = [primary_model, fallback_model, fallback_model]

    lesson: Optional[dict] = None
    last_raw = ""
    last_error = ""
    truncated = False

    for attempt, model in enumerate(attempt_models):
        if lesson:
            break
        if attempt > 0:
            await asyncio.sleep(0.4 * attempt)

        try:
            async with httpx.AsyncClient(timeout=90) as client:
                ai_res = await client.post(
                    "https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "EduTeach Prep Material"},
                    json={
                        "model": model,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                        "temperature": 0.75,
                        "max_tokens": 4000,
                        "response_format": {"type": "json_object"},
                    },
                )
        except httpx.TimeoutException as e:
            last_error = f"AI request timed out: {e}"
            continue

        if ai_res.status_code >= 400:
            last_error = ai_res.text
            continue

        ai_data = ai_res.json()
        choice = (ai_data.get("choices") or [{}])[0]
        last_raw = (choice.get("message") or {}).get("content", "")
        finish = choice.get("finish_reason")
        truncated = finish == "length"
        lesson = _try_parse_lesson(last_raw)
        if not lesson:
            print(f"[smart-lesson] unparseable JSON on attempt {attempt + 1} (model={model}, finish_reason={finish}), retrying")

    if not lesson and last_raw and not truncated:
        print("[smart-lesson] all attempts unparseable, trying model JSON repair")
        lesson = _try_parse_lesson(await _repair_raw_json(last_raw, api_key))

    if not lesson:
        print(f"[smart-lesson] gave up — raw head: {json.dumps(last_raw[:500])}")
        print(f"[smart-lesson] gave up — raw tail: {json.dumps(last_raw[-300:])}")
        reason = (
            f"AI error: {last_error}" if last_error
            else "The lesson was cut off before it finished generating. Please try again." if truncated
            else "AI returned malformed JSON"
        )
        raise HTTPException(status_code=500, detail=reason)

    resources = ((teaching_profile or {}).get("classroom") or {}).get("resources") or []
    lesson = _sanitize_lesson(lesson, resources)
    issues = _validate_lesson(lesson, resources, avoid_activities)
    if issues:
        print(f"[smart-lesson] Tier 2 violations, repairing: {issues}")
        lesson = _sanitize_lesson(await _repair_lesson(lesson, issues, api_key), resources)

    return lesson


# Reference illustrations — clean workbook-style figures that depict the
# section's SPECIFIC worked example accurately, which the teacher SHOWS
# students to explain that example. Hangs off the FIRST bullet of its section.
# Generated in PARALLEL so several images cost about the same wall-time as
# one, capped at 4. Never raises — a failed/timed-out image just leaves that
# bullet image-less. illustration_key_parts is the caller's own identity for
# the storage key — [class_id, topic, subtopic] for the live per-class route,
# [school_id, grade, subject, topic] for the shared batch generator, since the
# image belongs to whichever content actually used it. Mutates lesson in place.
async def generate_lesson_images(lesson: dict, textbook: Optional[dict], admin, illustration_key_parts: list) -> None:
    section_first_bullet = {
        "refresher": ((lesson.get("previousTopicRefresher") or {}).get("recap") or [None])[0],
        "concept": (lesson.get("concept") or [None])[0],
        "explore": ((lesson.get("explore") or {}).get("points") or [None])[0],
        "challenge": ((lesson.get("challenge") or {}).get("points") or [None])[0],
        "levelSet": ((lesson.get("levelSet") or {}).get("points") or [None])[0],
    }

    # Real textbook illustrations come first and win their section outright.
    used_textbook_sections = set()
    if textbook and textbook.get("images"):
        by_anchor = {i["imageId"]: i for i in textbook["images"]}
        picks = lesson.get("textbookImages") if isinstance(lesson.get("textbookImages"), list) else []
        for pick in picks:
            if not isinstance(pick, dict) or not isinstance(pick.get("section"), str) or not isinstance(pick.get("imageId"), str):
                continue
            image = by_anchor.get(pick["imageId"].strip())
            bullet = section_first_bullet.get(pick["section"])
            if not image or not bullet or pick["section"] in used_textbook_sections:
                continue
            bullet["image"] = {"url": f"/api/textbook-image/{image['id']}"}
            used_textbook_sections.add(pick["section"])

    diagram_reqs: list[dict] = []

    def add_req(section: str, focus: Optional[str]):
        if not focus or not focus.strip():
            return
        if not section_first_bullet.get(section):
            return
        if section in used_textbook_sections:
            return
        if any(r["section"] == section for r in diagram_reqs):
            return
        diagram_reqs.append({"section": section, "focus": focus.strip()})

    model_diagrams = [
        d for d in (lesson.get("diagrams") if isinstance(lesson.get("diagrams"), list) else [])
        if isinstance(d, dict) and isinstance(d.get("section"), str) and isinstance(d.get("focus"), str)
    ]

    def focus_for(section: str) -> Optional[str]:
        return next((d["focus"] for d in model_diagrams if d["section"] == section), None)

    # Concept, Explore AND the Challenge activity ALWAYS get a picture, queued
    # FIRST so they survive the image cap even when the model requested
    # diagrams for other sections.
    concept_bullet = section_first_bullet.get("concept") or {}
    add_req("concept", focus_for("concept") or concept_bullet.get("text"))
    explore_bullet = section_first_bullet.get("explore") or {}
    add_req("explore", focus_for("explore") or (lesson.get("explore") or {}).get("imageFocus") or explore_bullet.get("text"))
    challenge_bullet = section_first_bullet.get("challenge") or {}
    add_req("challenge", focus_for("challenge") or challenge_bullet.get("text") or (lesson.get("challenge") or {}).get("activity"))
    for d in model_diagrams:
        add_req(d["section"], d["focus"])
    final_reqs = diagram_reqs[:4]

    async def _generate_for(r: dict):
        bullet = section_first_bullet.get(r["section"])
        example_text = " — ".join(p for p in [(bullet or {}).get("text"), (bullet or {}).get("detail")] if p)
        img_prompt = f"""A clean, friendly primary-school WORKBOOK illustration that explains ONE worked example using concrete, real, relatable objects a child recognises — so the idea is obvious at a glance while a teacher points at it.

Illustrate this: {r['focus']}
It must accurately match this example from the lesson: "{example_text}"

Requirements:
- Use CONCRETE real-life objects to make the idea intuitive (coins, notes, fruits, rotis, sweets, blocks, groups of children, a pizza/roti cut into parts, etc.). AVOID abstract number lines, place-value grids, and bar charts — a picture of real things explains better than a diagram. Only use a number line if the concept is literally about position/order on a line.
- Show ONE single clear representation. Do NOT crowd several diagrams into one image — no "number-line + text banner + coin piles" collages. One clean idea with plenty of white space.
- Quantities must be EXACTLY right and countable: if the example says 7, show exactly 7; the picture must be arithmetically correct and the coin/object values must match the example (₹0.50 is half a rupee, NOT ₹50).
- At most a few short, correctly-spelled labels or the single key number, placed neatly — NO long sentences, paragraphs, or explanation banners inside the image.
- Plain white background, bright flat colours, bold simple shapes; nothing decorative or unrelated, no watermarks or logos.
Clean, accurate, and genuinely explanatory — a child should understand the idea just by looking."""
        generated = await generate_illustration(img_prompt, timeout_s=25)
        if not generated or not bullet:
            return
        # Into storage, not into the row — inlining the base64 is what made a
        # lesson 3MB.
        stored = await store_illustration(admin, generated["url"], illustration_key(illustration_key_parts + [r["section"]]))
        bullet["image"] = {"url": stored or generated["url"]}

    await asyncio.gather(*(_generate_for(r) for r in final_reqs))


# POST /api/smart-lesson
@router.post("/smart-lesson")
async def smart_lesson(body: SmartLessonSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    t0 = time.time()
    class_id, topic, subject, grade = body.classId, body.topic, body.subject, body.grade
    subtopic, topic_definition_id, teacher_id = body.subtopic, body.topicDefinitionId, body.teacherId
    context_note = body.contextNote

    admin = create_admin_client()

    # 1-3. Class size + interests, teaching profile, silent gap-awareness
    # (weak prior topics, excluding this exact topic, top 2).
    ctx = await gather_class_context(admin, class_id, teacher_id, exclude_topic=topic, weak_topic_limit=2)
    total_students = ctx["totalStudents"]
    class_interests = ctx["classInterests"]
    weak_topics = ctx["weakTopics"]
    teaching_profile = ctx["teachingProfile"]

    # 4. Grounding — two sources, and they answer different questions.
    # fetch_grounding gives the ontology's summary (exercises, sidebars, pages).
    # fetch_textbook_grounding gives the book's own words and its illustrations.
    grounding = await fetch_grounding(admin, topic_definition_id) if topic_definition_id else None
    class_row = (
        admin.table("classes").select("school_id").eq("id", class_id).maybe_single().execute()
    ).data
    textbook = await fetch_textbook_grounding(
        admin, (class_row or {}).get("school_id"), str(grade), subject, topic, subtopic,
    )

    # 5. Previous topic (for the refresher) + recent Challenge activities (rotation)
    previous_topic = await fetch_previous_topic(admin, class_id, topic, topic_definition_id, subtopic, subject)
    avoid_activities = await fetch_recent_challenge_activities(admin, class_id)

    # 5b. Post-class feedback from last time.
    feedback_context = await fetch_feedback_context(admin, class_id, topic)

    lesson = await generate_lesson_core(
        topic=topic, subject=subject, grade=grade, subtopic=subtopic,
        total_students=total_students, class_interests=class_interests, weak_topics=weak_topics,
        teaching_profile=teaching_profile, grounding=grounding, textbook=textbook,
        previous_topic=previous_topic, avoid_activities=avoid_activities,
        feedback_context=feedback_context, context_note=context_note,
    )
    await generate_lesson_images(lesson, textbook, admin, [class_id, topic, subtopic])

    api_log("smart-lesson", ip, (time.time() - t0) * 1000, False, "ok")
    return {
        "topic": topic,
        "subtopic": subtopic or None,
        "subject": subject,
        "grade": grade,
        "totalStudents": total_students,
        "lesson": lesson,
    }
