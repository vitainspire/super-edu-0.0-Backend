import os
import random
import string
import time
import uuid
from datetime import datetime, timezone
from fastapi import APIRouter, Request, HTTPException, Depends
from pydantic import BaseModel
from supabase import create_client
from supabase_auth.errors import AuthApiError

from ..lib.supabase_clients import create_admin_client
from ..lib.admin_queries import fetch_admin, fetch_school, upsert_admin, create_school
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_auth_rate_limit
from ..deps import require_user

router = APIRouter()


def _auth_client():
    return create_client(os.environ["NEXT_PUBLIC_SUPABASE_URL"], os.environ["NEXT_PUBLIC_SUPABASE_ANON_KEY"])


class LoginBody(BaseModel):
    email: str
    password: str


class RegisterBody(BaseModel):
    name: str
    email: str
    password: str
    schoolName: str


# POST /api/admin/login
# Port note: the Next.js original set the Supabase session as a cookie.
# Frontend and backend are no longer the same origin, so this returns the
# session tokens in the body — the frontend stores them and attaches
# `Authorization: Bearer <access_token>` on subsequent calls.
@router.post("/login")
def login(body: LoginBody, request: Request):
    ip = get_client_ip(request)
    t0 = time.time()

    allowed, _ = check_auth_rate_limit(ip)
    if not allowed:
        api_log("admin/login", ip, (time.time() - t0) * 1000, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    try:
        supabase = _auth_client()
        auth_res = supabase.auth.sign_in_with_password({"email": body.email, "password": body.password})
    except AuthApiError as e:
        # A real rejection from Supabase Auth (wrong password, unknown email,
        # unconfirmed email, etc.) — genuinely a 401.
        api_log("admin/login", ip, (time.time() - t0) * 1000, False, "unauthorized")
        raise HTTPException(status_code=401, detail=str(e) or "Invalid credentials")
    except Exception as e:
        # NOT a credentials verdict — a dropped connection, timeout, or other
        # transport failure talking to Supabase itself. Bucketing this as 401
        # is what made a transient network blip indistinguishable from a
        # wrong password on screen; 503 says "try again", not "check your
        # password", which is the actually-true thing here.
        api_log("admin/login", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=503, detail="Couldn't reach the login service — try again in a moment.")

    if not auth_res.user or not auth_res.session:
        api_log("admin/login", ip, (time.time() - t0) * 1000, False, "unauthorized")
        raise HTTPException(status_code=401, detail="Invalid credentials")

    ac = create_admin_client()
    admin = fetch_admin(auth_res.user.id, ac)
    if not admin:
        supabase.auth.sign_out()
        api_log("admin/login", ip, (time.time() - t0) * 1000, False, "forbidden")
        raise HTTPException(status_code=403, detail="No admin account found for this email")

    api_log("admin/login", ip, (time.time() - t0) * 1000, False, "ok", user_id=admin["id"])
    return {
        "admin": admin,
        "session": {
            "accessToken": auth_res.session.access_token,
            "refreshToken": auth_res.session.refresh_token,
            "expiresAt": auth_res.session.expires_at,
        },
    }


# POST /api/admin/register
@router.post("/register")
def register(body: RegisterBody, request: Request):
    ip = get_client_ip(request)
    t0 = time.time()

    allowed, _ = check_auth_rate_limit(ip)
    if not allowed:
        api_log("admin/register", ip, (time.time() - t0) * 1000, False, "rate_limited")
        raise HTTPException(status_code=429, detail="Too many attempts. Try again later.")

    supabase = _auth_client()
    try:
        signup_res = supabase.auth.sign_up({"email": body.email, "password": body.password})
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e) or "Sign up failed")

    if not signup_res.user:
        raise HTTPException(status_code=400, detail="Sign up failed")

    ac = create_admin_client()
    school_id = str(uuid.uuid4())
    admin_id = signup_res.user.id
    now = datetime.now(timezone.utc).isoformat()

    try:
        create_school(
            {
                "id": school_id,
                "name": body.schoolName,
                "joinCode": "".join(random.choices(string.ascii_uppercase + string.digits, k=6)),
                "createdBy": admin_id,
                "createdAt": now,
            },
            ac,
        )
        upsert_admin({"id": admin_id, "userId": admin_id, "name": body.name, "email": body.email, "schoolId": school_id, "createdAt": now}, ac)
    except Exception as e:
        ac.table("schools").delete().eq("id", school_id).execute()
        ac.auth.admin.delete_user(admin_id)
        api_log("admin/register", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Registration failed. Please try again.")

    if not signup_res.session:
        api_log("admin/register", ip, (time.time() - t0) * 1000, False, "ok", user_id=admin_id)
        return {"requiresEmailConfirmation": True}

    try:
        signin_res = supabase.auth.sign_in_with_password({"email": body.email, "password": body.password})
    except Exception:
        raise HTTPException(status_code=500, detail="Registered but sign-in failed. Please log in manually.")

    if not signin_res.user or not signin_res.session:
        raise HTTPException(status_code=500, detail="Registered but sign-in failed. Please log in manually.")

    api_log("admin/register", ip, (time.time() - t0) * 1000, False, "ok", user_id=admin_id)
    return {
        "adminId": admin_id,
        "schoolId": school_id,
        "session": {
            "accessToken": signin_res.session.access_token,
            "refreshToken": signin_res.session.refresh_token,
            "expiresAt": signin_res.session.expires_at,
        },
    }


# GET /api/admin/me
@router.get("/me")
def me(user: dict = Depends(require_user)):
    ac = create_admin_client()
    admin = fetch_admin(user["id"], ac)
    if not admin:
        raise HTTPException(status_code=403, detail="Not an admin")
    school = fetch_school(admin["schoolId"], ac)
    return {"admin": admin, "school": school}
