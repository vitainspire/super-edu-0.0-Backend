"""Mirrors backend/src/lib/scanner-auth.ts."""
import hmac
import hashlib
import os
from typing import Optional
from fastapi import Request
from .supabase_clients import create_admin_client

SECRET = os.environ.get("SCANNER_SESSION_SECRET") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""


def _sign(school_id: str) -> str:
    return hmac.new(SECRET.encode(), school_id.encode(), hashlib.sha256).hexdigest()


def sign_school_token(school_id: str) -> str:
    return f"{school_id}.{_sign(school_id)}"


def verify_school_token(raw: Optional[str]) -> Optional[str]:
    if not raw:
        return None
    idx = raw.rfind(".")
    if idx == -1:
        return None
    school_id = raw[:idx]
    sig = raw[idx + 1 :]
    expected = _sign(school_id)
    if not hmac.compare_digest(sig, expected):
        return None
    return school_id


def get_scanner_school_id(req: Request) -> Optional[str]:
    return verify_school_token(req.headers.get("x-scanner-token"))


async def verify_test_in_school(school_id: str, test_id: str) -> bool:
    admin = create_admin_client()
    test_res = admin.table("tests").select("class_id").eq("id", test_id).maybe_single().execute()
    test = test_res.data if test_res else None
    if not test or not test.get("class_id"):
        return False
    cls_res = admin.table("classes").select("school_id").eq("id", test["class_id"]).maybe_single().execute()
    cls = cls_res.data if cls_res else None
    return bool(cls) and cls.get("school_id") == school_id


async def verify_worksheet_in_school(school_id: str, worksheet_id: str) -> bool:
    admin = create_admin_client()
    ws_res = admin.table("worksheets").select("class_id").eq("id", worksheet_id).maybe_single().execute()
    ws = ws_res.data if ws_res else None
    if not ws or not ws.get("class_id"):
        return False
    cls_res = admin.table("classes").select("school_id").eq("id", ws["class_id"]).maybe_single().execute()
    cls = cls_res.data if cls_res else None
    return bool(cls) and cls.get("school_id") == school_id
