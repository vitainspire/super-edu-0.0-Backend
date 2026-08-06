import time
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_auth_rate_limit
from ..lib.scanner_auth import sign_school_token, get_scanner_school_id, verify_test_in_school, verify_worksheet_in_school
from ..deps import require_user

router = APIRouter()


def _maybe_single(query):
    """supabase-py's maybe_single().execute() returns None itself (not an
    object with .data = None) when zero rows match — mirrors the guard
    scanner_auth.py's verify_test_in_school already applies for the same reason."""
    res = query.execute()
    return res.data if res else None


def _bearer_user_present(request: Request) -> bool:
    """True if the request carries a valid Supabase Auth session (teacher or
    scanner-staff account) — the same identity check /save-score and
    /upload-scan rely on via require_user, reimplemented here since these
    routes also need to accept the anonymous school-token flow as an
    alternative (mirrors ai_routes2.py's get_worksheet_marks dual-auth)."""
    authorization = request.headers.get("authorization")
    if not (authorization and authorization.startswith("Bearer ")):
        return False
    from ..lib.supabase_clients import get_anon_client
    token = authorization[len("Bearer "):].strip()
    try:
        res = get_anon_client().auth.get_user(token)
    except Exception:
        return False
    return bool(res and res.user)


class ConnectBody(BaseModel):
    joinCode: str


# POST /api/scanner/connect
@router.post("/connect")
def connect(body: ConnectBody, request: Request):
    ip = get_client_ip(request)
    t0 = time.time()

    allowed, _ = check_auth_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    join_code = body.joinCode.strip().upper()
    if not join_code:
        raise HTTPException(status_code=400, detail="School code required")

    try:
        admin = create_admin_client()
        res = admin.table("schools").select("id, name").eq("join_code", join_code).maybe_single().execute()
        school = res.data if res else None

        if not school:
            api_log("scanner/connect", ip, (time.time() - t0) * 1000, False, "unauthorized")
            raise HTTPException(status_code=404, detail="School code not found. Ask your school admin for the correct code.")

        api_log("scanner/connect", ip, (time.time() - t0) * 1000, False, "ok", user_id=school["id"])
        return {"schoolId": school["id"], "schoolName": school.get("name") or "", "token": sign_school_token(school["id"])}
    except HTTPException:
        raise
    except Exception as e:
        api_log("scanner/connect", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Server error")


# GET /api/scanner/profile — scanner STAFF with a real Supabase account
# (deliberately NOT the join-code token flow above).
@router.get("/profile")
def profile(user: dict = Depends(require_user)):
    try:
        ac = create_admin_client()
        res = ac.table("scanner_profiles").select("id, name, email, school_id, schools(name)").eq("user_id", user["id"]).maybe_single().execute()
        data = res.data if res else None

        if not data:
            raise HTTPException(status_code=403, detail="No scanner account found. Ask your school admin to create your account.")

        schools = data.get("schools") or {}
        return {
            "id": data["id"], "name": data["name"], "email": data["email"],
            "schoolId": data["school_id"], "schoolName": schools.get("name") or "",
        }
    except HTTPException:
        raise
    except Exception as e:
        print(f"[scanner/profile] failed: {e}")
        raise HTTPException(status_code=500, detail="Server error")


# ─── Read-only listings (replace scanner-portal's former direct Supabase reads) ───
# Every route below accepts EITHER a Supabase Auth bearer session (teacher or
# scanner-staff account — no extra ownership check, matching /save-score's
# existing looseness) OR the anonymous school join-code token (ownership
# verified per-resource, matching /multi-grade-scan's existing checks).

# GET /api/scanner/overview — connect page's class/test/worksheet listing.
# Scanner-token only: this page is reached before any Supabase session exists.
@router.get("/overview")
def scanner_overview(request: Request):
    school_id = get_scanner_school_id(request)
    if not school_id:
        raise HTTPException(status_code=401, detail="Not authenticated")

    ac = create_admin_client()
    classes = ac.table("classes").select("id, name, grade, section").eq("school_id", school_id).execute().data or []
    class_ids = [c["id"] for c in classes]
    if not class_ids:
        return {"classes": [], "tests": [], "worksheets": [], "studentCounts": {}, "marksCounts": {}, "worksheetMarksCounts": {}}

    tests = ac.table("tests").select("id, topic, total_marks, conducted_on, class_id, subject, term") \
        .in_("class_id", class_ids).order("conducted_on", desc=True).execute().data or []
    worksheets = ac.table("worksheets").select("id, topic, total_marks, subject, class_id, created_at") \
        .in_("class_id", class_ids).order("created_at", desc=True).execute().data or []

    students = ac.table("students").select("id, class_id").in_("class_id", class_ids).eq("is_active", True).execute().data or []
    student_counts: dict = {}
    for s in students:
        student_counts[s["class_id"]] = student_counts.get(s["class_id"], 0) + 1

    test_ids = [t["id"] for t in tests]
    marks_counts: dict = {}
    if test_ids:
        marks = ac.table("marks").select("test_id").in_("test_id", test_ids).execute().data or []
        for m in marks:
            marks_counts[m["test_id"]] = marks_counts.get(m["test_id"], 0) + 1

    ws_ids = [w["id"] for w in worksheets]
    ws_marks_counts: dict = {}
    if ws_ids:
        ws_marks = ac.table("worksheet_marks").select("worksheet_id").in_("worksheet_id", ws_ids).execute().data or []
        for m in ws_marks:
            ws_marks_counts[m["worksheet_id"]] = ws_marks_counts.get(m["worksheet_id"], 0) + 1

    return {
        "classes": classes, "tests": tests, "worksheets": worksheets,
        "studentCounts": student_counts, "marksCounts": marks_counts, "worksheetMarksCounts": ws_marks_counts,
    }


# GET /api/scanner/classes/{class_id}/tests — the class-scoped test list.
@router.get("/classes/{class_id}/tests")
def scanner_class_tests(class_id: str, request: Request):
    ac = create_admin_client()
    if not _bearer_user_present(request):
        school_id = get_scanner_school_id(request)
        if not school_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        cls = _maybe_single(ac.table("classes").select("school_id").eq("id", class_id).maybe_single())
        if not cls or cls.get("school_id") != school_id:
            raise HTTPException(status_code=403, detail="Forbidden")

    cls_info = _maybe_single(ac.table("classes").select("grade, section, name").eq("id", class_id).maybe_single())
    if not cls_info:
        raise HTTPException(status_code=404, detail="Class not found")

    tests = ac.table("tests").select("id, topic, total_marks, conducted_on, subject, term") \
        .eq("class_id", class_id).order("conducted_on", desc=True).execute().data or []
    total_students = len(ac.table("students").select("id").eq("class_id", class_id).eq("is_active", True).execute().data or [])

    test_ids = [t["id"] for t in tests]
    marks_counts: dict = {}
    if test_ids:
        marks = ac.table("marks").select("test_id").in_("test_id", test_ids).execute().data or []
        for m in marks:
            marks_counts[m["test_id"]] = marks_counts.get(m["test_id"], 0) + 1

    return {"class": cls_info, "tests": tests, "totalStudents": total_students, "marksCounts": marks_counts}


# GET /api/scanner/tests/{test_id} — test detail + roster + existing marks.
@router.get("/tests/{test_id}")
async def scanner_test_detail(test_id: str, request: Request):
    ac = create_admin_client()
    test = _maybe_single(ac.table("tests").select("id, topic, total_marks, subject, term, conducted_on, questions, class_id") \
        .eq("id", test_id).maybe_single())
    if not test:
        raise HTTPException(status_code=404, detail="Test not found")

    if not _bearer_user_present(request):
        school_id = get_scanner_school_id(request)
        if not school_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not await verify_test_in_school(school_id, test_id):
            raise HTTPException(status_code=403, detail="Forbidden")

    class_id = test.get("class_id")
    cls_info = _maybe_single(ac.table("classes").select("grade, section, name").eq("id", class_id).maybe_single()) if class_id else None
    students = ac.table("students").select("id, name, roll_number").eq("class_id", class_id).eq("is_active", True) \
        .order("roll_number").execute().data if class_id else []
    marks = ac.table("marks").select("student_id, score, source").eq("test_id", test_id).execute().data or []

    return {"test": test, "class": cls_info, "students": students, "marks": marks}


# GET /api/scanner/worksheets/{worksheet_id} — worksheet detail + roster + done students.
@router.get("/worksheets/{worksheet_id}")
async def scanner_worksheet_detail(worksheet_id: str, request: Request):
    ac = create_admin_client()
    ws = _maybe_single(ac.table("worksheets").select("id, topic, subject, grade, total_marks, sections, class_id") \
        .eq("id", worksheet_id).maybe_single())
    if not ws:
        raise HTTPException(status_code=404, detail="Worksheet not found")

    if not _bearer_user_present(request):
        school_id = get_scanner_school_id(request)
        if not school_id:
            raise HTTPException(status_code=401, detail="Not authenticated")
        if not await verify_worksheet_in_school(school_id, worksheet_id):
            raise HTTPException(status_code=403, detail="Forbidden")

    class_id = ws.get("class_id")
    students = ac.table("students").select("id, name, roll_number").eq("class_id", class_id).eq("is_active", True) \
        .order("roll_number").execute().data if class_id else []
    ws_marks = ac.table("worksheet_marks").select("student_id").eq("worksheet_id", worksheet_id).execute().data or []

    return {"worksheet": ws, "students": students, "doneStudentIds": [m["student_id"] for m in ws_marks]}
