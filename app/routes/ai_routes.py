import json
import re
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Request, Depends, HTTPException

from ..lib.ai import call_ai
from ..lib.schemas import YearPlanSchema, BriefingSchema, CatchupPlanSchema, ClassPulseSchema, FlashcardsSchema
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit
from ..lib.server_cache import with_cache, ck
from ..deps import require_user

router = APIRouter()


def _extract_json(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if fenced:
        return fenced.group(1)
    first, last = raw.find("["), raw.rfind("]")
    if first != -1 and last != -1:
        return raw[first : last + 1]
    return raw


def _extract_json_obj(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if fenced:
        return fenced.group(1)
    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last != -1:
        return raw[first : last + 1]
    return raw


# POST /api/year-plan
@router.post("/year-plan")
async def year_plan(body: YearPlanSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    user_id = user["id"]

    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("year-plan", ip, (time.time() - t0) * 1000, False, "rate_limited", user_id=user_id)
        raise HTTPException(status_code=429, detail="Too many requests. Try again later.")

    try:
        total_sessions = body.totalWeeks * body.sessionsPerWeek
        topic_list = "\n".join(
            f"{i + 1}. {t.topic}" + (f" — {t.description}" if t.description else "") for i, t in enumerate(body.topics)
        )

        prompt = f"""You are helping an Indian school teacher plan their academic year for {body.subject}, Grade {body.grade}.

Total teaching sessions available: {total_sessions} ({body.totalWeeks} weeks × {body.sessionsPerWeek} sessions per week)
Number of topics to cover: {len(body.topics)}

Topics:
{topic_list}

Assign a realistic number of sessions to each topic based on its complexity and importance.
- Simple/short topics: 8–12 sessions
- Medium topics: 12–18 sessions
- Complex/foundational topics: 18–25 sessions
- The total MUST add up to exactly {total_sessions}
- Every topic must get at least 5 sessions

Return ONLY a valid JSON array in this exact order (same order as the topics above):
[
  {{ "id": "{body.topics[0].id if body.topics else 'id'}", "estimatedSessions": 15, "rationale": "one short sentence why" }},
  ...
]"""

        raw = await call_ai([{"role": "user", "content": prompt}])

        try:
            ai_plan = json.loads(_extract_json(raw))
            if not isinstance(ai_plan, list):
                raise ValueError("not an array")
        except Exception:
            api_log("year-plan", ip, (time.time() - t0) * 1000, False, "error", user_id=user_id, error="JSON parse failed")
            raise HTTPException(status_code=500, detail="Failed to parse year plan")

        fallback = round(total_sessions / len(body.topics))
        plan = []
        for i, t in enumerate(body.topics):
            entry = ai_plan[i] if i < len(ai_plan) else {}
            estimated = entry.get("estimatedSessions") if isinstance(entry, dict) else None
            plan.append({
                "id": t.id,
                "estimatedSessions": estimated if isinstance(estimated, (int, float)) and estimated > 0 else fallback,
                "rationale": (entry.get("rationale") if isinstance(entry, dict) else "") or "",
            })

        api_log("year-plan", ip, (time.time() - t0) * 1000, False, "ok", user_id=user_id)
        return {"plan": plan}
    except HTTPException:
        raise
    except Exception as e:
        api_log("year-plan", ip, (time.time() - t0) * 1000, False, "error", user_id=user_id, error=str(e))
        raise HTTPException(status_code=500, detail="Failed to generate year plan")


def _build_class_point(c) -> dict:
    label = f"Grade {c.grade}" + (f" · {c.section}" if c.section else "")
    last = c.lastSession
    return {
        "label": label,
        "lastTopic": last.topic if last else None,
        "lastSubTopics": c.lastSubTopics or [],
        "absentCount": last.absentCount if last else None,
        "nextTopic": c.nextTopic,
        "nextSubTopic": c.nextSubTopic,
        "atRiskCount": c.atRiskCount,
        "hasSession": bool(last),
    }


# POST /api/briefing
@router.post("/briefing")
async def briefing(body: BriefingSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("briefing", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        class_data = body.classData
        points = [_build_class_point(c) for c in class_data]
        name = (body.teacherName or "Teacher").split(" ")[0]
        greeting = f"Good morning, {name}! Here's your day at a glance."

        has_at_risk = any((c.atRiskStudents or []) for c in class_data)
        if not has_at_risk:
            api_log("briefing", ip, (time.time() - t0) * 1000, False, "ok")
            return {"greeting": greeting, "points": points, "priorities": []}

        today = datetime.now(timezone.utc).date().isoformat()
        at_risk_fp = "|".join(f"{c.grade}{c.section}:{c.atRiskCount}" for c in class_data)
        cache_key = ck("briefing", body.teacherName, today, at_risk_fp)

        async def _compute():
            class_lines_parts = []
            for c in class_data:
                label = f"Grade {c.grade}" + (f" {c.section}" if c.section else "") + f" ({c.studentCount} students)"
                next_line = f"Next topic: {c.nextTopic}" if c.nextTopic else "No upcoming topic set"
                pacing_line = (
                    f"Syllabus: {c.completedTopics}/{c.totalTopics} topics done"
                    if c.completedTopics is not None and c.totalTopics is not None and c.totalTopics > 0
                    else ""
                )
                last = c.lastSession
                last_line = (
                    f"Last class: {last.topic} ({last.date}), {last.absentCount} absent"
                    if last else "No previous sessions"
                )
                student_lines = "\n".join(
                    f"  - {s.name} {'[CHRONIC ABSENTEE]' if s.absenteeType == 'chronic' else '[rare absentee]'}: {s.warning}"
                    + (f" (topic: {s.topic})" if s.topic else "")
                    for s in (c.atRiskStudents or [])
                )
                block = [label, next_line, pacing_line, last_line]
                if student_lines:
                    block.append(f"At-risk students:\n{student_lines}")
                class_lines_parts.append("\n".join(p for p in block if p))
            class_lines = "\n\n".join(class_lines_parts)

            prompt = (
                "You are helping a teacher in an Indian government school plan their day.\n\n"
                f"Teacher: {body.teacherName}\n\n"
                f"CLASS OVERVIEW:\n{class_lines}\n\n"
                "Write 3–5 specific, named action items for today. Rules:\n"
                "- Name a specific student and exactly what to do — no vague advice\n"
                "- One sentence per item, maximum\n"
                "- Chronic absentees: suggest catchup plan or one-on-one check\n"
                "- Rare absentees: suggest quick topic recap today\n"
                "- If a topic was missed by multiple at-risk students, prioritise re-explaining it first\n"
                "- Mark urgent=true only for chronic absentees or students failing the same topic\n\n"
                "Return JSON only:\n"
                '{ "priorities": [{ "urgent": true, "message": "Raju has missed 7 sessions overall — generate a catchup plan for Fractions before today\'s class." }] }'
            )

            raw = await call_ai([{"role": "user", "content": prompt}], {"temperature": 0.4})
            if not raw:
                return []
            try:
                parsed = json.loads(_extract_json_obj(raw))
                priorities = parsed.get("priorities") if isinstance(parsed, dict) else None
                if not isinstance(priorities, list):
                    return []
                return [
                    {"urgent": bool(p.get("urgent")) if isinstance(p, dict) else False,
                     "message": str(p.get("message", "")) if isinstance(p, dict) else ""}
                    for p in priorities[:6]
                ]
            except Exception:
                return []

        priorities, _from_cache = await with_cache(cache_key, 86400, _compute)
        api_log("briefing", ip, (time.time() - t0) * 1000, False, "ok")
        return {"greeting": greeting, "points": points, "priorities": priorities or []}
    except Exception as e:
        api_log("briefing", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {"greeting": "", "points": [], "priorities": []}


# POST /api/catchup-plan  (public — rate-limited only, no Supabase session; used by students too)
@router.post("/catchup-plan")
async def catchup_plan(body: CatchupPlanSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("catchup-plan", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    interests = [i for i in (body.studentInterests or []) if i]
    top_interest_line = (
        f'You MUST use "{interests[0]}" as the central analogy or example. Do not use a generic example instead.'
        if interests
        else "Use a relatable Indian everyday example: cricket scoring, market shopping, cooking measurements, or farming."
    )

    style_rule = (
        "This student learns through stories and narratives — frame everything as a mini-story or journey."
        if body.learningStyle == "story-based"
        else "This student is analytical — use clear steps, patterns, and logical sequences rather than stories."
        if body.learningStyle == "analytical"
        else "Use simple, conversational language."
    )

    goal_line = (
        f'Student\'s personal goal: "{body.studentGoal}" — connect the topic to this goal in one sentence if natural.'
        if body.studentGoal else ""
    )

    score = body.score
    if score is None:
        score_line = "No test score yet — treat cautiously and check understanding as you go."
    elif score < 40:
        score_line = f"Test score: {score}% — VERY LOW. The student missed the lesson and is failing the test. Start from absolute basics."
    elif score < 70:
        score_line = f"Test score: {score}% — below passing. They missed the lesson and are struggling. Fill the gap carefully."
    else:
        score_line = f"Test score: {score}% — decent. They missed the lesson but are managing. One focused session should be enough."

    attendance_pct = round(body.overallAttendanceRate * 100) if body.overallAttendanceRate is not None else None
    missed_note = (
        f", missed {body.topicSessionsMissed} of {body.topicSessionsTotal} sessions on this topic"
        if body.topicSessionsMissed is not None else ""
    )
    if body.absenteeType == "chronic":
        absentee_rule = (
            f"CHRONIC ABSENTEE ({attendance_pct if attendance_pct is not None else '?'}% attendance{missed_note}).\n"
            "→ Do NOT assume classroom continuity. This student likely missed prerequisite sessions too.\n"
            "→ Start the explanation with a simple check question to see what they already know.\n"
            "→ Build from foundational concepts up to the topic — this is a re-entry plan, not a quick recap."
        )
    else:
        absentee_rule = (
            f"RARE ABSENTEE ({attendance_pct if attendance_pct is not None else '?'}% attendance{missed_note}).\n"
            "→ Assume the student knows class basics and what came before this topic.\n"
            "→ Focus only on what was covered in the missed session. No need to re-teach prerequisites.\n"
            "→ One focused 10-minute session is enough to bring them up to speed."
        )

    snapshot = body.lessonSnapshot
    hook_rule = ""
    if snapshot and snapshot.hook:
        examples = "; ".join(snapshot.realLifeExamples)
        hook_rule = (
            f'The class started with this hook: "{snapshot.hook}"\n'
            f"Real-life examples the class saw: {examples}\n"
            "→ OPEN your explanation with the exact same hook. Weave in the same examples so this student feels connected to what their classmates experienced."
        )

    if body.absenteeType == "chronic":
        explanation_hint = (
            "5-6 sentences. Open with a friendly check: 'Do you remember what [prerequisite] means? Let me remind you...' "
            f"Then build step by step to {body.topic}. End by confirming the student understands with one rhetorical question."
        )
    else:
        opener = (
            f'Open with: "{snapshot.hook[:60]}..." — the same hook the class heard.'
            if snapshot and snapshot.hook
            else f"Open with the {interests[0] if interests else 'everyday Indian'} analogy."
        )
        explanation_hint = f"4-5 sentences. {opener} Explain {body.topic} clearly and end with one sentence connecting it to what they already know."

    prompt = f"""You are writing a personalised catch-up plan for a student in an Indian government school. A teacher will use this in a 10-minute one-on-one session — reading it aloud or giving it as a handwritten note. The student has no phone or internet.

━━ STUDENT ━━
Name: {body.studentName} | Grade: {body.grade} | Subject: {body.subject}
Topic missed: {body.topic}
{score_line}
{absentee_rule}
{hook_rule}
{goal_line}

━━ HOW TO EXPLAIN ━━
{top_interest_line}
{style_rule}
Write the explanation as if you are SPEAKING DIRECTLY to {body.studentName} — warm, simple, conversational. A Grade {body.grade} student must be able to follow every sentence.

━━ OUTPUT ━━
Return ONLY valid JSON with exactly these 4 fields:

{{
  "explanation": "{explanation_hint}",

  "practiceQuestions": [
    "Warm-up: [A very simple question — even a nervous student should get this right. Tests that they understood the basic concept.]",
    "Basic: [A straightforward question directly on {body.topic}. One step to answer.]",
    "Medium: [Requires applying {body.topic} in a small problem. Two steps.]",
    "Challenge: [A slightly harder question that makes them think. Connects {body.topic} to a real situation{f' involving {interests[0]}' if interests else ''}.]"
  ],

  "activity": "Step 1: [What teacher says/does first — 2 min]. Step 2: [Student does something with chalk or notebook — 3 min]. Step 3: [Teacher checks and corrects — 3 min]. Step 4: [Quick confidence check — 2 min]. (Uses only chalk, fingers, or notebook. No materials needed.)",

  "focusNote": "Start by asking {body.studentName}: [one specific question that reveals if they understood]. If they struggle, [exactly what to do or say]. The key idea to lock in today: [one sentence on the core concept]."
}}"""

    t0 = time.time()
    try:
        score_bucket = "none" if score is None else "low" if score < 50 else "medium" if score < 75 else "high"
        snapshot_key = re.sub(r"\s+", "_", snapshot.hook[:30]) if snapshot and snapshot.hook else "none"
        top_interest = re.sub(r"\s+", "_", interests[0][:15]) if interests else "none"

        async def _compute():
            text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 1400})
            cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I), flags=re.I).strip()
            match = re.search(r"\{[\s\S]*\}", cleaned)
            return json.loads(match.group(0) if match else cleaned)

        parsed, from_cache = await with_cache(
            ck("catchup-v2", body.topic.lower().strip(), body.subject.lower().strip(), body.grade,
               score_bucket, snapshot_key, body.absenteeType or "rare", top_interest),
            604800,
            _compute,
        )
        api_log("catchup-plan", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return {
            "explanation": parsed.get("explanation") if isinstance(parsed.get("explanation"), str) else "",
            "practiceQuestions": parsed.get("practiceQuestions") if isinstance(parsed.get("practiceQuestions"), list) else [],
            "activity": parsed.get("activity") if isinstance(parsed.get("activity"), str) else "",
            "focusNote": parsed.get("focusNote") if isinstance(parsed.get("focusNote"), str) else "",
        }
    except Exception as e:
        print(f"[catchup-plan] generation failed: {e}")
        api_log("catchup-plan", ip, (time.time() - t0) * 1000, False, "error")
        raise HTTPException(status_code=500, detail="Failed to generate plan")


# POST /api/class-pulse
@router.post("/class-pulse")
async def class_pulse(body: ClassPulseSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("class-pulse", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        student_lines = "\n".join(
            f"  - {s.name}: mastery {round(s.avgMastery * 100)}%, attendance {round(s.attendanceRate * 100)}%"
            for s in body.students
        )
        test_lines = (
            "\n".join(f"  - {t.topic}: class avg {round((t.avgScore / t.totalMarks) * 100)}%" for t in body.tests)
            if body.tests else "  No tests yet."
        )
        coverage_lines = (
            "\n".join(f"  - {c.topic}: {c.status}" for c in body.topicCoverage)
            if body.topicCoverage else "  No topics taught yet."
        )

        prompt = f"""You are an AI assistant helping an Indian government school teacher understand their class performance.

Class: {body.className}
Subject: {body.subject}, Grade: {body.grade}
Overall attendance rate: {round(body.attendanceRate * 100)}%
Number of students: {len(body.students)}

Student performance overview:
{student_lines}

Test results:
{test_lines}

Topic coverage:
{coverage_lines}

Write a concise CLASS PULSE REPORT with these FOUR sections:
1. Class Health (1-2 sentences — overall picture, strengths of the class)
2. Concern Areas (which topics or which students need the most attention and why)
3. Wins to Celebrate (something positive — even small — to acknowledge)
4. This Week's Focus (one specific, actionable priority for the teacher this week)

Be warm, specific, and encouraging. No jargon. Suitable for an Indian school context.

Return ONLY valid JSON:
{{
  "health": "...",
  "concerns": "...",
  "wins": "...",
  "focus": "..."
}}"""

        attendance_bucket = "low" if body.attendanceRate < 0.6 else "mid" if body.attendanceRate < 0.85 else "high"
        test_key = "~".join(sorted(t.topic for t in body.tests))

        async def _compute():
            raw = await call_ai([{"role": "user", "content": prompt}])
            return json.loads(_extract_json_obj(raw))

        parsed, from_cache = await with_cache(
            ck("class-pulse", body.className, body.subject, body.grade, attendance_bucket, test_key, len(body.students)),
            86400,
            _compute,
        )
        api_log("class-pulse", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return parsed
    except Exception as e:
        api_log("class-pulse", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {
            "health": "Could not generate pulse at this time.",
            "concerns": "",
            "wins": "",
            "focus": "",
        }


# POST /api/flashcards  (public — rate-limited only, no Supabase session; used by students too)
@router.post("/flashcards")
async def flashcards(body: FlashcardsSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    interests = body.interests or []
    interest_hint = (
        f"Where natural, relate examples to: {', '.join(interests[:2])}."
        if interests
        else "Use simple Indian everyday examples (cricket, market, cooking, farming) where helpful."
    )

    prompt = f"""You are creating a set of 8 revision flashcards for a Grade {body.grade} student in an Indian government school studying {body.subject}.
Topic: {body.topic}
{interest_hint}

Rules:
- Each flashcard has a FRONT (a short prompt: a term, question, or "What is…?") and a BACK (a clear, correct answer in 1–2 simple sentences).
- Match Grade {body.grade} level — simple language, no jargon.
- Cover the key ideas of the topic: definitions, one worked example, and one "why it matters".
- Keep the front under 12 words. Keep the back under 40 words.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "cards": [
    {{ "front": "front text here", "back": "back text here" }}
  ]
}}"""

    try:
        top_interest = re.sub(r"\s+", "_", interests[0][:20].lower()) if interests else "none"

        async def _compute():
            text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 900})
            cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I), flags=re.I).strip()
            match = re.search(r"\{[\s\S]*\}", cleaned)
            return json.loads(match.group(0) if match else cleaned)

        value, _from_cache = await with_cache(
            ck("flashcards-v1", body.topic.lower().strip(), body.subject.lower().strip(), body.grade, top_interest),
            86400,
            _compute,
        )
        raw_cards = value.get("cards") if isinstance(value, dict) else None
        cards = [
            c for c in (raw_cards or [])
            if isinstance(c, dict) and isinstance(c.get("front"), str) and isinstance(c.get("back"), str)
        ]
        if not cards:
            raise ValueError("No valid flashcards returned by AI")
        return {"cards": cards}
    except Exception as e:
        print(f"[flashcards] generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate flashcards")
