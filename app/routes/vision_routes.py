import json
import re
import time
from fastapi import APIRouter, Request, Depends, HTTPException

from ..lib.ai import call_ai
from ..lib.pipeline_client import run_ocr, run_text
from ..lib.google_drive_upload import upload_to_google_drive
from ..lib.graders import grade_mcq, grade_fib, grade_short_answer
from ..lib.grading_cache import get_consistent_long_answer_grade
from ..lib.grade_review import assess_paper_confidence
from ..lib.schemas import (
    ExtractStudentsSchema, ExtractSyllabusSchema, ScanStudentsSchema, ScanAttendanceSchema,
    UploadScanSchema, GradeImageSchema, GradePaperSchema2, GradeScanSchema2,
)
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit, check_vision_rate_limit
from ..lib.supabase_clients import create_admin_client
from ..deps import require_user

router = APIRouter()


def _extract_json_obj(raw: str) -> str:
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", raw)
    if fenced:
        return fenced.group(1)
    first, last = raw.find("{"), raw.rfind("}")
    if first != -1 and last != -1:
        return raw[first : last + 1]
    return raw


EXTRACT_STUDENTS_SYSTEM = """You are reading a photo of a school class roster or attendance register. It may be handwritten (including Indian regional handwriting styles) or printed, and may be messy, angled, or partly faded.

Extract every student's name and roll number you can identify.

Return ONLY valid JSON (no markdown, no code fences):
{
  "students": [
    { "name": "student full name", "rollNumber": "roll number as written" }
  ]
}

Rules:
- List students in the order they appear on the page (top to bottom).
- If a roll number isn't clearly visible for an entry, use an empty string for rollNumber — don't invent a number that isn't there.
- Correct obvious spelling/OCR issues in names only where you're confident — don't invent names that aren't there.
- Skip headers, titles, and column labels (e.g. "Class 5A Roster", "Name", "Roll No.") — only actual student entries.
- If handwriting for an entry is fully illegible, skip that entry rather than guessing."""


# POST /api/extract-students
@router.post("/extract-students")
async def extract_students(body: ExtractStudentsSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        api_log("extract-students", ip, (time.time() - t0) * 1000, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests.")

    try:
        messages = [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": body.image}},
            {"type": "text", "text": EXTRACT_STUDENTS_SYSTEM},
        ]}]
        raw = await call_ai(messages, {"json_mode": False, "temperature": 0.1})

        if not raw:
            api_log("extract-students", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="empty AI response")
            raise HTTPException(status_code=500, detail="AI returned an empty response. Please try again.")

        try:
            result = json.loads(_extract_json_obj(raw))
        except Exception:
            api_log("extract-students", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="JSON parse failed")
            raise HTTPException(status_code=500, detail="Could not read the photo. Try a clearer or better-lit picture.")

        students = [
            {"name": str(s.get("name", "")).strip(), "rollNumber": str(s.get("rollNumber", "")).strip()}
            for s in (result.get("students") or [])
        ]
        students = [s for s in students if s["name"]]

        if not students:
            api_log("extract-students", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="no students found")
            raise HTTPException(status_code=422, detail="No student names found in this photo. Try a clearer picture.")

        api_log("extract-students", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"students": students}
    except HTTPException:
        raise
    except Exception as e:
        api_log("extract-students", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        raise HTTPException(status_code=500, detail="Unexpected server error. Please try again.")


EXTRACT_SYLLABUS_SYSTEM = """You are a school syllabus parser for Indian curriculum (CBSE/State boards).
Parse the input and return structured topics with their sub-topics.

Return ONLY valid JSON (no markdown, no code fences):
{
  "topics": [
    {
      "topic": "unit or chapter name",
      "description": "brief one-line description of the unit",
      "subTopics": ["individual lesson or concept 1", "individual lesson or concept 2"],
      "weekNumber": 1
    }
  ]
}

Rules:
- Each unit/chapter = one topic entry
- subTopics = array of individual lessons, concepts, or sub-units listed under that unit
- description = one short sentence describing the overall unit (not a list)
- Assign weekNumber sequentially starting from 1
- If no clear units, treat each row/line as one topic with empty subTopics
- Keep topic names short and clean
- Each subTopic should be a meaningful standalone lesson (not just a single word)"""


# POST /api/extract-syllabus
@router.post("/extract-syllabus")
async def extract_syllabus(body: ExtractSyllabusSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        api_log("extract-syllabus", ip, (time.time() - t0) * 1000, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many requests.")

    try:
        if body.image:
            messages = [{"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": body.image}},
                {"type": "text", "text": EXTRACT_SYLLABUS_SYSTEM},
            ]}]
        else:
            messages = [{"role": "user", "content": f"{EXTRACT_SYLLABUS_SYSTEM}\n\nSyllabus to parse:\n{body.text}"}]

        raw = await call_ai(messages, {"json_mode": False, "temperature": 0.1})

        if not raw:
            api_log("extract-syllabus", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="empty AI response")
            raise HTTPException(status_code=500, detail="AI returned an empty response. Please try again.")

        try:
            result = json.loads(_extract_json_obj(raw))
        except Exception:
            api_log("extract-syllabus", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="JSON parse failed")
            raise HTTPException(status_code=500, detail="Could not parse AI response. Try rephrasing your syllabus text.")

        topics = []
        for i, t in enumerate(result.get("topics") or []):
            raw_subs = t.get("subTopics") if isinstance(t.get("subTopics"), list) else []
            week_number = t.get("weekNumber") if isinstance(t.get("weekNumber"), int) else i + 1
            topic = {
                "topic": str(t.get("topic") or f"Unit {i + 1}").strip(),
                "description": str(t.get("description") or "").strip(),
                "weekNumber": week_number,
                "subTopics": [str(s).strip() for s in raw_subs if str(s).strip()],
            }
            if topic["topic"]:
                topics.append(topic)

        api_log("extract-syllabus", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"topics": topics}
    except HTTPException:
        raise
    except Exception as e:
        api_log("extract-syllabus", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        raise HTTPException(status_code=500, detail="Unexpected server error. Please try again.")


SCAN_STUDENTS_PROMPT = """You are scanning a school document (class register, attendance sheet, marksheet, or student list) to extract student names.

Return ONLY valid JSON (no markdown, no code fences):
{
  "names": ["Full Name 1", "Full Name 2", "Full Name 3"]
}

Rules:
- Extract only student/person names — ignore column headers, numbers, roll numbers, dates, subjects, marks
- Return each name as a clean proper-case string (e.g. "Ravi Kumar", not "RAVI KUMAR" or "ravi kumar")
- If a name has a roll number prefix like "01. Ravi Kumar", return only "Ravi Kumar"
- If you cannot find any names, return { "names": [] }
- Do not invent names — only extract what is visible in the image"""

SCAN_STUDENTS_OCR_INSTRUCTION = (
    "Transcribe this document exactly as written — every line, name, roll number, and heading. "
    "Preserve the line-by-line layout. Do not interpret, summarise, or reorder anything; just copy the visible text."
)


def _build_names_prompt(transcription: str) -> str:
    return f"""You are extracting student names from the transcription of a school document (class register, attendance sheet, marksheet, or student list).

TRANSCRIBED TEXT:
{transcription}

Return ONLY valid JSON (no markdown, no code fences):
{{"names":["Full Name 1","Full Name 2","Full Name 3"]}}

Rules:
- Extract only student/person names — ignore column headers, numbers, roll numbers, dates, subjects, marks
- Return each name as a clean proper-case string (e.g. "Ravi Kumar", not "RAVI KUMAR" or "ravi kumar")
- If a name has a roll number prefix like "01. Ravi Kumar", return only "Ravi Kumar"
- If you cannot find any names, return {{"names":[]}}
- Do not invent names — only extract what is present in the transcription"""


def _parse_names(raw: str):
    try:
        parsed = json.loads(_extract_json_obj(raw))
        return [str(n).strip() for n in (parsed.get("names") or []) if str(n).strip()]
    except Exception:
        return None


def _extract_mime(data_url: str) -> str:
    m = re.match(r"^data:image/([a-z]+);base64,", data_url)
    return m.group(1) if m else "jpeg"


def _strip_data_prefix(data_url: str) -> str:
    idx = data_url.find(",")
    return data_url[idx + 1:] if idx >= 0 else data_url


async def _scan_via_ocr_pipeline(image: str):
    try:
        mime = _extract_mime(image)
        raw_base64 = _strip_data_prefix(image)
        transcription = await run_ocr(raw_base64, instruction=SCAN_STUDENTS_OCR_INSTRUCTION, mime=mime, max_tokens=2048)
        if not transcription.strip():
            return None
        names_raw = await run_text(_build_names_prompt(transcription), temperature=0, max_tokens=1024)
        names = _parse_names(names_raw)
        return names if names else None
    except Exception as e:
        print(f"[scan-students] OCR pipeline failed, falling back to vision model: {e}")
        return None


async def _scan_via_vision_model(image: str):
    messages = [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": image}},
        {"type": "text", "text": SCAN_STUDENTS_PROMPT},
    ]}]
    raw = await call_ai(messages, {"json_mode": False, "temperature": 0.1, "timeout_s": 90})
    if not raw:
        return None
    return _parse_names(raw)


# POST /api/scan-students
@router.post("/scan-students")
async def scan_students(body: ScanStudentsSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        api_log("scan-students", ip, (time.time() - t0) * 1000, False, "rate_limited", user_id=user["id"])
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in an hour.")

    try:
        names = await _scan_via_ocr_pipeline(body.image)
        if names is None:
            names = await _scan_via_vision_model(body.image)

        if names is None:
            api_log("scan-students", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="Could not extract names")
            raise HTTPException(status_code=500, detail="Could not read names from image. Try a clearer photo.")

        api_log("scan-students", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"names": names}
    except HTTPException:
        raise
    except Exception as e:
        api_log("scan-students", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        raise HTTPException(status_code=500, detail="Unexpected server error. Please try again.")


# POST /api/scan-attendance
@router.post("/scan-attendance")
async def scan_attendance(body: ScanAttendanceSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        api_log("scan-attendance", ip, (time.time() - t0) * 1000, False, "rate_limited", user_id=user["id"])
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in an hour.")

    try:
        student_list = "\n".join(f"{i + 1}. {s.name} (roll: {s.rollNumber}, id: {s.id})" for i, s in enumerate(body.students))

        prompt = f"""You are reading a teacher's handwritten class attendance register from a photo.

Known students in this class:
{student_list}

For each student, work out whether they were marked present, absent, or late. Handwritten registers vary in convention:
- A tick/checkmark or "P" next to a name usually means present
- A cross, "A", or an empty attendance cell usually means absent
- "L" or a note about arriving late means late
- Some sheets only list the names of ABSENT students (no full roster) — in that case, mark only those listed names as absent

Return ONLY valid JSON (no markdown, no extra text):
{{
  "entries": [
    {{ "studentId": "...", "status": "present" }}
  ]
}}

Rules:
- status must be exactly one of: "present", "absent", "late"
- studentId must exactly match one of the ids listed above
- Match each handwritten name/roll number to the closest student in the list above, even if spelling or handwriting is imperfect
- Only include a student if you can reasonably tell their status from the image — omit anyone you're unsure about"""

        raw = await call_ai([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": body.imageBase64}},
                {"type": "text", "text": prompt},
            ],
        }], {"temperature": 0.2, "timeout_s": 90})

        try:
            parsed = json.loads(raw)
        except Exception:
            api_log("scan-attendance", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="JSON parse failed")
            return {"entries": []}

        valid_statuses = {"present", "absent", "late"}
        student_ids = {s.id for s in body.students}
        entries = [
            {"studentId": e["studentId"], "status": e["status"]}
            for e in (parsed.get("entries") or [])
            if isinstance(e, dict) and e.get("status") in valid_statuses and e.get("studentId") in student_ids
        ]

        api_log("scan-attendance", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"entries": entries}
    except Exception as e:
        msg = str(e)
        api_log("scan-attendance", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=msg)
        if msg.startswith("[ai]"):
            raise HTTPException(status_code=503, detail="AI service temporarily unavailable. Please retry shortly.")
        return {"entries": []}


# POST /api/upload-scan
@router.post("/upload-scan")
async def upload_scan(body: UploadScanSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    try:
        import base64
        mime_type = body.mimeType or "image/jpeg"
        filename = body.filename or "scan.jpg"
        buffer = base64.b64decode(body.imageBase64)
        unique_name = f"{int(time.time() * 1000)}_{filename}"

        ac = create_admin_client()
        try:
            ac.storage.from_("scanned-papers").upload(unique_name, buffer, {"content-type": mime_type, "upsert": "false"})
        except Exception as e:
            api_log("upload-scan", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
            raise HTTPException(status_code=500, detail=str(e))

        public_url = ac.storage.from_("scanned-papers").get_public_url(unique_name)

        drive = await upload_to_google_drive(body.imageBase64, unique_name, mime_type)

        api_log("upload-scan", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"url": public_url, "driveUrl": drive["url"] if drive else None}
    except HTTPException:
        raise
    except Exception as e:
        api_log("upload-scan", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# POST /api/grade-image
@router.post("/grade-image")
async def grade_image(body: GradeImageSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        api_log("grade-image", ip, (time.time() - t0) * 1000, False, "rate_limited", user_id=user["id"])
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in an hour.")

    try:
        student_list = "\n".join(f"{i + 1}. {s.name} (id: {s.id})" for i, s in enumerate(body.students))

        prompt = f"""You are helping a teacher grade student test papers.

Topic: {body.topic}
Total marks: {body.totalMarks}

The image shows a mark sheet or student answer paper. Extract the score for each student listed below.
Also add a short observation/feedback if anything is visible (e.g. "left Q3 blank", "calculation errors", "good work", "skipped last question"). Keep feedback under 10 words. If nothing notable, leave feedback as an empty string.

Students:
{student_list}

Return ONLY valid JSON:
{{
  "entries": [
    {{ "studentId": "...", "score": 0, "feedback": "" }}
  ]
}}

Rules:
- score must be a number between 0 and {body.totalMarks}
- Only include students whose score you can clearly read
- studentId must exactly match one of the ids above
- feedback is optional — empty string if nothing notable visible"""

        raw = await call_ai([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": body.imageBase64}},
                {"type": "text", "text": prompt},
            ],
        }], {"temperature": 0.2, "timeout_s": 90})

        try:
            parsed = json.loads(raw)
        except Exception:
            api_log("grade-image", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error="JSON parse failed")
            return {"entries": []}

        student_ids = {s.id for s in body.students}
        entries = [
            {"studentId": e["studentId"], "score": e["score"], "feedback": e.get("feedback", "").strip() if isinstance(e.get("feedback"), str) else ""}
            for e in (parsed.get("entries") or [])
            if isinstance(e, dict) and isinstance(e.get("score"), (int, float)) and 0 <= e["score"] <= body.totalMarks and e.get("studentId") in student_ids
        ]

        api_log("grade-image", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"entries": entries}
    except Exception as e:
        msg = str(e)
        api_log("grade-image", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=msg)
        if msg.startswith("[ai]"):
            raise HTTPException(status_code=503, detail="AI service temporarily unavailable. Please retry shortly.")
        return {"entries": []}


def _build_extract_grade_prompt(questions, total_marks, topic, student_name) -> tuple[str, set]:
    long_indices = {i for i, q in enumerate(questions) if not q.type or q.type == "long-answer"}

    q_lines = []
    for i, q in enumerate(questions):
        qtype = q.type or "long-answer"
        if qtype == "mcq":
            opts = "  ".join(q.options or [])
            q_lines.append(f"Q{i + 1} [MCQ, {q.marks}m]: {q.text}" + (f"\n   Options: {opts}" if opts else "") + "\n   → Extract the letter the student wrote.")
        elif qtype == "fill-in-blank":
            q_lines.append(f"Q{i + 1} [Fill in blank, {q.marks}m]: {q.text}\n   → Extract the exact word or phrase written in the blank.")
        elif qtype == "short-answer":
            q_lines.append(f"Q{i + 1} [Short answer, {q.marks}m]: {q.text}\n   → Extract the student's full written answer.")
        else:
            model_ans = f"\n   Model answer: {q.answer}" if q.answer else ""
            q_lines.append(
                f"Q{i + 1} [Long answer, {q.marks}m — EXTRACT AND GRADE]:{model_ans}\n   Question: {q.text}\n   "
                "→ Extract answer AND award marks (0–" + str(q.marks) + ") with brief feedback AND set errorType:\n"
                '     "conceptual" = student misunderstands the core idea\n'
                '     "procedural" = understands idea but wrong method or steps\n'
                '     "careless" = mostly correct, minor arithmetic or language slip\n'
                "     null = full marks"
            )

    prompt = (
        f"Grade the handwritten answer paper for student: {student_name}\n"
        f"Topic: {topic}  Total marks: {total_marks}\n\n"
        f"QUESTIONS:\n" + "\n\n".join(q_lines) + "\n\n"
        "RULES:\n"
        '- Extract what the student wrote for every question into "answers".\n'
        "- Do NOT grade MCQ, fill-in-blank, or short-answer — only extract text.\n"
        '- For long-answer questions, grade them in "longAnswerGrades".\n'
        '- Blank/unreadable → set text to "" in answers.\n\n'
        "Return ONLY valid JSON:\n"
        "{\n"
        '  "answers": [{ "questionIndex": 0, "text": "C" }, ...],\n'
        + (f'  "longAnswerGrades": [{{ "questionIndex": {next(iter(long_indices))}, "marksAwarded": 3, "feedback": "...", "errorType": "procedural" }}],\n'
           if long_indices else '  "longAnswerGrades": [],\n')
        + '  "generalFeedback": "One sentence summary"\n'
        "}"
    )
    return prompt, long_indices


async def _grade_breakdown(questions, answer_map, long_grade_map):
    breakdown = []
    for i, q in enumerate(questions):
        scanned = answer_map.get(i, "")
        qtype = q.type or "long-answer"
        max_marks = q.marks or 0
        error_type = None

        if qtype == "mcq":
            g = grade_mcq(scanned, q.answer or "", max_marks)
            marks_awarded, feedback = g["marksAwarded"], g["feedback"]
        elif qtype == "fill-in-blank":
            g = grade_fib(scanned, q.answer or "", max_marks)
            marks_awarded, feedback = g["marksAwarded"], g["feedback"]
        elif qtype == "short-answer":
            g = grade_short_answer(scanned, q.keywords or [], max_marks)
            marks_awarded, feedback = g["marksAwarded"], g["feedback"]
            if marks_awarded < max_marks and scanned.strip():
                error_type = "conceptual" if marks_awarded == 0 else "procedural"
        else:
            llm_grade = long_grade_map.get(i)

            def _live():
                return {
                    "marksAwarded": max(0, min(llm_grade.get("marksAwarded", 0), max_marks)) if llm_grade else 0,
                    "feedback": (llm_grade.get("feedback") if llm_grade else None) or ("Graded by AI" if scanned else "No answer written"),
                    "errorType": (llm_grade.get("errorType") if llm_grade else None),
                }

            graded = await get_consistent_long_answer_grade(q.text, q.answer, scanned, max_marks, _live)
            marks_awarded, feedback = graded["marksAwarded"], graded["feedback"]
            error_type = graded.get("errorType") if marks_awarded < max_marks else None

        breakdown.append({"questionIndex": i, "marksAwarded": marks_awarded, "maxMarks": max_marks, "feedback": feedback, "errorType": error_type})
    return breakdown


# POST /api/grade-paper
@router.post("/grade-paper")
async def grade_paper(body: GradePaperSchema2, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        api_log("grade-paper", ip, (time.time() - t0) * 1000, False, "rate_limited", user_id=user["id"])
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in an hour.")

    try:
        prompt, _long_indices = _build_extract_grade_prompt(body.questions, body.totalMarks, body.topic, body.studentName)

        raw = await call_ai([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": body.imageBase64}},
                {"type": "text", "text": prompt},
            ],
        }], {"temperature": 0.1, "timeout_s": 90})

        if not raw:
            raise HTTPException(status_code=500, detail="AI returned empty response")

        try:
            result = json.loads(_extract_json_obj(raw))
        except Exception:
            raise HTTPException(status_code=500, detail="Could not parse AI response")

        answer_map = {a["questionIndex"]: a.get("text", "") for a in (result.get("answers") or [])}
        long_grade_map = {g["questionIndex"]: g for g in (result.get("longAnswerGrades") or [])}

        breakdown = await _grade_breakdown(body.questions, answer_map, long_grade_map)
        total_score = min(sum(b["marksAwarded"] for b in breakdown), body.totalMarks)

        api_log("grade-paper", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {"totalScore": total_score, "breakdown": breakdown, "generalFeedback": result.get("generalFeedback") or ""}
    except HTTPException:
        raise
    except Exception as e:
        api_log("grade-paper", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        raise HTTPException(status_code=500, detail="Unexpected server error")


def _find_closest_student(ai_name, students):
    if not ai_name:
        return None
    needle = ai_name.lower().strip()
    for s in students:
        if s.name.lower() == needle:
            return s
    for s in students:
        if needle in s.name.lower() or s.name.lower() in needle:
            return s
    return None


# POST /api/grade-scan
@router.post("/grade-scan")
async def grade_scan(body: GradeScanSchema2, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    t0 = time.time()
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        api_log("grade-scan", ip, (time.time() - t0) * 1000, False, "rate_limited", user_id=user["id"])
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again in an hour.")

    if not body.studentId and not body.students:
        raise HTTPException(status_code=400, detail="Provide studentId or students array")

    is_pre_selected = bool(body.studentId)
    prompt = _build_grade_scan_prompt(body.questions, body.totalMarks, body.topic, body.subject, body.studentName, is_pre_selected)

    try:
        raw = await call_ai([{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": f"data:{body.mimeType or 'image/jpeg'};base64,{body.imageBase64}"}},
                {"type": "text", "text": prompt},
            ],
        }], {"temperature": 0, "timeout_s": 90})
    except Exception as e:
        api_log("grade-scan", ip, (time.time() - t0) * 1000, False, "error", user_id=user["id"], error=str(e))
        raise HTTPException(status_code=502, detail="Failed to reach grading service")

    result = _parse_extraction_result(raw)

    answer_map = {a["questionIndex"]: a.get("text", "") for a in (result.get("answers") or [])}
    long_grade_map = {g["questionIndex"]: g for g in (result.get("longAnswerGrades") or [])}

    breakdown_raw = await _grade_breakdown(body.questions, answer_map, long_grade_map)
    breakdown = [{"question": b["questionIndex"] + 1, "awarded": b["marksAwarded"], "max": b["maxMarks"], "note": b["feedback"], "errorType": b["errorType"]} for b in breakdown_raw]

    score = min(sum(b["awarded"] for b in breakdown), body.totalMarks)
    extracted_answers = [answer_map.get(i, "") for i in range(len(body.questions))]
    review = assess_paper_confidence(breakdown, extracted_answers)

    if is_pre_selected:
        api_log("grade-scan", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
        return {
            "studentId": body.studentId,
            "studentName": body.studentName,
            "score": score,
            "confidence": "high",
            "breakdown": breakdown,
            "feedback": result.get("generalFeedback"),
            "needsReview": review["needsReview"],
            "reviewReason": review["reviewReason"],
        }

    ai_name = result.get("studentName") if isinstance(result.get("studentName"), str) else None
    matched = _find_closest_student(ai_name, body.students or [])
    api_log("grade-scan", ip, (time.time() - t0) * 1000, False, "ok", user_id=user["id"])
    return {
        "matchedStudent": matched.model_dump() if matched else None,
        "score": score,
        "breakdown": breakdown,
        "feedback": result.get("generalFeedback"),
        "needsReview": review["needsReview"],
        "reviewReason": review["reviewReason"],
    }


def _build_grade_scan_prompt(questions, total_marks, topic, subject, student_name, is_pre_selected) -> str:
    long_indices = {i for i, q in enumerate(questions) if not q.type or q.type == "long-answer"}

    q_lines = []
    for i, q in enumerate(questions):
        qtype = q.type or "long-answer"
        if qtype == "mcq":
            opts = "  ".join(q.options or [])
            q_lines.append(f"Q{i + 1} [MCQ, {q.marks}m]: {q.text}" + (f"\n   Options: {opts}" if opts else "") + "\n   → Extract the letter the student wrote or circled.")
        elif qtype == "fill-in-blank":
            q_lines.append(f"Q{i + 1} [Fill in blank, {q.marks}m]: {q.text}\n   → Extract the exact word or phrase the student wrote in the blank.")
        elif qtype == "short-answer":
            q_lines.append(f"Q{i + 1} [Short answer, {q.marks}m]: {q.text}\n   → Extract the student's full written answer.")
        else:
            model_ans = f"\n   Model answer: {q.answer}" if q.answer else ""
            q_lines.append(
                f"Q{i + 1} [Long answer, {q.marks}m — EXTRACT AND GRADE THIS]:{model_ans}\n   Question: {q.text}\n   "
                "→ Extract answer AND award marks (0–" + str(q.marks) + ") with brief feedback AND set errorType:\n"
                '     "conceptual" = student misunderstands the core idea\n'
                '     "procedural" = understands idea but wrong method or steps\n'
                '     "careless" = mostly correct, minor arithmetic or language slip\n'
                "     null = full marks"
            )

    has_long = len(long_indices) > 0
    student_line = f"Student: {student_name}\n" if is_pre_selected and student_name else ""

    return (
        "You are grading a handwritten school exam paper.\n"
        f"{student_line}Subject: {subject}  Topic: {topic}  Total marks: {total_marks}\n\n"
        f"QUESTIONS:\n" + "\n\n".join(q_lines) + "\n\n"
        "TASK:\n"
        + ("" if is_pre_selected else "1. Read the student's name from the top of the paper.\n")
        + f"{'1' if is_pre_selected else '2'}. For every question, find what the student wrote and return it in \"answers\".\n"
        + (f"{'2' if is_pre_selected else '3'}. For long-answer questions, award marks and write feedback in \"longAnswerGrades\".\n" if has_long else "")
        + "\nRules:\n"
        '- If a question is blank or unreadable, set text to "" in answers.\n'
        "- Keep feedback under 10 words.\n"
        "- Do NOT grade MCQ, fill-in-blank, or short-answer questions — only extract their text.\n\n"
        "Return ONLY valid JSON — no markdown, no extra text:\n"
        "{\n"
        + ("" if is_pre_selected else '  "studentName": "name from paper or null",\n')
        + '  "answers": [\n'
        '    { "questionIndex": 0, "text": "C" },\n'
        '    { "questionIndex": 1, "text": "the student wrote here" }\n'
        "  ],\n"
        + (f'  "longAnswerGrades": [\n    {{ "questionIndex": {next(iter(long_indices))}, "marksAwarded": 3, "feedback": "Good attempt", "errorType": "procedural" }}\n  ],\n'
           if has_long else '  "longAnswerGrades": [],\n')
        + '  "generalFeedback": "One sentence summary"\n'
        "}"
    )


def _parse_extraction_result(raw: str) -> dict:
    cleaned = re.sub(r"\s*```$", "", re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.I), flags=re.I).strip()
    try:
        parsed = json.loads(cleaned)
    except Exception:
        match = re.search(r"\{[\s\S]*\}", cleaned)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except Exception:
            return {}
    return parsed if isinstance(parsed, dict) else {}
