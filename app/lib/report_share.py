"""Stateless, signed parent-report share links — same HMAC-token convention
as scanner_auth.py and student_auth.py, with an expiry baked into the signed
payload instead of a database row. No new table: the token itself is the only
state, so a link can never outlive its signature without a code change."""
import hmac
import hashlib
import os
import time
from typing import Optional

SECRET = os.environ.get("REPORT_SHARE_SECRET") or os.environ.get("SUPABASE_SERVICE_ROLE_KEY") or ""

THIRTY_DAYS_SECONDS = 30 * 24 * 60 * 60


def _sign(student_id: str, expires_at: int) -> str:
    return hmac.new(SECRET.encode(), f"{student_id}:{expires_at}".encode(), hashlib.sha256).hexdigest()


def sign_report_token(student_id: str, expires_at: int) -> str:
    return f"{student_id}.{expires_at}.{_sign(student_id, expires_at)}"


def verify_report_token(raw: Optional[str]) -> Optional[str]:
    """Verifies signature and expiry, returning the student id or None."""
    if not raw:
        return None
    parts = raw.split(".")
    if len(parts) != 3:
        return None
    student_id, expires_at_raw, sig = parts
    try:
        expires_at = int(expires_at_raw)
    except ValueError:
        return None
    if time.time() > expires_at:
        return None
    expected = _sign(student_id, expires_at)
    if not hmac.compare_digest(sig, expected):
        return None
    return student_id
