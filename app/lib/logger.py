import json
from datetime import datetime, timezone
from typing import Literal, Optional
from fastapi import Request

LogStatus = Literal["ok", "error", "rate_limited", "unauthorized", "forbidden", "bad_request"]


def get_client_ip(req: Request) -> str:
    forwarded_for = req.headers.get("x-forwarded-for")
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    real_ip = req.headers.get("x-real-ip")
    if real_ip:
        return real_ip
    return "unknown"


def api_log(
    route: str,
    ip: str,
    duration_ms: float,
    from_cache: bool,
    status: LogStatus,
    user_id: Optional[str] = None,
    error: Optional[str] = None,
) -> None:
    """Structured JSON log line — mirrors backend/src/lib/logger.ts's apiLog."""
    entry = {
        "t": datetime.now(timezone.utc).isoformat(),
        "route": route,
        "ip": ip,
        "fromCache": from_cache,
        "durationMs": duration_ms,
        "status": status,
    }
    if user_id is not None:
        entry["userId"] = user_id
    if error is not None:
        entry["error"] = error
    print(json.dumps(entry))
