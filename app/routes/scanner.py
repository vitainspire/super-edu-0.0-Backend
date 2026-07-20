import time
from fastapi import APIRouter, Request, Depends, HTTPException
from pydantic import BaseModel

from ..lib.supabase_clients import create_admin_client
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_auth_rate_limit
from ..lib.scanner_auth import sign_school_token
from ..deps import require_user

router = APIRouter()


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
