"""Mirrors backend/src/lib/student-auth.ts. Signatures are HMAC-SHA256 hex
digests — format-compatible with the TypeScript version (same secret env var,
same algorithm), even though nothing currently needs cross-language token
verification."""
import hmac
import hashlib
import os
from typing import Optional
from .supabase_clients import create_admin_client

SECRET = os.environ.get("STUDENT_SESSION_SECRET") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""


def _sign(student_id: str) -> str:
    return hmac.new(SECRET.encode(), student_id.encode(), hashlib.sha256).hexdigest()


def sign_student_id(student_id: str) -> str:
    return f"{student_id}.{_sign(student_id)}"


def verify_student_cookie(raw: Optional[str]) -> Optional[str]:
    """Verifies the signed token's signature and returns the authenticated student id, or None."""
    if not raw:
        return None
    idx = raw.rfind(".")
    if idx == -1:
        return None
    student_id = raw[:idx]
    sig = raw[idx + 1 :]
    expected = _sign(student_id)
    if not hmac.compare_digest(sig, expected):
        return None
    return student_id


async def verify_student_access(cookie_student_id: str, requested_student_id: str, requested_class_id: str) -> bool:
    """A student can have more than one `students` row — confirms a (studentId,
    classId) pair requested by the client actually belongs to the same physical
    student as the authenticated token, before any row-level data is returned."""
    if requested_student_id == cookie_student_id:
        return True

    supabase = create_admin_client()
    me_res = supabase.table("students").select("*, classes(*)").eq("id", cookie_student_id).maybe_single().execute()
    target_res = supabase.table("students").select("*, classes(*)").eq("id", requested_student_id).maybe_single().execute()
    me = me_res.data if me_res else None
    target = target_res.data if target_res else None
    if not me or not target or not target.get("is_active"):
        return False
    if target.get("class_id") != requested_class_id:
        return False

    my_class = me.get("classes")
    target_class = target.get("classes")
    if not my_class or not target_class:
        return False

    same_school = (
        my_class.get("school_id") == target_class.get("school_id")
        if my_class.get("school_id")
        else my_class.get("school_name") == target_class.get("school_name")
    )
    return (
        target.get("roll_number") == me.get("roll_number")
        and target_class.get("grade") == my_class.get("grade")
        and (target_class.get("section") or "") == (my_class.get("section") or "")
        and same_school
    )
