from typing import Optional
from fastapi import APIRouter
from ..lib.supabase_clients import create_admin_client

router = APIRouter()


@router.get("/school/has-admin")
def has_admin(schoolId: Optional[str] = None):
    if not schoolId:
        return {"hasAdmin": False}
    ac = create_admin_client()
    res = ac.table("admins").select("id").eq("school_id", schoolId).limit(1).maybe_single().execute()
    return {"hasAdmin": bool(res.data if res else None)}
