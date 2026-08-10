"""Admin-facing counterpart to app/lib/notifications.py (teacher_notifications)
— same plain outbox shape, scoped to school_id instead of teacher_id. No admin
notification mechanism existed at all before this; the first trigger is a
Prep Material batch actually regenerating under a given generation mode."""
import uuid
from datetime import datetime, timezone
from typing import Optional


def create_admin_notification(school_id: str, notif_type: str, message: str, ac) -> None:
    ac.table("admin_notifications").insert({
        "id": str(uuid.uuid4()), "school_id": school_id, "type": notif_type, "message": message,
    }).execute()


def fetch_admin_notifications(school_id: str, ac, limit: int = 30) -> list[dict]:
    data = (
        ac.table("admin_notifications").select("*")
        .eq("school_id", school_id).order("created_at", desc=True).limit(limit).execute().data or []
    )
    return [
        {
            "id": r["id"], "type": r["type"], "message": r["message"],
            "readAt": r.get("read_at"), "createdAt": r["created_at"],
        }
        for r in data
    ]


def mark_admin_notification_read(notification_id: str, school_id: str, ac) -> None:
    ac.table("admin_notifications").update({
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", notification_id).eq("school_id", school_id).execute()


def mark_all_admin_notifications_read(school_id: str, ac) -> None:
    ac.table("admin_notifications").update({
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("school_id", school_id).is_("read_at", "null").execute()
