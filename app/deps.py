"""FastAPI dependencies mirroring backend/src/middleware/auth.ts's Express
middlewares. FastAPI's Depends() chain plays the same role — declare a
dependency, it runs before the route body, raising HTTPException to short-circuit
(the FastAPI equivalent of Express calling res.status(...).json(...) and
returning without next())."""
from typing import Optional
from fastapi import Header, HTTPException, Depends
from supabase_auth.errors import AuthApiError

from .lib.supabase_clients import get_anon_client, reset_anon_client, create_admin_client
from .lib.admin_queries import fetch_admin
from .lib.student_auth import verify_student_cookie
from .lib.scanner_auth import verify_school_token


def _bearer_token(authorization: Optional[str]) -> Optional[str]:
    if not authorization or not authorization.startswith("Bearer "):
        return None
    token = authorization[len("Bearer ") :].strip()
    return token or None


def _get_user(token: str, attempts: int = 3):
    """Like admin_auth.py's _retry_auth: a dropped connection to Supabase Auth
    (SSL EOF, WinError 10060, ...) is retried, while a real rejection
    (AuthApiError -- expired/invalid token) is never retried, since retrying
    it just reproduces the same verdict. Every /api/teacher/* and
    /api/admin/* route runs through this on every request, so an unretried
    transient error here used to surface as a blanket 401 across the whole
    app rather than the one call that actually failed.

    THREE ATTEMPTS, NOT TWO -- same reasoning as retry_supabase's own bump:
    measured for real, a single retry still wasn't enough. A genuinely dead
    session still ends in a real 401 one attempt later; this only costs
    anything when the connection recovers partway through, which is exactly
    the case worth paying for."""
    last_exc: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return get_anon_client().auth.get_user(token)
        except AuthApiError:
            raise
        except Exception as e:
            last_exc = e
            print(f"[deps] transient auth failure (attempt {attempt}/{attempts}): {type(e).__name__}: {e}")
            if attempt < attempts:
                reset_anon_client()
    raise last_exc


def require_user(authorization: Optional[str] = Header(None)) -> dict:
    """Replaces the Next.js/Express bearer-token check — verifies the Supabase
    access token the frontend attaches on every call."""
    token = _bearer_token(authorization)
    if not token:
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        res = _get_user(token)
    except Exception as e:
        print(f"[deps] require_user failed: {type(e).__name__}: {e}")
        raise HTTPException(status_code=401, detail="Unauthorized")

    user = res.user if res else None
    if not user:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return {"id": user.id, "email": user.email}


def require_admin(schoolId: str, user: dict = Depends(require_user)) -> dict:
    """Resolves the admin row for this user and confirms the path's :schoolId
    matches — same check every admin route did inline in the Next.js version."""
    ac = create_admin_client()
    admin = fetch_admin(user["id"], ac)
    if not admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    if admin["schoolId"] != schoolId:
        raise HTTPException(status_code=403, detail="Forbidden")
    return admin


def require_any_admin(user: dict = Depends(require_user)) -> dict:
    """Admin gate for routes with no :schoolId in the path — the canonical
    library and the pedagogy library are curated content shared by every
    school, so there is nothing to scope the caller against. Being *an* admin
    of *some* school is the whole trust boundary here (see admin_canonical.py's
    module docstring); require_admin's per-school check simply has no path
    parameter to compare against on these routes."""
    ac = create_admin_client()
    admin = fetch_admin(user["id"], ac)
    if not admin:
        raise HTTPException(status_code=403, detail="Forbidden")
    return admin


def require_teacher(user: dict = Depends(require_user)) -> str:
    """Resolves this user's teacher row id, or 403s."""
    ac = create_admin_client()
    res = ac.table("teachers").select("id").eq("user_id", user["id"]).maybe_single().execute()
    teacher = res.data if res else None
    if not teacher:
        raise HTTPException(status_code=403, detail="Forbidden")
    return teacher["id"]


def require_student_token(x_student_token: Optional[str] = Header(None)) -> str:
    """The student portal's signed token can't cross origins as a cookie —
    the frontend sends the same signed value as X-Student-Token instead."""
    student_id = verify_student_cookie(x_student_token)
    if not student_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return student_id


def require_scanner_token(x_scanner_token: Optional[str] = Header(None)) -> str:
    """Scanner portal's join-code-derived token — already header-based, ports unchanged."""
    school_id = verify_school_token(x_scanner_token)
    if not school_id:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return school_id
