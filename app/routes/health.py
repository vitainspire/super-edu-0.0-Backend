import os
from datetime import datetime, timezone
from fastapi import APIRouter, Response
from supabase import create_client

router = APIRouter()


@router.get("/health")
def health(response: Response):
    checks: dict[str, str] = {}

    try:
        supabase = create_client(
            os.environ.get("NEXT_PUBLIC_SUPABASE_URL", ""),
            os.environ.get("NEXT_PUBLIC_SUPABASE_ANON_KEY", ""),
        )
        supabase.table("teachers").select("id").limit(1).execute()
        checks["supabase"] = "ok"
    except Exception:
        checks["supabase"] = "error"

    checks["openrouter"] = "ok" if os.environ.get("OPENROUTER_API_KEY") else "error"

    all_ok = all(v == "ok" for v in checks.values())
    response.status_code = 200 if all_ok else 503
    return {"status": "ok" if all_ok else "degraded", "checks": checks, "ts": datetime.now(timezone.utc).isoformat()}
