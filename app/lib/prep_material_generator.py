"""Stage 6 of the prep-material pipeline (see PROMPTS.md's design doc): the
Prompt Assembly Engine that combines curriculum knowledge (Phase B), pedagogy
knowledge (Phase C), and teacher constraints into one prompt, then calls the
LLM to produce an actual prep material.

    Curriculum facts (Phase B)  ─┐
    Pedagogy library (Phase C)  ─┼─> Prompt Assembly Engine ─> LLM ─> Prep Material
    Teacher constraints         ─┘

This is a NEW, parallel pipeline for this pilot — it does not touch or replace
the existing production /api/smart-lesson route (that route's Python port is
a much thinner stand-in for a ~900-line original; see the migration notes).
Keeping this separate means iterating on these prompts can't regress the
feature real teachers currently use.

Also includes extract_knowledge_from_text() — a text-only counterpart to
vision_extraction's per-topic semantic extraction (concepts/competencies/
vocabulary/learning_outcomes/contexts/bloom/difficulty), for when the source
is pasted topic content rather than a PDF page image. Both paths write the
same shape, so canonical_mapping.resolve_canonical() and
pedagogy_library.get_recommended_activities() work identically regardless of
where the knowledge came from.
"""

import json
import random
import re
from typing import Optional

from .ai import call_ai

VALID_BLOOM_LEVELS = frozenset({
    "remember", "understand", "apply", "analyze", "evaluate", "create",
})
VALID_DIFFICULTIES = frozenset({"easy", "medium", "hard"})


def grade_band_for(grade) -> Optional[str]:
    """The Pedagogy Library's grade_band for a numeric grade, or None if the
    grade falls outside the pilot's two bands (migration 022 constrains
    activity_templates.grade_band to '1-3' / '4-5'). None means "don't filter
    by band" rather than "no activities" — a grade 6 lookup still returns
    whatever matches on competency alone."""
    try:
        g = int(str(grade).strip())
    except (TypeError, ValueError):
        return None
    if 1 <= g <= 3:
        return "1-3"
    if 4 <= g <= 5:
        return "4-5"
    return None


def _strip_fences(text: str) -> str:
    text = (text or "").strip()
    m = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text)
    return (m.group(1) if m else text).strip()


def _to_list(value) -> list:
    if not value:
        return []
    if isinstance(value, str):
        value = [v.strip() for v in value.split(",")]
    return [v for v in (str(x).strip() for x in value) if v]


def _parse_json_response(raw: str, context: str) -> dict:
    """json.loads(_strip_fences(raw)), but a failure carries the raw text with
    it. A bare JSONDecodeError only says where parsing broke, which is useless
    for telling "the API call itself errored and this is an error message or
    refusal" apart from "the model just wrote malformed JSON" — and those need
    different fixes (check credentials/credits vs. tighten the prompt).
    Also the only place that would reveal call_ai()'s silent fallback: on any
    primary-model failure it retries the free FALLBACK_MODEL with json_mode
    turned OFF, so a parse failure here is a real candidate for "that's not
    Gemini answering, it's the free backup model, without JSON enforced."""
    cleaned = _strip_fences(raw)
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError as exc:
        # A fixed head-snippet is useless once the response is longer than it
        # — the actual break (exc.pos) is what matters, not character 0, so
        # show a window straddling it instead. Also report the total length:
        # a response that's suspiciously short (worked example: under 200
        # chars) points at a truncated/dropped API response, not bad prompt
        # engineering — those need different fixes.
        window_start = max(0, exc.pos - 300)
        window_end = min(len(cleaned), exc.pos + 300)
        window = cleaned[window_start:window_end]
        raise ValueError(
            f"{context}: model response wasn't valid JSON ({exc}).\n"
            f"total response length: {len(cleaned)} chars\n"
            f"--- window around char {exc.pos} (chars {window_start}-{window_end}) ---\n"
            f"{'…' if window_start > 0 else ''}{window}{'…' if window_end < len(cleaned) else ''}\n"
            f"--- end ---"
        ) from exc


# ── Stage 1 (text path): knowledge extraction from pasted content ───────────

_EXTRACTION_PROMPT = """You are an expert educational architect analyzing a single topic's teaching content.

TOPIC: {topic}
SUBTOPIC: {subtopic}
GRADE: {grade}
SUBJECT: {subject}

CONTENT:
{content}

Extract the underlying educational knowledge in this content — not activities,
just what a curriculum expert would call the ideas/skills/words involved:
- concepts: the underlying idea(s) being taught (e.g. "Addition", "Counting")
- competencies: the specific skill(s) a student practices, phrased as an action
  a student does (e.g. "Count Objects", not "Counting")
- vocabulary: new/key words this topic introduces or relies on
- learning_outcomes: what a student can DO after this topic — short action
  phrases, not sentences
- contexts: the SPECIFIC, CONCRETE real-world things the content actually names
  or clearly implies — e.g. "Mangoes", "Tamarind Seeds", "Kirana Shop",
  "Rangoli". NEVER invent a generic category like "Real-life objects",
  "Everyday objects", or "Fruit" as a stand-in for a specific noun — if the
  content only says something generic like "objects" or "things" with no
  actual named example, return an empty list rather than abstracting one
- bloom_level: the single Bloom's-taxonomy level this topic mainly targets —
  one of remember | understand | apply | analyze | evaluate | create
- difficulty: easy | medium | hard, relative to this grade level

Leave any field as an empty list (or bloom_level/difficulty omitted) rather
than guessing if the content doesn't support it.

Return ONLY valid JSON, no markdown fences:
{{
  "concepts": [], "competencies": [], "vocabulary": [], "learning_outcomes": [],
  "contexts": [], "bloom_level": null, "difficulty": null
}}
"""


async def extract_knowledge_from_text(
    topic: str, subtopic: str, content: str, grade: str, subject: str,
) -> dict:
    """Stage 1 for pasted text instead of a PDF page image. Returns the same
    shape vision_extraction's per-topic fields do, so the rest of the pipeline
    (canonical mapping, pedagogy lookup) doesn't care which path produced it.
    """
    prompt = _EXTRACTION_PROMPT.format(
        topic=topic, subtopic=subtopic or "(none)", grade=grade, subject=subject, content=content,
    )
    raw = await call_ai(
        [{"role": "user", "content": prompt}],
        {"json_mode": True, "temperature": 0.2, "max_tokens": 1500},
    )
    data = _parse_json_response(raw, "Stage 1 extraction")

    bloom = (data.get("bloom_level") or "").strip().lower() or None
    if bloom and bloom not in VALID_BLOOM_LEVELS:
        bloom = None
    difficulty = (data.get("difficulty") or "").strip().lower() or None
    if difficulty and difficulty not in VALID_DIFFICULTIES:
        difficulty = None

    return {
        "concepts": _to_list(data.get("concepts")),
        "competencies": _to_list(data.get("competencies")),
        "vocabulary": _to_list(data.get("vocabulary")),
        "learning_outcomes": _to_list(data.get("learning_outcomes")),
        "contexts": _to_list(data.get("contexts")),
        "bloom_level": bloom,
        "difficulty": difficulty,
    }


# ── Stage 4/5: activity + context selection ──────────────────────────────────

def select_activity_and_context(
    activities: list, preferred_context: Optional[str] = None,
) -> tuple:
    """Stage 4 (Activity Selection) + Stage 5 (Context Engine) — the design doc
    is explicit that context selection happens before the LLM call, not inside it.

    Picks the activity with the most matched competencies (best curriculum
    fit); ties broken by declaration order. Within that activity's contexts,
    matches `preferred_context` by name or category (case-insensitive
    substring); with no preference (or no match), picks RANDOMLY among the
    available contexts rather than always the first — a template like "Count
    Real Objects" carries ten interchangeable contexts specifically so
    different lessons on the same competency don't all land on the same one
    (e.g. every unpreferenced call landing on "Tamarind Seeds" because it
    happened to be first in the list is the bug this avoids, not a feature).

    Returns (activity, context) — both None if `activities` is empty.
    """
    if not activities:
        return None, None

    best = max(activities, key=lambda a: len(a.get("matchedCompetencyIds") or []))
    contexts = best.get("contexts") or []
    if not contexts:
        return best, None

    if preferred_context:
        needle = preferred_context.strip().lower()
        for ctx in contexts:
            if needle in (ctx.get("name") or "").lower() or needle in (ctx.get("category") or "").lower():
                return best, ctx

    return best, random.choice(contexts)


# ── Stage 6: Prompt Assembly Engine + generation ─────────────────────────────
#
# Output shape matches the production /api/smart-lesson route's real
# ParsedSmartLesson contract (app/api/smart-lesson/route.ts in the frontend
# repo) — same field names and section structure (refresher / concept /
# explore / challenge / levelSet / sectionWatch), so this pilot's output is
# directly comparable to, and could eventually feed, the real feature. What's
# NOT replicated is that route's teacher-profile/class-interests/previous-
# topic-history/image-generation machinery — none of that infrastructure is
# part of this Phase A/B/C pilot, so "challenge.activity" is grounded in this
# module's own Pedagogy Library match instead of that route's activity bank +
# avoid-list rotation, and previousTopicRefresher only appears when the CLI
# caller passes one in explicitly (no DB history lookup here).

_BULLET_SHAPE = '{"text": "6-12 word headline", "detail": "1-2 sentences: exact words the teacher says (quoted), how long to wait, how students respond, what to watch for"}'

_GENERATION_PROMPT = """You are an expert teaching assistant creating a lesson prep material for a
teacher in an Indian classroom. This is a TEACHER'S prep sheet, not a student
handout: the visible "text" says WHAT happens; "detail" tells the teacher HOW
to run it — what to actually say, how long to wait, whether students answer
aloud/in pairs, and what to listen for. A first-time teacher should not have
to improvise anything.

TOPIC: {topic} — {subtopic}
GRADE: {grade} | SUBJECT: {subject}
{previous_topic_line}
CURRICULUM KNOWLEDGE (extracted from the textbook — Phase B):
- Concepts: {concepts}
- Vocabulary: {vocabulary}
- Learning outcomes: {learning_outcomes}
- Bloom level: {bloom_level} | Difficulty: {difficulty}

MATCHED ACTIVITY (from the Pedagogy Library — Phase C; this is "challenge.activity"
below, copied exactly by name — adapt the details freely, but do not swap in an
unrelated activity or invent a different one):
- Name: {activity_name} ({activity_category})
- Description: {activity_description}
- Suggested context: {context_name}
- Grouping: {grouping} | Classroom: {classroom_type} | Assessment method: {assessment_method}

TEACHER SETTINGS:
- Total lesson duration: {duration} minutes | Class size: {class_size} students
- Resource level: {resource_level} (0 = no materials, 1 = notebook/pencil, 2 = local materials)
- Language: {language} | Learning objective: {learning_objective} | Teaching style: {teaching_style}
{teacher_preferences_block}

Return ONLY valid JSON (no markdown fences), matching this exact shape:
{{
  "planningNote": "1-2 sentences of your own reasoning, written first: how the matched activity and context fit this topic, and what this bridges from — the refresher if one exists, or an acknowledgement that this is a starting point if it doesn't.",
  "objective": "ONE short verb-first headline, 4-8 words — not a full sentence. Good: 'Count and compare small groups of objects'. Bad: 'Students will be able to count objects.'",
  "previousTopicRefresher": {previous_topic_json_hint},
  "concept": [{bullet_shape}, "... EXACTLY 3 total — DISCOVERED, not lectured. Students find the idea in pairs BEFORE it's named; only the LAST bullet has the teacher confirm it aloud. Never open with 'Explain that...'."],
  "explore": {{
    "points": [{bullet_shape}, "... EXACTLY 3, in order: (1) CURIOSITY HOOK — a vivid scene using the suggested context, ending in one question students want to answer; (2) PAIR-FIRST — partners try a guess before any explanation; (3) the activity itself, carrying ONE real either/or choice the students decide."],
    "imageFocus": "one short phrase describing the single most useful thing to sketch on the board"
  }},
  "challenge": {{
    "activity": "{activity_name_literal}",
    "points": [{bullet_shape}, "... EXACTLY 3, as PLAY -> REFLECT -> ACT: (1) PLAY carries a SECOND real choice, framed as the students' decision; (2) REFLECT is a 30-second 'what worked?' turn-and-tell (never about mistakes); (3) ACT applies the idea to one real problem, with the worked answer stated in 'detail' so the teacher never computes live."]
  }},
  "sectionWatch": {{
    "refresher": {refresher_watch_hint},
    "concept": {bullet_shape},
    "explore": {bullet_shape},
    "challenge": {bullet_shape},
    "levelSet": {bullet_shape}
  }},
  "materialsUsed": ["every item actually referenced in explore/challenge — nothing invented, nothing unused"],
  "levelSet": {{
    "points": [{bullet_shape}, "... EXACTLY 3: (1) a self-check partners tick together WITHOUT the teacher ('Can you...?'); (2) a reflection on what was easiest/most interesting today (never on errors); (3) a final either/or choice that carries the idea home, framed as an invitation, not homework."]
  }},
  "timings": {{"refresher": {timing_refresher}, "concept": 5, "explore": 10, "challenge": 10, "levelSet": 5}}
}}

Rules:
- EXACTLY 3 bullets in concept, explore.points, challenge.points, and levelSet.points — no more, no fewer. "text" is a 6-12 word headline; "detail" carries the substance (1-2 sentences, never a paragraph): the exact words the teacher says (quoted where it's dialogue), how long to wait, how students respond (aloud/in pairs/written), and what to watch or listen for.
- STUDENT ACTION FIRST, every section: opens with something students DO (pair up, try, guess, build) — never with the teacher explaining. If a "text" starts with "Explain...", "Tell them...", or "Say that...", rewrite it to start with the student action.
- ANSWERS ARE FOR THE TEACHER, NOT A VERDICT: "detail" states the worked answer for the teacher's confidence, but the script spoken to students stays invitational — frame as what to listen/watch for, never "the answer is X" announced to the class.
- Set sectionWatch.refresher to null when previousTopicRefresher is null.
- "sectionWatch" is ONE headline + ONE detail per section — the single moment in that section the teacher most needs to watch or listen for, not a summary of the whole section.
- "challenge.activity" must be copied character-for-character from the Matched Activity above.
- Use one consistent currency (₹) if the lesson involves money — never mix currencies.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- "timings" should sum to roughly {duration} minutes (omit "refresher" if previousTopicRefresher is null).
"""


def build_prep_material_prompt(
    topic: str, subtopic: str, grade: str, subject: str, knowledge: dict,
    activity: Optional[dict], context: Optional[dict], teacher_settings: dict,
    previous_topic: Optional[str] = None, teacher_preferences: Optional[str] = None,
) -> str:
    """Pure — no I/O. Exposed separately from generate_prep_material() so a
    caller (the CLI) can print exactly what will be sent to the LLM before
    spending a call on it, which is the whole point of a prompt-refinement tool.

    previous_topic: pass the prior topic's name to test the refresher path;
    omit (the default) to match "no topic on record", same as the real route's
    null convention when there's no history.

    teacher_preferences: a rendered preferences paragraph (resources on hand,
    class size, comfort zones, activity leanings). Deliberately a STRING rather
    than the raw teaching_profile dict, because a shared prep material is
    generated once per grade+subject and consumed by every teacher of it — so
    what belongs here is a profile reconciled across that cohort, not one
    teacher's. Whose profile that is, and how conflicting ones merge, is the
    caller's decision; this module only renders what it's handed.
    """
    activity_name = (activity or {}).get("name") or "(none matched — invent one true to the topic)"
    has_previous = bool(previous_topic)

    return _GENERATION_PROMPT.format(
        topic=topic,
        subtopic=subtopic or "(none)",
        grade=grade,
        subject=subject,
        previous_topic_line=(f"PREVIOUS TOPIC (for the refresher): {previous_topic}\n" if has_previous
                              else "PREVIOUS TOPIC: none on record — set previousTopicRefresher to null.\n"),
        concepts=", ".join(knowledge.get("concepts") or []) or "(none extracted)",
        vocabulary=", ".join(knowledge.get("vocabulary") or []) or "(none extracted)",
        learning_outcomes=", ".join(knowledge.get("learning_outcomes") or []) or "(none extracted)",
        bloom_level=knowledge.get("bloom_level") or "(unspecified)",
        difficulty=knowledge.get("difficulty") or "(unspecified)",
        activity_name=activity_name,
        activity_name_literal=activity_name,
        activity_category=(activity or {}).get("category") or "",
        activity_description=(activity or {}).get("description") or "",
        context_name=(context or {}).get("name") or "(none — invent a reasonable Indian context)",
        grouping=(activity or {}).get("grouping") or "individual",
        classroom_type=(activity or {}).get("classroomType") or "both",
        assessment_method=(activity or {}).get("assessmentMethod") or "observation",
        duration=teacher_settings.get("duration", 30),
        class_size=teacher_settings.get("classSize", 40),
        resource_level=teacher_settings.get("resourceLevel", 0),
        language=teacher_settings.get("language", "English"),
        learning_objective=teacher_settings.get("learningObjective", "new lesson"),
        teaching_style=teacher_settings.get("teachingStyle", "interactive"),
        teacher_preferences_block=(
            f"\nCOHORT TEACHING PROFILE (reconciled across every teacher of this "
            f"grade+subject — treat resource and comfort limits as HARD constraints, "
            f"activity leanings as preferences):\n{teacher_preferences}\n"
            if teacher_preferences else ""
        ),
        bullet_shape=_BULLET_SHAPE,
        previous_topic_json_hint=(
            f'{{"previousTopic": "{previous_topic}", "recap": [{_BULLET_SHAPE}, "... up to 3 — a pair-start '
            f'student action that re-uses {previous_topic}\'s skill and bridges into today"]}}'
            if has_previous else "null"
        ),
        refresher_watch_hint=(_BULLET_SHAPE + " OR null if there is no refresher" if has_previous else "null"),
        timing_refresher=(3 if has_previous else "null (omit if no refresher)"),
    )


async def generate_prep_material(
    topic: str, subtopic: str, grade: str, subject: str, knowledge: dict,
    activity: Optional[dict], context: Optional[dict], teacher_settings: dict,
    previous_topic: Optional[str] = None, teacher_preferences: Optional[str] = None,
) -> dict:
    prompt = build_prep_material_prompt(
        topic, subtopic, grade, subject, knowledge, activity, context, teacher_settings,
        previous_topic, teacher_preferences,
    )
    raw = await call_ai(
        [{"role": "user", "content": prompt}],
        {"json_mode": True, "temperature": 0.5, "max_tokens": 4000},
    )
    return _parse_json_response(raw, "Stage 6 generation")


# ── Markdown rendering ────────────────────────────────────────────────────────

def _as_section_dict(value) -> dict:
    """Normalize a {"points": [...]} section — the model occasionally drops
    the wrapper and returns the bullet list bare (`"levelSet": [...]` instead
    of `"levelSet": {"points": [...]}`), which is a real, observed deviation
    from the requested shape, not hypothetical. Treat a bare list as if it
    were `{"points": <list>}` so rendering degrades gracefully (losing only
    imageFocus/activity, which a bare list never carried anyway) instead of
    crashing on a JSON response that was otherwise perfectly usable."""
    if isinstance(value, dict):
        return value
    if isinstance(value, list):
        return {"points": value}
    return {}


def _render_bullets(bullets: Optional[list]) -> str:
    lines = []
    for i, b in enumerate(bullets or [], start=1):
        if not isinstance(b, dict):
            continue
        text = (b.get("text") or "").strip()
        detail = (b.get("detail") or "").strip()
        lines.append(f"{i}. **{text}** — {detail}" if detail else f"{i}. **{text}**")
    return "\n".join(lines) if lines else "_(none)_"


def render_prep_material_markdown(material: dict, topic: str = "", subtopic: str = "") -> str:
    """Render a generate_prep_material() result as a readable Markdown prep
    sheet — the same section structure as the JSON (refresher / concept /
    explore / challenge / sectionWatch / materials / levelSet / timings),
    just formatted for a human to read rather than a program to parse."""
    out = []

    title = material.get("objective") or topic or "Prep Material"
    out.append(f"# {title}")
    if topic or subtopic:
        out.append(f"_{topic}{' — ' + subtopic if subtopic else ''}_")
    if material.get("planningNote"):
        out.append("")
        out.append(f"> {material['planningNote']}")

    refresher_raw = material.get("previousTopicRefresher")
    if refresher_raw:
        refresher = {"recap": refresher_raw} if isinstance(refresher_raw, list) else refresher_raw
        out.append("")
        out.append(f"## Refresher — {refresher.get('previousTopic', '')}")
        out.append(_render_bullets(refresher.get("recap")))

    out.append("")
    out.append("## Concept")
    out.append(_render_bullets(material.get("concept")))

    explore = _as_section_dict(material.get("explore"))
    out.append("")
    out.append("## Explore")
    if explore.get("imageFocus"):
        out.append(f"*Board sketch: {explore['imageFocus']}*")
        out.append("")
    out.append(_render_bullets(explore.get("points")))

    challenge = _as_section_dict(material.get("challenge"))
    out.append("")
    out.append(f"## Challenge — {challenge.get('activity', '(unspecified)')}")
    out.append(_render_bullets(challenge.get("points")))

    level_set = _as_section_dict(material.get("levelSet"))
    out.append("")
    out.append("## Level Set")
    out.append(_render_bullets(level_set.get("points")))

    watch = material.get("sectionWatch") or {}
    watch_lines = []
    for section in ("refresher", "concept", "explore", "challenge", "levelSet"):
        w = watch.get(section)
        if w:
            label = "Level Set" if section == "levelSet" else section.capitalize()
            text = (w.get("text") or "").strip()
            detail = (w.get("detail") or "").strip()
            watch_lines.append(f"- **{label}:** {text}" + (f" — {detail}" if detail else ""))
    if watch_lines:
        out.append("")
        out.append("## Watch For")
        out.extend(watch_lines)

    materials = material.get("materialsUsed") or []
    out.append("")
    out.append("## Materials")
    out.append("\n".join(f"- {m}" for m in materials) if materials else "_(none)_")

    timings = material.get("timings") or {}
    if timings:
        section_labels = {"levelSet": "Level Set"}
        parts = [
            f"{section_labels.get(k, k.capitalize())}: {v} min"
            for k, v in timings.items() if v is not None
        ]
        total = sum(v for v in timings.values() if isinstance(v, (int, float)))
        out.append("")
        out.append("## Timings")
        out.append(", ".join(parts) + f" (Total: {total} min)")

    return "\n".join(out)
