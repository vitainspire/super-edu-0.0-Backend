"""A minimal notification outbox — one table, no scheduler, no AI. Populated
by deterministic triggers (currently: substitute assignment on leave
approval) and read by the teacher portal's notification bell. Deliberately
plain: a row is a fact ("you're covering Period 4"), not a chat message."""
import uuid
from datetime import datetime, timezone
from typing import Optional


def create_notification(teacher_id: str, notif_type: str, message: str, ac, class_id: Optional[str] = None, date: Optional[str] = None) -> None:
    ac.table("teacher_notifications").insert({
        "id": str(uuid.uuid4()), "teacher_id": teacher_id, "type": notif_type, "message": message,
        "class_id": class_id, "date": date,
    }).execute()


def fetch_notifications(teacher_id: str, ac, limit: int = 30) -> list[dict]:
    data = (
        ac.table("teacher_notifications").select("*")
        .eq("teacher_id", teacher_id).order("created_at", desc=True).limit(limit).execute().data or []
    )
    return [
        {
            "id": r["id"], "type": r["type"], "message": r["message"],
            "classId": r.get("class_id"), "date": r.get("date"),
            "readAt": r.get("read_at"), "createdAt": r["created_at"],
        }
        for r in data
    ]


def mark_notification_read(notification_id: str, teacher_id: str, ac) -> None:
    # Scoped by teacher_id too, not just id — a teacher can only mark their own read.
    ac.table("teacher_notifications").update({
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", notification_id).eq("teacher_id", teacher_id).execute()


def mark_all_notifications_read(teacher_id: str, ac) -> None:
    ac.table("teacher_notifications").update({
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("teacher_id", teacher_id).is_("read_at", "null").execute()
