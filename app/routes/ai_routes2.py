import json
import re
import time
from typing import Optional
from fastapi import APIRouter, Request, Depends, HTTPException

from postgrest.exceptions import APIError

from ..lib.ai import call_ai, generate_illustration
from ..lib.schemas import (
    RecoverySchema, LessonPlanSchema, LessonPrepSchema, QuestionsSchema,
    PracticeQuizSchema, TestAnalysisSchema, TestStudyGuideSchema, StudentReportSchema,
    PotentialSchema, TestPrepSchema, PeerPairSchema, SaveScoreSchema,
    SmartLessonSchema, GenerateAnswerKeySchema, GenerateWorksheetSchema, WorksheetUpsertSchema,
)
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit, check_vision_rate_limit
from ..lib.server_cache import with_cache, ck
from ..lib.supabase_clients import create_admin_client
from ..lib.student_auth import verify_student_cookie
from ..lib.personality_traits import today_date_str, trait_for_date
from ..deps import require_user, require_student_token

router = APIRouter()


def _extract_json_obj(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if fenced:
        return fenced.group(1)
    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last != -1:
        return raw[first : last + 1]
    return raw


def _strip_and_match_obj(text: str) -> str:
    cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", text.strip(), flags=re.I), flags=re.I).strip()
    match = re.search(r"\{[\s\S]*\}", cleaned)
    return match.group(0) if match else cleaned


def _whole_if_int(x):
    """Postgrest sends whatever json.dumps produces; a Python whole float like
    7.0 renders as "7.0", which Postgres's integer columns reject (unlike JS,
    where JSON.stringify(7) is "7"). Normalize before writes to integer columns."""
    if isinstance(x, float) and x == int(x):
        return int(x)
    return x


# POST /api/recovery
@router.post("/recovery")
async def recovery(body: RecoverySchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("recovery", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        approaches = body.previousApproaches or []
        helped = [a for a in approaches if a.helped is True]
        not_helped = [a for a in approaches if a.helped is False]
        partial = [a for a in approaches if a.helped is None]

        if not approaches:
            prev_list = "\nNo previous approaches tried yet."
        else:
            lines = []
            for i, a in enumerate(approaches):
                outcome = "✓ helped" if a.helped is True else "✗ did not help" if a.helped is False else "~ partially helped"
                lines.append(f"{i + 1}. [{outcome}] {a.approachUsed}")
            guidance = ["\nPrevious approaches and outcomes:\n" + "\n".join(lines)]
            if not_helped:
                guidance.append(f"⚠ These {len(not_helped)} approach(es) did NOT help — avoid similar styles.")
            if partial:
                guidance.append(f"These {len(partial)} approach(es) partially helped — try a different angle that builds on what worked.")
            if helped:
                guidance.append(f"{len(helped)} approach(es) previously helped — the student CAN learn this; try an equally strong but genuinely different angle.")
            prev_list = "\n".join(guidance)

        prompt = f"""A Grade {body.grade} student named {body.studentName} in a rural Indian school has tried to understand "{body.topic}" {body.attempts} time(s) without full success.
{prev_list}

Generate ONE completely new explanation approach that:
1. Uses a real-life Indian example — from cricket, food, farming, festivals, or daily village life
2. Can be explained verbally in class — no materials or equipment needed
3. Is genuinely different from all previous approaches listed above
4. If any previous approach partially or fully helped, note what worked and take it further from a fresh angle
5. Ends with one short question the teacher can ask to immediately check if the student understood

Return valid JSON only:
{{
  "explanation": "The new explanation approach (2-4 sentences)",
  "example": "The specific real-life Indian example to use (1-2 sentences)",
  "checkQuestion": "One short question to ask the student to check understanding"
}}"""

        prev_key = "~~".join(sorted(f"{a.approachUsed}:{a.helped if a.helped is not None else 'null'}" for a in approaches))

        async def _compute():
            result = await call_ai([{"role": "user", "content": prompt}])
            return json.loads(result)

        parsed, from_cache = await with_cache(
            ck("recovery", body.topic.lower().strip(), body.grade, body.attempts, prev_key), 604800, _compute
        )
        api_log("recovery", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return parsed
    except Exception as e:
        api_log("recovery", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {
            "explanation": "Try explaining the concept using a story from everyday life.",
            "example": "Use something the student sees every day — like dividing a roti equally.",
            "checkQuestion": "Can you show me on your fingers how you would divide this?",
        }


# POST /api/lesson-plan
@router.post("/lesson-plan")
async def lesson_plan(body: LessonPlanSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    try:
        pending = [t for t in body.topics if not t.isCompleted]
        done = sum(1 for t in body.topics if t.isCompleted)

        topic_list = "\n".join(
            f"{i + 1}. {t.topic}" + (f" ({t.description})" if t.description else "")
            + (f" [Week {t.weekNumber}]" if t.weekNumber else "")
            for i, t in enumerate(pending[:10])
        )

        interest_line = (
            f"Class interests: {', '.join(body.studentInterests[:5])}"
            if body.studentInterests else "Student interests not recorded yet"
        )

        prompt = f"""You are helping a teacher at an Indian government school plan their lessons.

Class: {body.className}
Subject: {body.subject}
{interest_line}
Topics completed so far: {done}

Remaining syllabus topics (up to 10):
{topic_list or 'No pending topics'}

Create a practical week-by-week lesson plan for the NEXT 4 weeks covering the pending topics above.
For each week:
- Assign 1-2 topics
- Write one short teaching tip (max 20 words) connecting the topic to the students' interests
- Suggest one quick activity or example (max 15 words)

Be warm, practical, and specific. No jargon.

Return ONLY valid JSON:
{{
  "weeks": [
    {{
      "week": 1,
      "topics": ["topic name"],
      "tip": "short teaching tip using student interests",
      "activity": "quick activity or example"
    }}
  ]
}}"""

        raw = await call_ai([{"role": "user", "content": prompt}])
        try:
            result = json.loads(raw)
        except Exception:
            api_log("lesson-plan", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="JSON parse failed")
            return {"weeks": []}

        api_log("lesson-plan", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return result
    except Exception as e:
        api_log("lesson-plan", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        return {"weeks": []}


# POST /api/lesson-prep
@router.post("/lesson-prep")
async def lesson_prep(body: LessonPrepSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("lesson-prep", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    grade = body.grade or ""
    language = body.language or "english"
    subtopic = body.subtopic

    lang_note = (
        f"The teacher prefers {language}. Use simple English but include key terms in {language} where natural."
        if language != "english" else ""
    )
    focus = (
        f'The specific subtopic for today is "{subtopic.strip()}" within "{body.topic}". Focus all examples, mistakes, and activity on this subtopic.'
        if subtopic and subtopic.strip() else ""
    )
    display_topic = f"{body.topic} → {subtopic.strip()}" if subtopic and subtopic.strip() else body.topic

    prompt = f"""You are a mentor helping an Indian {body.subject} teacher prepare to teach "{display_topic}" to Grade {grade or 'school'} students.
{lang_note}
{focus}

Give a quick lesson prep guide. Use familiar Indian contexts (cricket, chai, markets, festivals, auto-rickshaw, mobile data, Bollywood) for examples.

Respond ONLY as valid JSON:
{{
  "explanation": "A clear 2-sentence explanation of {display_topic} in simple language a student can understand",
  "examples": ["Indian real-life example 1", "Indian real-life example 2", "Indian real-life example 3"],
  "commonMistakes": ["Common mistake students make 1", "Common mistake students make 2"],
  "quickActivity": "One specific 2-minute activity the teacher can do right now to check if students understood"
}}"""

    t0 = time.time()
    try:
        async def _compute():
            content = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 700})
            return json.loads(content)

        parsed, from_cache = await with_cache(
            ck("lesson-prep", body.topic.lower().strip(), body.subject.lower().strip(), grade, language,
               subtopic.lower().strip() if subtopic else ""),
            2592000,
            _compute,
        )
        api_log("lesson-prep", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return parsed
    except Exception:
        api_log("lesson-prep", ip, (time.time() - t0) * 1000, False, "error")
        raise HTTPException(status_code=500, detail="Failed to generate prep")


_QUESTION_PATTERNS = {
    10: [{"marks": 2, "count": 3, "difficulty": "easy", "type": "short-answer", "label": "Short Answer"},
         {"marks": 4, "count": 1, "difficulty": "hard", "type": "long-answer", "label": "Long Answer"}],
    20: [{"marks": 2, "count": 4, "difficulty": "easy", "type": "short-answer", "label": "Short Answer"},
         {"marks": 6, "count": 2, "difficulty": "hard", "type": "long-answer", "label": "Long Answer"}],
    25: [{"marks": 3, "count": 5, "difficulty": "medium", "type": "short-answer", "label": "Short Answer"},
         {"marks": 5, "count": 2, "difficulty": "hard", "type": "long-answer", "label": "Long Answer"}],
    50: [{"marks": 4, "count": 5, "difficulty": "medium", "type": "short-answer", "label": "Short Answer"},
         {"marks": 6, "count": 5, "difficulty": "hard", "type": "long-answer", "label": "Long Answer"}],
    100: [{"marks": 4, "count": 10, "difficulty": "medium", "type": "short-answer", "label": "Short Answer"},
          {"marks": 12, "count": 5, "difficulty": "hard", "type": "long-answer", "label": "Long Answer"}],
}


def _get_pattern(total_marks: float) -> list[dict]:
    supported = [10, 20, 25, 50, 100]
    closest = min(supported, key=lambda b: abs(b - total_marks))
    return _QUESTION_PATTERNS[closest]


# POST /api/questions
@router.post("/questions")
async def questions(body: QuestionsSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("questions", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        try:
            marks = int(str(body.totalMarks))
        except Exception:
            marks = 10
        if not marks:
            marks = 10
        pattern = _get_pattern(marks)
        total = sum(p["marks"] * p["count"] for p in pattern)

        section_lines = "\n".join(
            f"  Section {chr(65 + i)} — {s['label']}: {s['count']} question{'s' if s['count'] > 1 else ''} × "
            f"{s['marks']} mark{'s' if s['marks'] > 1 else ''} each ({s['type']}, difficulty: {s['difficulty']})"
            for i, s in enumerate(pattern)
        )

        prompt = f"""Generate a {total}-mark subjective exam paper for Grade {body.grade} {body.subject} on the topic: "{body.topic}".

Paper structure — follow EXACTLY (correct count and marks per section):
{section_lines}
Total: {total} marks

Rules:
- short-answer: question requires a 2-4 sentence written response. "answer" = model answer (2-4 sentences). "keywords" = 3-5 key terms a teacher would look for when grading.
- long-answer: question requires a detailed paragraph response. "answer" = full model answer paragraph (5-8 sentences).
- Simple language for Grade {body.grade} students in Indian schools
- Use Indian contexts (farming, cricket, food, festivals, Indian cities, rupees)
- Self-contained questions only — no "refer to diagram" or "as discussed"
- Follow section counts exactly — no MCQ, no fill-in-the-blank

Return valid JSON only — no markdown, no extra text:
{{
  "questions": [
    {{ "text": "Why is the Sun important for life on Earth?", "type": "short-answer", "difficulty": "easy", "marks": 2, "options": [], "answer": "The Sun provides light and heat needed for plants to grow and for humans to stay warm. Without the Sun, life on Earth would not be possible.", "keywords": ["light", "heat", "energy", "plants"] }},
    {{ "text": "Describe the water cycle and explain why it is important for living things.", "type": "long-answer", "difficulty": "hard", "marks": 4, "options": [], "answer": "The water cycle is the continuous movement of water...", "keywords": [] }}
  ]
}}"""

        async def _compute():
            result = await call_ai([{"role": "user", "content": prompt}])
            return json.loads(result)

        parsed, from_cache = await with_cache(ck("questions", "v4-subj", body.topic.lower().strip(), body.grade, total), 2592000, _compute)
        api_log("questions", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return parsed
    except Exception as e:
        api_log("questions", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="questions generation failed")


def _is_valid_question(q) -> bool:
    return isinstance(q, dict) and isinstance(q.get("text"), str) and isinstance(q.get("options"), list) and isinstance(q.get("answerIndex"), (int, float))


# POST /api/practice-quiz (public — rate-limited only)
@router.post("/practice-quiz")
async def practice_quiz(body: PracticeQuizSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    interests = body.interests or []
    interest_hint = f"Use examples from: {', '.join(interests[:2])}." if interests else "Use simple Indian everyday examples (cricket, market, cooking, farming)."

    prompt = f"""You are creating a 4-question multiple-choice practice quiz for a Grade {body.grade} student in an Indian government school studying {body.subject}.
Topic: {body.topic}
{interest_hint}

Rules:
- Questions must match Grade {body.grade} level — simple language, no jargon.
- Each question has exactly 4 options (A, B, C, D). Only ONE is correct.
- Vary difficulty: Q1 easy, Q2 easy-medium, Q3 medium, Q4 slightly harder.
- The explanation must be 1 sentence — explain WHY the answer is correct in simple terms.
- Do NOT use "All of the above" or "None of the above" options.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "questions": [
    {{
      "text": "question text here",
      "options": ["option A text", "option B text", "option C text", "option D text"],
      "answerIndex": 0,
      "explanation": "one sentence explanation"
    }}
  ]
}}"""

    try:
        top_interest = re.sub(r"\s+", "_", interests[0][:20].lower()) if interests else "none"

        async def _compute():
            text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 800})
            return json.loads(_strip_and_match_obj(text))

        value, _from_cache = await with_cache(
            ck("practice-quiz-v1", body.topic.lower().strip(), body.subject.lower().strip(), body.grade, top_interest),
            86400,
            _compute,
        )
        questions_ = [q for q in (value.get("questions") or []) if _is_valid_question(q)] if isinstance(value, dict) else []
        if not questions_:
            raise ValueError("No valid questions returned by AI")
        return {"questions": questions_}
    except Exception as e:
        print(f"[practice-quiz] generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate quiz")


# POST /api/adaptive-quiz (public — rate-limited only)
@router.post("/adaptive-quiz")
async def adaptive_quiz(body: PracticeQuizSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    interests = body.interests or []
    interest_hint = f"Use examples from: {', '.join(interests[:2])}." if interests else "Use simple Indian everyday examples (cricket, market, cooking, farming)."

    prompt = f"""You are creating an adaptive practice-quiz question bank for a Grade {body.grade} student in an Indian government school studying {body.subject}.
Topic: {body.topic}
{interest_hint}

Write THREE difficulty tiers of multiple-choice questions on this exact topic:
- "easy": 3 questions — very simple, one clear step to the answer.
- "medium": 4 questions — the standard difficulty for this grade level.
- "hard": 3 questions — a genuinely harder variation (an extra step, a trickier case), still fair for this grade.

Rules for every question:
- Exactly 4 options (A, B, C, D). Only ONE is correct.
- No "All of the above" / "None of the above".
- "explanation": 1 sentence, explains WHY the answer is correct in simple terms.
- Simple language throughout — no jargon beyond what's needed for the topic.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "easy":   [ {{ "text": "string", "options": ["a","b","c","d"], "answerIndex": 0, "explanation": "string" }} ],
  "medium": [ {{ "text": "string", "options": ["a","b","c","d"], "answerIndex": 0, "explanation": "string" }} ],
  "hard":   [ {{ "text": "string", "options": ["a","b","c","d"], "answerIndex": 0, "explanation": "string" }} ]
}}
("easy" must contain exactly 3, "medium" exactly 4, "hard" exactly 3)"""

    try:
        top_interest = re.sub(r"\s+", "_", interests[0][:20].lower()) if interests else "none"

        async def _compute():
            text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 1600})
            return json.loads(_strip_and_match_obj(text))

        value, _from_cache = await with_cache(
            ck("adaptive-quiz-v1", body.topic.lower().strip(), body.subject.lower().strip(), body.grade, top_interest),
            86400,
            _compute,
        )
        pool = {
            "easy": [q for q in (value.get("easy") or []) if _is_valid_question(q)] if isinstance(value, dict) else [],
            "medium": [q for q in (value.get("medium") or []) if _is_valid_question(q)] if isinstance(value, dict) else [],
            "hard": [q for q in (value.get("hard") or []) if _is_valid_question(q)] if isinstance(value, dict) else [],
        }
        if not pool["easy"] and not pool["medium"] and not pool["hard"]:
            raise ValueError("No valid questions returned by AI")
        return {"pool": pool}
    except Exception as e:
        print(f"[adaptive-quiz] generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate adaptive quiz")


# POST /api/test-analysis
@router.post("/test-analysis")
async def test_analysis(body: TestAnalysisSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    try:
        avg = sum(r.percentage for r in body.results) / len(body.results)
        sorted_results = sorted(body.results, key=lambda r: r.percentage, reverse=True)
        result_lines = "\n".join(f"  {r.name}: {r.score}/{body.totalMarks} ({round(r.percentage)}%)" for r in sorted_results)

        prompt = f"""You are an experienced Indian school teacher reviewing a class test.

Subject: {body.subject}, Grade: {body.grade}
Topic: {body.topic}
Total Marks: {body.totalMarks}
Class Average: {round(avg)}%

Student Results (best to lowest):
{result_lines}

Write a short analysis in 4 parts. Be specific — use student names. Be warm and practical.
1. Summary: How did the class do overall? (1-2 sentences)
2. Top Performers: Which 2-3 students did well and what did they demonstrate?
3. Needs Help: Which students scored below 50%? What should the teacher watch for?
4. Next Action: One concrete step the teacher should take next (re-teach a subtopic, pair weaker with stronger, give extra practice, etc.)

Return ONLY valid JSON:
{{
  "summary": "...",
  "topPerformers": "...",
  "needHelp": "...",
  "action": "..."
}}"""

        raw = await call_ai([{"role": "user", "content": prompt}])
        try:
            result = json.loads(_extract_json_obj(raw))
        except Exception:
            api_log("test-analysis", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="JSON parse failed")
            raise HTTPException(status_code=500, detail="Failed to parse analysis")

        api_log("test-analysis", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return result
    except HTTPException:
        raise
    except Exception as e:
        api_log("test-analysis", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        raise HTTPException(status_code=500, detail="Failed to analyse test")


# POST /api/test-study-guide (public — vision-tier rate limit only)
@router.post("/test-study-guide")
async def test_study_guide(body: TestStudyGuideSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    interests = body.interests or []
    interest_hint = f"Where natural, use examples from: {', '.join(interests[:2])}." if interests else "Use simple Indian everyday examples (cricket, market, cooking, farming) where helpful."
    total_marks_note = f" worth {body.totalMarks} marks" if body.totalMarks else ""

    prompt = f"""You are building a complete study guide for a Grade {body.grade} student in an Indian government school preparing for an upcoming {body.subject} test{total_marks_note}.
Test topic: {body.topic}
{interest_hint}

Break this topic into 3-5 focus areas that together comprehensively cover everything the student should revise for this test, ordered from foundational to advanced. For EACH focus area, write:
- "name": a short 2-5 word title for this focus area.
- "summary": 2-3 simple sentences explaining what this focus area covers and why it matters.
- "keyPoints": 3-6 short bullet facts/steps/rules to remember (each under 18 words).
- "examples": 2-3 short worked examples or real-life illustrations of this focus area.
- "commonMistakes": 2-3 short mistakes students often make with this focus area, and how to avoid them.
- "practiceQuestions": exactly 3 practice questions with their answers, ordered easy → medium → hard, matching what could appear on this test.
- "imageQuery": the single best Wikipedia article title for a helpful diagram or photo of this focus area (e.g. "Human heart", "Water cycle"). 1-3 words, a real encyclopedia topic.
- "diagramLabels": 3-6 short labels (1-2 words each) for the main visible parts/steps of this focus area that a labelled diagram should point to. Use plain English words a Grade {body.grade} student knows. If this focus area has no distinct visual parts to label (e.g. an abstract idea), return an empty array.

Rules:
- Match Grade {body.grade} level — simple language, no jargon.
- Every focus area must be genuinely distinct — do not repeat the same content across focus areas.
- Together, the focus areas should let a student revise this ENTIRE topic without missing anything important.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "topics": [
    {{
      "name": "...",
      "summary": "...",
      "keyPoints": ["...", "..."],
      "examples": ["...", "..."],
      "commonMistakes": ["...", "..."],
      "practiceQuestions": [{{ "question": "...", "answer": "..." }}, {{ "question": "...", "answer": "..." }}, {{ "question": "...", "answer": "..." }}],
      "imageQuery": "...",
      "diagramLabels": ["...", "..."]
    }}
  ]
}}"""

    try:
        top_interest = re.sub(r"\s+", "_", interests[0][:20].lower()) if interests else "none"

        async def _compute():
            text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 6000})
            parsed = json.loads(_strip_and_match_obj(text))

            raw_topics = parsed.get("topics") if isinstance(parsed.get("topics"), list) else []

            async def _with_image(t):
                if not isinstance(t, dict):
                    return t
                image_subject = t.get("imageQuery").strip() if isinstance(t.get("imageQuery"), str) and t.get("imageQuery").strip() else (t.get("name") if isinstance(t.get("name"), str) else body.topic)
                diagram_labels = [l for l in (t.get("diagramLabels") or []) if isinstance(l, str) and l.strip()][:6] if isinstance(t.get("diagramLabels"), list) else []
                if diagram_labels:
                    image_prompt = (
                        f"A clean, simple, colorful flat-vector educational diagram for a Grade {body.grade} school student, depicting: {image_subject} ({body.subject}). "
                        f"Clearly label these parts on the diagram: {', '.join(diagram_labels)} — each label in bold black sans-serif text, spelled exactly as given, connected to its part with a thin leader line. No other text in the image. Friendly, age-appropriate, plain light background."
                    )
                else:
                    image_prompt = (
                        f"A clean, simple, colorful flat-vector educational illustration for a Grade {body.grade} school student, depicting: {image_subject} ({body.subject}). "
                        "No text or labels in the image. Friendly, age-appropriate, plain light background."
                    )
                generated = await generate_illustration(image_prompt)
                return {**t, "image": ({"url": generated["url"], "caption": image_subject} if generated else None)}

            import asyncio
            topics_with_images = await asyncio.gather(*[_with_image(t) for t in raw_topics])
            return {**parsed, "topics": list(topics_with_images)}

        value, _from_cache = await with_cache(
            ck("test-study-guide-v2", body.topic.lower().strip(), body.subject.lower().strip(), body.grade, body.totalMarks or 0, top_interest),
            604800,
            _compute,
        )

        def _str_arr(v):
            return [x for x in (v or []) if isinstance(x, str) and x.strip()] if isinstance(v, list) else []

        raw_topics = value.get("topics") if isinstance(value, dict) and isinstance(value.get("topics"), list) else []
        topics = []
        for t in raw_topics:
            if not isinstance(t, dict) or not isinstance(t.get("name"), str) or not t.get("name").strip():
                continue
            practice_questions = []
            for q in (t.get("practiceQuestions") or []):
                if isinstance(q, dict) and isinstance(q.get("question"), str) and q.get("question").strip() and isinstance(q.get("answer"), str) and q.get("answer").strip():
                    practice_questions.append({"question": q["question"].strip(), "answer": q["answer"].strip()})
            image = t.get("image") if isinstance(t.get("image"), dict) and isinstance(t.get("image", {}).get("url"), str) else None
            topics.append({
                "name": t["name"].strip(),
                "summary": t.get("summary").strip() if isinstance(t.get("summary"), str) else "",
                "keyPoints": _str_arr(t.get("keyPoints")),
                "examples": _str_arr(t.get("examples")),
                "commonMistakes": _str_arr(t.get("commonMistakes")),
                "practiceQuestions": practice_questions,
                "image": image,
            })

        if not topics:
            raise ValueError("No valid study guide returned by AI")
        return {"topics": topics}
    except Exception as e:
        print(f"[test-study-guide] generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate study guide")


# POST /api/student-report
@router.post("/student-report")
async def student_report(body: StudentReportSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("student-report", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        marks_summary = "\n".join(
            f"  - {m.topic}: {m.score}/{m.totalMarks} ({round((m.score / m.totalMarks) * 100)}%)" for m in body.marks
        ) if body.marks else "  No assessments yet."

        mastery_summary = "\n".join(
            f"  - {m.topic}: mastery {round(m.mastery * 100)}% ({m.attempts} attempt{'s' if m.attempts != 1 else ''})" for m in body.mastery
        ) if body.mastery else "  No mastery data."

        warning_text = "\n".join(f"  [{w.level}] {w.reason}" for w in body.warnings) if body.warnings else "  None."

        prompt = f"""You are a compassionate AI assistant helping an Indian government school teacher write a student progress report.

Student: {body.student.name} (Roll #{body.student.rollNumber})
Grade: {body.grade}, Subject: {body.subject}
Interests: {', '.join(body.student.interests) or 'not recorded'}
Goal/Ambition: {body.student.goal or 'not recorded'}
Overall Attendance: {round(body.attendanceRate * 100)}%

Assessment Results:
{marks_summary}

Topic Mastery:
{mastery_summary}

Flags/Warnings:
{warning_text}

Write a warm, encouraging student progress report with these FOUR sections:
1. Overall Summary (2 sentences — performance snapshot)
2. Strengths (1-2 specific topics or skills where the student is doing well)
3. Areas for Growth (1-2 topics that need more practice — be encouraging, not harsh)
4. Recommendation (one actionable suggestion for teacher or student, linking to the student's interests/goal if possible)

Keep it simple, clear, and suitable for an Indian school context. No jargon.

Return ONLY valid JSON:
{{
  "summary": "...",
  "strengths": "...",
  "growth": "...",
  "recommendation": "..."
}}"""

        attendance_bucket = "low" if body.attendanceRate < 0.6 else "mid" if body.attendanceRate < 0.85 else "high"

        async def _compute():
            raw = await call_ai([{"role": "user", "content": prompt}])
            return json.loads(raw)

        parsed, from_cache = await with_cache(
            ck("student-report", body.student.rollNumber, body.grade, body.subject, len(body.marks), len(body.mastery), attendance_bucket, len(body.warnings)),
            604800,
            _compute,
        )
        api_log("student-report", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return parsed
    except Exception as e:
        api_log("student-report", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {
            "summary": "Could not generate report at this time.",
            "strengths": "",
            "growth": "",
            "recommendation": "",
        }


# POST /api/potential
@router.post("/potential")
async def potential(body: PotentialSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("potential", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        prompt = f"""Write ONE short sentence for a teacher about this student's hidden potential.
Be specific, positive, and encouraging. Maximum 20 words. No jargon.

Student name: {body.studentName}
Signal type: {body.signal.get('type')}
Data: {json.dumps(body.signal.get('data'))}

Return valid JSON only: {{ "sentence": "Your sentence here." }}"""

        async def _compute():
            result = await call_ai([{"role": "user", "content": prompt}])
            return json.loads(result)

        parsed, from_cache = await with_cache(
            ck("potential", body.signal.get("type"), json.dumps(body.signal.get("data"))), 604800, _compute
        )
        api_log("potential", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return parsed
    except Exception as e:
        api_log("potential", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {"sentence": ""}


# POST /api/test-prep (public — vision-tier rate limit only)
@router.post("/test-prep")
async def test_prep(body: TestPrepSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    interests = body.interests or []
    interest_hint = f"Where natural, use examples from: {', '.join(interests[:2])}." if interests else "Use simple Indian everyday examples (cricket, market, cooking, farming) where helpful."

    prompt = f"""You are helping a Grade {body.grade} student in an Indian government school prepare for an upcoming test in {body.subject}.
Test topic: {body.topic}
{interest_hint}

Produce focused revision material to help them prepare, organized as 3–5 short "sections":
- The first section must be a brief intro: {{ "heading": short 2–4 word heading, "body": 1–2 simple sentences recapping what this topic is about }}.
- Later sections should cover the key facts, parts, or steps as: {{ "heading": short 2–4 word heading, "bullets": 3–6 short points (each under 15 words) }}.
- Each section has EITHER "body" OR "bullets", never both.
- "imageQuery": the single best Wikipedia article title for a helpful diagram or photo of this concept (e.g. "Human heart", "Water cycle", "Human body"). 1–3 words, a real encyclopedia topic.
- "diagramLabels": 3–6 short labels (1–2 words each, e.g. "Roots", "Stem", "Leaves") for the main visible parts/steps of this concept that a labelled diagram should point to. Use plain English words a Grade {body.grade} student knows. If this concept has no distinct visual parts to label (e.g. an abstract idea), return an empty array.

Rules:
- Match Grade {body.grade} level — simple language, no jargon.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "sections": [
    {{ "heading": "...", "body": "..." }},
    {{ "heading": "...", "bullets": ["...", "..."] }}
  ],
  "imageQuery": "...",
  "diagramLabels": ["...", "..."]
}}"""

    try:
        top_interest = re.sub(r"\s+", "_", interests[0][:20].lower()) if interests else "none"

        async def _compute():
            text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 1100})
            parsed = json.loads(_strip_and_match_obj(text))
            image_subject = parsed.get("imageQuery").strip() if isinstance(parsed.get("imageQuery"), str) and parsed.get("imageQuery").strip() else body.topic
            diagram_labels = [l for l in (parsed.get("diagramLabels") or []) if isinstance(l, str) and l.strip()][:6] if isinstance(parsed.get("diagramLabels"), list) else []
            if diagram_labels:
                image_prompt = (
                    f"A clean, simple, colorful flat-vector educational diagram for a Grade {body.grade} school student, depicting: {image_subject} ({body.subject}). "
                    f"Clearly label these parts on the diagram: {', '.join(diagram_labels)} — each label in bold black sans-serif text, spelled exactly as given, connected to its part with a thin leader line. No other text in the image. Friendly, age-appropriate, plain light background."
                )
            else:
                image_prompt = (
                    f"A clean, simple, colorful flat-vector educational illustration for a Grade {body.grade} school student, depicting: {image_subject} ({body.subject}). "
                    "No text or labels in the image. Friendly, age-appropriate, plain light background."
                )
            generated = await generate_illustration(image_prompt)
            image = {"url": generated["url"], "caption": image_subject} if generated else None
            return {**parsed, "image": image}

        value, _from_cache = await with_cache(
            ck("test-prep-v9", body.topic.lower().strip(), body.subject.lower().strip(), body.grade, top_interest),
            604800,
            _compute,
        )
        image = value.get("image") if isinstance(value, dict) and isinstance(value.get("image", {}).get("url") if isinstance(value.get("image"), dict) else None, str) else None
        sections = []
        for s in (value.get("sections") or []) if isinstance(value, dict) else []:
            if not isinstance(s, dict) or not isinstance(s.get("heading"), str) or not s.get("heading").strip():
                continue
            body_text = s.get("body").strip() if isinstance(s.get("body"), str) and s.get("body").strip() else None
            bullets = [b for b in (s.get("bullets") or []) if isinstance(b, str) and b.strip()] if isinstance(s.get("bullets"), list) else []
            if not body_text and not bullets:
                continue
            entry = {"heading": s["heading"].strip()}
            if body_text:
                entry["body"] = body_text
            else:
                entry["bullets"] = bullets
            sections.append(entry)
        if not sections:
            raise ValueError("No valid prep material returned by AI")
        return {"sections": sections, "image": image}
    except Exception as e:
        print(f"[test-prep] generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate prep material")


# POST /api/peer-pair
@router.post("/peer-pair")
async def peer_pair(body: PeerPairSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("peer-pair", ip, 0, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        if len(body.students) < 2:
            return {"pairs": [], "activity": ""}

        student_list = "\n".join(
            f"{i + 1}. {s.name} (mastery: {round(s.avgMastery * 100)}%, interests: {', '.join(s.interests[:3]) or 'none'}, goal: {s.goal or 'none'})"
            for i, s in enumerate(body.students)
        )

        prompt = f"""You are an AI assistant helping a teacher in an Indian government school create peer learning pairs.

Topic: {body.topic}
Subject: {body.subject}

Students (with mastery level and interests):
{student_list}

Create peer pairs where a stronger student (higher mastery) is paired with a weaker student (lower mastery) who shares at least one interest or similar goal. This helps the stronger student reinforce knowledge while the weaker student gets relatable peer support.

Rules:
- Pair students with DIFFERENT mastery levels (ideally one >60% with one <50%)
- Prefer pairs who share an interest or similar goal
- Every student should be in exactly one pair (if odd number, one group of 3 is fine)
- For each pair, suggest a SHORT, specific peer activity (1 sentence, 15 words max) the teacher can assign

Return ONLY valid JSON:
{{
  "pairs": [
    {{
      "mentor": "student name",
      "mentee": "student name",
      "sharedInterest": "what they share (or 'different interests')",
      "activity": "one sentence activity suggestion"
    }}
  ]
}}"""

        student_key = "~".join(sorted(s.id for s in body.students))

        async def _compute():
            raw = await call_ai([{"role": "user", "content": prompt}])
            return json.loads(raw)

        parsed, from_cache = await with_cache(
            ck("peer-pair", body.topic.lower().strip(), body.subject.lower().strip(), student_key), 86400, _compute
        )
        api_log("peer-pair", ip, (time.time() - t0) * 1000, from_cache, "ok")
        return {"pairs": parsed.get("pairs") or []}
    except Exception as e:
        api_log("peer-pair", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        return {"pairs": []}


# POST /api/save-score
@router.post("/save-score")
async def save_score(body: SaveScoreSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    try:
        if body.score > body.totalMarks:
            api_log("save-score", ip, (time.time() - t0) * 1000, False, "bad_request", user_id=user["id"], error="score > totalMarks")
            raise HTTPException(status_code=400, detail="score cannot exceed totalMarks")

        import uuid
        ac = create_admin_client()
        ac.table("marks").upsert(
            {
                "id": str(uuid.uuid4()),
                "test_id": body.testId,
                "student_id": body.studentId,
                "score": _whole_if_int(body.score),
                "entered_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "source": body.source or "ai_scanned",
                "breakdown": body.breakdown,
                "feedback": body.feedback,
                "image_url": body.imageUrl,
                "drive_url": body.driveUrl,
            },
            on_conflict="student_id,test_id",
        ).execute()

        api_log("save-score", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"success": True}
    except HTTPException:
        raise
    except Exception as e:
        api_log("save-score", ip, (time.time() - t0) * 1000, False, "error", user_id=user.get("id"), error=str(e))
        raise HTTPException(status_code=500, detail=str(e))


# ─── Worksheets (plain CRUD, teacher-session-gated by convention) ───────────

@router.get("/worksheets")
async def get_worksheets(teacherId: Optional[str] = None, user: dict = Depends(require_user)):
    if not teacherId:
        raise HTTPException(status_code=400, detail="teacherId is required")
    ac = create_admin_client()
    res = ac.table("worksheets").select("*").eq("teacher_id", teacherId).order("created_at", desc=True).execute()
    return {"worksheets": res.data or []}


@router.post("/worksheets")
async def upsert_worksheet(body: WorksheetUpsertSchema, user: dict = Depends(require_user)):
    ac = create_admin_client()
    ac.table("worksheets").upsert({
        "id": body.id,
        "teacher_id": body.teacherId,
        "class_id": body.classId,
        "topic": body.topic,
        "subject": body.subject or "",
        "grade": body.grade or "",
        "template": body.template,
        "total_marks": _whole_if_int(body.totalMarks or 0),
        "sections": body.sections or [],
        "answer_key": body.answerKey or {},
        "created_at": body.createdAt,
    }).execute()
    return {"ok": True}


@router.delete("/worksheets")
async def delete_worksheet(id: Optional[str] = None, user: dict = Depends(require_user)):
    if not id:
        raise HTTPException(status_code=400, detail="id is required")
    ac = create_admin_client()
    ac.table("worksheets").delete().eq("id", id).execute()
    return {"ok": True}


# POST /api/generate-answer-key
@router.post("/generate-answer-key")
async def generate_answer_key(body: GenerateAnswerKeySchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    items = []
    for si, sec in enumerate(body.sections):
        for qi, q in enumerate(sec.questions):
            if sec.type != "mcq":
                items.append({"key": f"{si}-{qi}", "type": sec.type, "label": sec.label, "text": q.text})

    if not items:
        return {"answerKey": {}}

    questions_block = "\n".join(f"{i + 1}. [{it['type']}] {it['text']}" for i, it in enumerate(items))
    prompt = f"""You are generating answer keys for a Grade {body.grade} {body.subject} worksheet on "{body.topic}".

For each question below, write a concise, grade-appropriate answer.
- fill-in-blank: one or two words that fill the blank
- short-answer: 1-2 sentence answer with key facts
- long-answer: 4-5 bullet points or a short paragraph with the main marking points

Questions:
{questions_block}

Return ONLY valid JSON, no markdown:
{{
  "answers": [
    {{ "key": "{items[0]['key']}", "answer": "..." }},
    ...
  ]
}}"""

    try:
        text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 1500})
        parsed = json.loads(_strip_and_match_obj(text))
        answer_key: dict = {}
        for a in (parsed.get("answers") or []):
            if isinstance(a, dict) and a.get("key") and a.get("answer"):
                answer_key[a["key"]] = a["answer"]
        for si, sec in enumerate(body.sections):
            if sec.type == "mcq":
                for qi, q in enumerate(sec.questions):
                    if q.answer:
                        answer_key[f"{si}-{qi}"] = q.answer
        return {"answerKey": answer_key}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to generate answer key")


_TYPE_LABELS = {
    "mcq": "Multiple Choice Questions",
    "fill-in-blank": "Fill in the Blanks",
    "short-answer": "Short Answer Questions",
    "long-answer": "Long Answer Questions",
}
_SECTION_LETTERS = ["A", "B", "C", "D", "E"]


# POST /api/generate-worksheet
@router.post("/generate-worksheet")
async def generate_worksheet_route(body: GenerateWorksheetSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    total_marks = sum(d.count * d.marksEach for d in body.distribution)

    dist_lines = "\n".join(
        f"Section {_SECTION_LETTERS[i]} — {_TYPE_LABELS.get(d.type, d.type)}: exactly {d.count} question{'s' if d.count > 1 else ''} × {d.marksEach} mark{'s' if d.marksEach > 1 else ''} each"
        for i, d in enumerate(body.distribution)
    )

    section_schemas = []
    for i, d in enumerate(body.distribution):
        letter = _SECTION_LETTERS[i]
        label = f"Section {letter} — {_TYPE_LABELS.get(d.type, d.type)}"
        if d.type == "mcq":
            section_schemas.append(
                f'{{ "type": "mcq", "label": "{label}", "marksEach": {d.marksEach}, "questions": [{{ "text": "Question?", "options": ["A. First", "B. Second", "C. Third", "D. Fourth"], "answer": "A" }}, ... (exactly {d.count} items) ] }}'
            )
        else:
            section_schemas.append(
                f'{{ "type": "{d.type}", "label": "{label}", "marksEach": {d.marksEach}, "questions": [{{ "text": "Question?" }}, ... (exactly {d.count} items) ] }}'
            )

    prompt = f"""You are generating a school exam worksheet.
Subject: {body.subject} | Grade: {body.grade} | Topic: {body.topic} | Total: {total_marks} marks

{dist_lines}

Rules:
- Strictly Grade {body.grade} difficulty — simple, clear language.
- MCQ: exactly 4 options each (A, B, C, D). Set "answer" to the correct option letter only (e.g. "B").
- Fill in blank: embed "___" in the question text where the answer goes.
- Short answer: question only, no answer needed.
- Long answer: question only, add "(Write 3–4 sentences)" guidance in the text.
- Generate EXACTLY the count specified for each section — no more, no fewer.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "sections": [
    {','.join(section_schemas)}
  ]
}}"""

    try:
        text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 2500})
        parsed = json.loads(_strip_and_match_obj(text))
        return {"sections": parsed.get("sections") or [], "topic": body.topic.strip(), "totalMarks": total_marks}
    except Exception:
        raise HTTPException(status_code=500, detail="Failed to generate worksheet")


# POST /api/smart-lesson
@router.post("/smart-lesson")
async def smart_lesson(body: SmartLessonSchema, request: Request, user: dict = Depends(require_user)):
    import httpx
    import os

    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    ac = create_admin_client()

    students_res = ac.table("students").select("id, name").eq("class_id", body.classId).eq("is_active", True).execute()
    student_ids = [s["id"] for s in (students_res.data or [])]
    total_students = len(student_ids)

    marks_res = (
        ac.table("marks").select("student_id, score, tests(topic, total_marks, subject)").in_("student_id", student_ids).execute()
        if student_ids else None
    )
    marks = marks_res.data or [] if marks_res else []

    topic_stats: dict[str, dict] = {}
    for mark in marks:
        tests = mark.get("tests")
        if not tests or not tests.get("total_marks"):
            continue
        t = tests["topic"]
        pct = mark["score"] / tests["total_marks"]
        cur = topic_stats.setdefault(t, {"totalPct": 0.0, "count": 0})
        cur["totalPct"] += pct
        cur["count"] += 1

    # The original TS route queries a table called "topic_mastery", which does
    # not exist (the real table is "student_topic_mastery", per lib/supabase-queries.ts)
    # — there, the unchecked Supabase error silently no-ops this part of gap
    # detection; here, supabase-py raises on error, so the correct table name
    # is used instead of reproducing a hard crash.
    mastery_res = (
        ac.table("student_topic_mastery").select("student_id, topic, mastery").in_("student_id", student_ids).execute()
        if student_ids else None
    )
    for row in (mastery_res.data or [] if mastery_res else []):
        cur = topic_stats.setdefault(row["topic"], {"totalPct": 0.0, "count": 0})
        cur["totalPct"] += row["mastery"]
        cur["count"] += 1

    gap_topics = []
    for t, stats in topic_stats.items():
        if t.lower() == body.topic.lower():
            continue
        avg = stats["totalPct"] / stats["count"]
        if avg < 0.65:
            weak_count = sum(
                1 for m in marks
                if m.get("tests") and m["tests"]["topic"] == t and m["tests"].get("total_marks")
                and (m["score"] / m["tests"]["total_marks"]) < 0.65
            )
            gap_topics.append({
                "topic": t,
                "avgMastery": round(avg * 100) / 100,
                "weakStudentCount": weak_count,
                "totalStudents": total_students,
            })

    top_gaps = sorted(gap_topics, key=lambda g: g["avgMastery"])[:3]

    if top_gaps:
        gap_lines = "\n".join(f'- "{g["topic"]}": {round(g["avgMastery"] * 100)}% avg mastery ({g["weakStudentCount"]}/{g["totalStudents"]} students weak)' for g in top_gaps)
        gap_context = (
            f"Assessment data shows these prior concepts are weak in this class:\n{gap_lines}\n\n"
            'Your job: at the exact moment each of these concepts naturally becomes relevant while explaining today\'s topic, explain it briefly in plain teacher language — as if you\'re just explaining the topic really well. Never announce "we need to review this" or "this is from last year" or use words like prerequisite, recap, or revision. It must feel like one seamless lesson.'
        )
    else:
        gap_context = "No specific knowledge gaps detected. Write a thorough, well-explained lesson that naturally reinforces foundational ideas as part of teaching."

    system_prompt = """You are an expert classroom teacher writing a practical lesson plan.

Your teaching style: when students are missing prior knowledge, you never stop class or call it out. Instead, you explain the missing concept at exactly the moment it becomes useful for understanding today's topic — woven into your explanation so naturally that students who already know it hear a good reminder, and students who missed it learn it without feeling behind.

This is not remediation. It is just excellent, thorough teaching."""

    subtopic_line = f"\nSubtopic (focus specifically on this): {body.subtopic}" if body.subtopic else ""
    user_prompt = f"""Write a SHORT classroom lesson plan for:

Topic: {body.topic}{subtopic_line}
Subject: {body.subject}
Grade: {body.grade}
Students: {total_students}

{gap_context}

Return ONLY valid JSON (no markdown, no extra text):
{{
  "hook": "One short punchy question or statement — max 15 words",
  "sections": [
    {{
      "type": "teach",
      "title": "3-4 word title",
      "content": "1-2 sentences max. Direct, simple, classroom-ready.",
      "bridgeNote": {{
        "concept": "Prior concept name",
        "text": "One sentence — what the teacher says naturally at this moment"
      }}
    }}
  ],
  "closingActivity": "One sentence. Doable immediately in class."
}}

Rules:
- 3 to 4 sections total
- Most sections type "teach", max 1 type "check"
- bridgeNote OPTIONAL — only when a gap concept naturally surfaces; every listed gap must appear once
- content: never say "review", "recall", "prerequisite", "from last year"
- Keep everything SHORT — this is a quick reference card, not an essay"""

    api_key = os.environ.get("OPENROUTER_API_KEY")
    if not api_key:
        raise HTTPException(status_code=500, detail="AI not configured")

    async with httpx.AsyncClient(timeout=90) as client:
        ai_res = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "X-Title": "EduTeach Smart Lesson"},
            json={
                "model": os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash"),
                "messages": [{"role": "system", "content": system_prompt}, {"role": "user", "content": user_prompt}],
                "temperature": 0.7,
                "max_tokens": 1400,
            },
        )

    if ai_res.status_code >= 400:
        raise HTTPException(status_code=500, detail=f"AI error: {ai_res.text}")

    ai_data = ai_res.json()
    raw = ai_data.get("choices", [{}])[0].get("message", {}).get("content", "")

    try:
        cleaned = re.sub(r"```\s*$", "", re.sub(r"^```json\s*", "", raw.strip(), flags=re.I), flags=re.I).strip()
        lesson = json.loads(cleaned)
    except Exception:
        raise HTTPException(status_code=500, detail="AI returned malformed JSON")

    return {
        "topic": body.topic,
        "subtopic": body.subtopic or None,
        "subject": body.subject,
        "grade": body.grade,
        "totalStudents": total_students,
        "gapTopics": top_gaps,
        "lesson": lesson,
    }


# ─── Personality story (student-token GET) ──────────────────────────────────

def _is_valid_option(o) -> bool:
    return isinstance(o, dict) and isinstance(o.get("text"), str) and o.get("leadsToward") in ("wise", "regret")


def _is_valid_step(v) -> bool:
    return (
        isinstance(v, dict) and isinstance(v.get("scene"), str) and isinstance(v.get("question"), str)
        and isinstance(v.get("options"), list) and len(v["options"]) == 3 and all(_is_valid_option(o) for o in v["options"])
    )


def _is_valid_triple(v) -> bool:
    return isinstance(v, dict) and all(isinstance(v.get(k), str) for k in ("wise", "mixed", "regret"))


def _is_valid_story(v) -> bool:
    return (
        isinstance(v, dict) and isinstance(v.get("title"), str) and isinstance(v.get("introduction"), str)
        and isinstance(v.get("steps"), list) and len(v["steps"]) == 3 and all(_is_valid_step(s) for s in v["steps"])
        and _is_valid_triple(v.get("endings")) and _is_valid_triple(v.get("personalityAnalysis")) and _is_valid_triple(v.get("learningSummary"))
    )


@router.get("/personality-story")
async def personality_story(request: Request, student_id: str = Depends(require_student_token)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests.")

    import uuid
    ac = create_admin_client()
    date = today_date_str()

    existing = ac.table("personality_stories").select("trait, story").eq("student_id", student_id).eq("date", date).maybe_single().execute()
    if existing and existing.data:
        return {"trait": existing.data["trait"], "story": existing.data["story"]}

    student_res = ac.table("students").select("name, interests").eq("id", student_id).single().execute()
    student = student_res.data
    if not student:
        raise HTTPException(status_code=404, detail="Student not found")

    trait = trait_for_date(date)
    interests = student.get("interests") or []
    interest_hint = (
        f"Weave in one of these interests as the setting or characters: {', '.join(interests[:3])}."
        if interests else "Use a simple everyday Indian setting — school, playground, home, or market."
    )
    first_name = student["name"].split(" ")[0]

    prompt = f"""You are writing a short, interactive personality-development story for a young Indian schoolchild named {first_name}, in Class 1 to Class 5 (age 6-10).

Personality trait to teach: {trait}
{interest_hint}

READING LEVEL — this is the most important rule. Write for a 6-10 year old reading alone:
- Short sentences only — about 8-12 words each, never long or twisty.
- Simple, everyday words a young child already knows. No big or abstract words (don't say things like "consequence," "reflect," "prioritize" — say what actually happened instead).
- Concrete and visual — say exactly what the character sees/does/says, not how they "feel" in the abstract.
- Never explain the trait itself or lecture the reader — the child should feel the story, not be told a lesson mid-story.

SETTING — keep it to a child's real world. Good scenarios: school, classroom, playground, friends, siblings, parents, grandparents, festivals (Diwali, Holi, Eid, etc.), sharing food or toys, homework, a cricket/sports match, pocket money, finding something that isn't theirs, a promise to a friend, screen time / TV / games. Do NOT use grown-up settings like jobs, careers, exams-as-high-stakes, dating, or money problems beyond simple pocket money.

Write the story as exactly 3 connected scenes ("steps"), each ending at a decision point. Each scene must continue directly from the one before it — the SAME ongoing situation moving forward in time, not a new unrelated one each time. Give it something real a child would recognize (a friendship, a game, a promise, a family moment) — not a generic "be nice" filler scenario.

IMPORTANT — the child is NEVER interrupted with feedback while the story is happening. They pick an option and the story silently moves straight on to the next scene, with no reveal, no judgment, no hint of right-or-wrong at any point in the middle. Because of this, write each step's "scene" so it reads naturally as the next moment in the story NO MATTER which of the 3 options was picked before it — do not write it as a direct reaction to one specific choice. Keep the situation moving forward in a way that stays sensible regardless of which option led there.

First, write an "introduction" — 1-2 short sentences that introduce the character and where they are, before anything happens yet.

For each of the 3 steps, write:
- "scene": 2-3 short sentences of story. For step 1, this sets up the situation right after the introduction. For steps 2 and 3, this moves the same situation forward to its next moment. End right at the moment of a decision.
- "question": one short sentence asking what the character should do next.
- "options": exactly 3 short choices (a few words each, simple words), each with:
  - "leadsToward": "wise" if this choice reflects {trait} well, or "regret" if it doesn't. Choices should feel understandable either way (never villainous or scary), just leaning one way or the other.

Only AFTER all 3 choices are made does the child see anything else — the story then plays out to ONE ending that reflects the whole pattern of choices, followed by an explanation. Nothing before this point may reveal how any single choice turned out.

Write three possible closing scenes in "endings" — this is the single moment the child finally sees the real result of their choices, specific to what actually happened across all 3 steps, not a generic wrap-up:
- "wise": the character mostly chose well through the story. Show the concrete good result those specific choices led to, AND have someone (a friend, parent, or teacher) or the character themself plainly say something warm, in their own words.
- "regret": the character mostly chose poorly. This should be a genuinely disappointing outcome, not softened — spell out the actual, specific thing that went wrong because of those choices, concrete and simple (not vague feelings), so the child clearly feels "that didn't go well." Keep it age-appropriate and never scary or harsh toward the character as a person — the choices were wrong, not the child.
- "mixed": an in-between result — name one specific thing that went right and one that went less well, and the result is noticeably weaker than the "wise" ending.

Then write "personalityAnalysis" — for EACH of wise/mixed/regret, 2-3 simple sentences spoken directly to the child ("You...") plainly explaining WHY that outcome happened — connect it clearly to the specific choices made across the story. Plain words only, not abstract ("because you chose to X, then Y happened").

Then write "learningSummary" — for EACH of wise/mixed/regret, one short, concrete sentence telling the child exactly what the better choice would have been at these decision points (for "wise", instead affirm that this is exactly what to keep doing). This must be real, usable advice for a similar situation in real life — not a vague mood note. Say plainly what to do differently, in simple child-friendly words.

Also write a short "title" (3-6 simple words) for the story.

No violence, no scary content, no narrator voice stating a moral mid-story — everything in the steps and endings must come through as the character's own experience. The personalityAnalysis and learningSummary are the only places allowed to speak directly to the reader, and only after the ending.

Return ONLY valid JSON, no markdown, no extra text:
{{
  "title": "string",
  "introduction": "string",
  "steps": [
    {{
      "scene": "string",
      "question": "string",
      "options": [
        {{ "text": "string", "leadsToward": "wise" }},
        {{ "text": "string", "leadsToward": "wise" }},
        {{ "text": "string", "leadsToward": "regret" }}
      ]
    }}
  ],
  "endings": {{ "wise": "string", "mixed": "string", "regret": "string" }},
  "personalityAnalysis": {{ "wise": "string", "mixed": "string", "regret": "string" }},
  "learningSummary": {{ "wise": "string", "mixed": "string", "regret": "string" }}
}}
(steps must contain exactly 3 entries, each with exactly 3 options)"""

    try:
        text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 2000})
        parsed = json.loads(_strip_and_match_obj(text))
        if not _is_valid_story(parsed):
            raise ValueError("AI returned an incomplete story")

        story = {**parsed, "trait": trait}

        try:
            ac.table("personality_stories").insert(
                {"id": str(uuid.uuid4()), "student_id": student_id, "date": date, "trait": trait, "story": story}
            ).execute()
        except APIError:
            # Someone else won the race — fetch what they inserted instead of erroring.
            winner = ac.table("personality_stories").select("trait, story").eq("student_id", student_id).eq("date", date).maybe_single().execute()
            if winner and winner.data:
                return {"trait": winner.data["trait"], "story": winner.data["story"]}

        return {"trait": trait, "story": story}
    except HTTPException:
        raise
    except Exception as e:
        print(f"[personality-story] generation failed: {e}")
        raise HTTPException(status_code=500, detail="Failed to generate story")
