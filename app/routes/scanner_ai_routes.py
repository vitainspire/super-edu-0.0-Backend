import time
import uuid
from typing import Optional
from fastapi import APIRouter, Request, HTTPException, Query, Depends

from ..lib.ai import call_ai
from ..lib.google_drive_upload import upload_to_google_drive
from ..lib.graders import grade_mcq, grade_fib, grade_short_answer
from ..lib.grading_cache import get_consistent_long_answer_grade
from ..lib.grade_review import assess_paper_confidence
from ..lib.schemas import MultiGradeScanSchema, ScannerUploadSchema, ScannerSaveScoreSchema, WorksheetMarksUpsertSchema, WorksheetSaveScoreSchema
from ..lib.logger import get_client_ip
from ..lib.rate_limit import check_rate_limit, check_vision_rate_limit
from ..lib.supabase_clients import create_admin_client
from ..lib.scanner_auth import get_scanner_school_id, verify_test_in_school, verify_worksheet_in_school
from ..deps import require_user

router = APIRouter()


def _build_prompt(questions, total_marks, topic, subject, student_name, page_count) -> tuple[str, set]:
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
                '     "careless" = mostly correct, minor slip\n'
                "     null = full marks"
            )

    has_long = len(long_indices) > 0
    page_note = f"The {page_count} images above are all pages of the SAME student's answer sheet. Treat them as one paper.\n" if page_count > 1 else ""

    return (
        "You are grading a handwritten school exam paper.\n"
        f"{page_note}Student: {student_name}\nSubject: {subject}  Topic: {topic}  Total marks: {total_marks}\n\n"
        "QUESTIONS:\n" + "\n\n".join(q_lines) + "\n\n"
        "TASK:\n"
        '1. For every question, find what the student wrote and return it in "answers".\n'
        + ('2. For long-answer questions, award marks and write feedback in "longAnswerGrades".\n' if has_long else "")
        + "\nRules:\n"
        '- If a question is blank or unreadable, set text to "" in answers.\n'
        "- Keep feedback under 10 words.\n"
        "- Do NOT grade MCQ, fill-in-blank, or short-answer questions — only extract their text.\n\n"
        "Return ONLY valid JSON — no markdown, no extra text:\n"
        "{\n"
        '  "answers": [\n'
        '    { "questionIndex": 0, "text": "student wrote here" }\n'
        "  ],\n"
        + (f'  "longAnswerGrades": [\n    {{ "questionIndex": {next(iter(long_indices))}, "marksAwarded": 3, "feedback": "Good attempt", "errorType": "procedural" }}\n  ],\n'
           if has_long else '  "longAnswerGrades": [],\n')
        + '  "generalFeedback": "One sentence summary"\n'
        "}"
    ), long_indices


def _parse_extraction_result(raw: str) -> dict:
    import re, json
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

        breakdown.append({"question": i + 1, "awarded": marks_awarded, "max": max_marks, "note": feedback, "errorType": error_type})
    return breakdown


# POST /api/multi-grade-scan (scanner-token auth, public per middleware)
@router.post("/multi-grade-scan")
async def multi_grade_scan(body: MultiGradeScanSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded. Try again later.")

    school_id = get_scanner_school_id(request)
    if not school_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not body.testId and not body.worksheetId:
        raise HTTPException(status_code=400, detail="testId or worksheetId is required")

    owns = await verify_test_in_school(school_id, body.testId) if body.testId else await verify_worksheet_in_school(school_id, body.worksheetId)
    if not owns:
        raise HTTPException(status_code=403, detail="Not authorized for this test or worksheet")

    prompt, _long_indices = _build_prompt(body.questions, body.totalMarks, body.topic, body.subject, body.studentName, len(body.images))
    image_blocks = [{"type": "image_url", "image_url": {"url": img}} for img in body.images]

    try:
        raw = await call_ai([{"role": "user", "content": [*image_blocks, {"type": "text", "text": prompt}]}], {"temperature": 0, "timeout_s": 90})
    except Exception:
        raise HTTPException(status_code=502, detail="Failed to reach grading service")

    result = _parse_extraction_result(raw)
    answer_map = {a["questionIndex"]: a.get("text", "") for a in (result.get("answers") or [])}
    long_grade_map = {g["questionIndex"]: g for g in (result.get("longAnswerGrades") or [])}

    breakdown = await _grade_breakdown(body.questions, answer_map, long_grade_map)
    score = min(sum(b["awarded"] for b in breakdown), body.totalMarks)
    extracted_answers = [answer_map.get(i, "") for i in range(len(body.questions))]
    review = assess_paper_confidence(breakdown, extracted_answers)

    return {
        "studentId": body.studentId,
        "studentName": body.studentName,
        "score": score,
        "needsReview": review["needsReview"],
        "reviewReason": review["reviewReason"],
        "breakdown": breakdown,
        "feedback": result.get("generalFeedback"),
    }


# POST /api/scanner-upload (scanner-token auth, public per middleware)
@router.post("/scanner-upload")
async def scanner_upload(body: ScannerUploadSchema, request: Request):
    import base64
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    school_id = get_scanner_school_id(request)
    if not school_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if not body.testId and not body.worksheetId:
        raise HTTPException(status_code=400, detail="Missing required fields")

    comma_idx = body.imageDataUrl.find(",")
    if comma_idx == -1:
        raise HTTPException(status_code=400, detail="Invalid data URL")
    base64_data = body.imageDataUrl[comma_idx + 1:]

    if body.testId:
        if not await verify_test_in_school(school_id, body.testId):
            raise HTTPException(status_code=403, detail="Forbidden")
    else:
        if not await verify_worksheet_in_school(school_id, body.worksheetId):
            raise HTTPException(status_code=403, detail="Forbidden")

    ac = create_admin_client()
    buffer = base64.b64decode(base64_data)
    folder = f"scanner/{body.testId[:8]}" if body.testId else f"worksheets/{body.worksheetId[:8]}"
    filename = f"{folder}/{body.studentId[:8]}_{int(time.time() * 1000)}.jpg"

    try:
        ac.storage.from_("scanned-papers").upload(filename, buffer, {"content-type": "image/jpeg", "upsert": "true"})
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    public_url = ac.storage.from_("scanned-papers").get_public_url(filename)
    drive = await upload_to_google_drive(base64_data, filename.split("/")[-1], "image/jpeg")

    return {"url": public_url, "driveUrl": drive["url"] if drive else None}


# POST /api/scanner-save-score (scanner-token auth, public per middleware)
@router.post("/scanner-save-score")
async def scanner_save_score(body: ScannerSaveScoreSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    school_id = get_scanner_school_id(request)
    if not school_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if body.score < 0 or body.score > body.totalMarks:
        raise HTTPException(status_code=400, detail="score out of range")

    if not await verify_test_in_school(school_id, body.testId):
        raise HTTPException(status_code=403, detail="Forbidden")

    ac = create_admin_client()
    score_value = int(body.score) if body.score == int(body.score) else body.score
    try:
        ac.table("marks").upsert(
            {
                "id": str(uuid.uuid4()),
                "test_id": body.testId,
                "student_id": body.studentId,
                "score": score_value,
                "entered_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "source": "ai_scanned",
                "breakdown": body.breakdown,
                "feedback": body.feedback,
                "image_url": body.imageUrl,
                "drive_url": body.driveUrl,
            },
            on_conflict="student_id,test_id",
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True}


# GET /api/worksheet-marks?worksheetId=... (teacher session OR scanner token)
@router.get("/worksheet-marks")
async def get_worksheet_marks(request: Request, worksheetId: Optional[str] = Query(default=None)):
    if not worksheetId:
        raise HTTPException(status_code=400, detail="Missing worksheetId")

    authorization = request.headers.get("authorization") if request else None
    ac = create_admin_client()

    if authorization and authorization.startswith("Bearer "):
        from ..lib.supabase_clients import get_anon_client
        token = authorization[len("Bearer "):].strip()
        try:
            res = get_anon_client().auth.get_user(token)
        except Exception:
            res = None
        if res and res.user:
            data = ac.table("worksheet_marks").select("*").eq("worksheet_id", worksheetId).execute()
            return {"marks": data.data or []}

    school_id = get_scanner_school_id(request)
    if not school_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    if not await verify_worksheet_in_school(school_id, worksheetId):
        raise HTTPException(status_code=403, detail="Forbidden")

    data = ac.table("worksheet_marks").select("*").eq("worksheet_id", worksheetId).execute()
    return {"marks": data.data or []}


# POST /api/worksheet-marks (teacher portal, auth required)
@router.post("/worksheet-marks")
async def upsert_worksheet_marks(body: WorksheetMarksUpsertSchema, request: Request, user: dict = Depends(require_user)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    ac = create_admin_client()
    rows = [
        {
            "id": str(uuid.uuid4()),
            "worksheet_id": body.worksheetId,
            "student_id": e.studentId,
            "score": int(e.score) if e.score == int(e.score) else e.score,
            "feedback": e.feedback,
            "source": e.source or "manual",
            "entered_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
        }
        for e in body.entries
    ]

    try:
        ac.table("worksheet_marks").upsert(rows, on_conflict="worksheet_id,student_id").execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True}


# POST /api/worksheet-save-score (scanner-token auth, public per middleware)
@router.post("/worksheet-save-score")
async def worksheet_save_score(body: WorksheetSaveScoreSchema, request: Request):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    school_id = get_scanner_school_id(request)
    if not school_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    if body.score < 0 or body.score > body.totalMarks:
        raise HTTPException(status_code=400, detail="score out of range")

    if not await verify_worksheet_in_school(school_id, body.worksheetId):
        raise HTTPException(status_code=403, detail="Forbidden")

    ac = create_admin_client()
    score_value = int(body.score) if body.score == int(body.score) else body.score
    try:
        ac.table("worksheet_marks").upsert(
            {
                "id": str(uuid.uuid4()),
                "worksheet_id": body.worksheetId,
                "student_id": body.studentId,
                "score": score_value,
                "entered_at": time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime()),
                "source": "ai_scanned",
                "breakdown": body.breakdown,
                "feedback": body.feedback,
                "image_url": body.imageUrl,
                "drive_url": body.driveUrl,
            },
            on_conflict="worksheet_id,student_id",
        ).execute()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    return {"success": True}
