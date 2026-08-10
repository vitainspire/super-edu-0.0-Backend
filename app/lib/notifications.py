"""Minimal notification outboxes — two tables, no scheduler, no AI. Populated
by deterministic triggers (see substitute_automation.py's event handlers) and
read by the portals' notification bells. Deliberately plain: a row is a fact
("you're covering Period 4"), not a chat message.

Two audiences, addressed differently:

  teacher_notifications  personal mail, keyed by teacher_id
  admin_notifications    work items belonging to a SCHOOL, keyed by school_id —
                         any admin can act on one, and doing so clears it for
                         all of them
"""
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


# ── Admin inbox ───────────────────────────────────────────────────────────────

def create_admin_notification(
    school_id: str, notif_type: str, message: str, ac,
    class_id: Optional[str] = None, date: Optional[str] = None, period_number: Optional[int] = None,
) -> None:
    """Raise a school-level alert. The class/date/period triple is what lets
    the bell deep-link to the fix and lets a superseded alert be found again by
    resolve_admin_notifications_for_slot."""
    ac.table("admin_notifications").insert({
        "id": str(uuid.uuid4()), "school_id": school_id, "type": notif_type, "message": message,
        "class_id": class_id, "date": date, "period_number": period_number,
    }).execute()


def fetch_admin_notifications(school_id: str, ac, limit: int = 30) -> list[dict]:
    data = (
        ac.table("admin_notifications").select("*")
        .eq("school_id", school_id).order("created_at", desc=True).limit(limit).execute().data or []
    )
    return [
        {
            "id": r["id"], "type": r["type"], "message": r["message"],
            "classId": r.get("class_id"), "date": r.get("date"), "periodNumber": r.get("period_number"),
            "readAt": r.get("read_at"), "createdAt": r["created_at"],
        }
        for r in data
    ]


def mark_admin_notification_read(notification_id: str, school_id: str, ac) -> None:
    # Scoped by school_id too, not just id — an admin can only acknowledge
    # alerts belonging to their own school.
    ac.table("admin_notifications").update({
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", notification_id).eq("school_id", school_id).execute()


def mark_all_admin_notifications_read(school_id: str, ac) -> None:
    ac.table("admin_notifications").update({
        "read_at": datetime.now(timezone.utc).isoformat(),
    }).eq("school_id", school_id).is_("read_at", "null").execute()


def resolve_admin_notifications_for_slot(
    school_id: str, date: str, class_id: str, period_number: int, ac,
    notif_type: Optional[str] = None,
) -> None:
    """Clear open alerts about one period once the problem has gone away.

    An uncovered period that later gets a substitute — because someone came
    back, or an admin reassigned by hand — should not keep sitting in the
    inbox. Without this the bell fills with alerts for problems that already
    fixed themselves, and admins learn to ignore it.
    """
    query = (
        ac.table("admin_notifications")
        .update({"read_at": datetime.now(timezone.utc).isoformat()})
        .eq("school_id", school_id).eq("date", date)
        .eq("class_id", class_id).eq("period_number", period_number)
        .is_("read_at", "null")
    )
    if notif_type:
        query = query.eq("type", notif_type)
    query.execute()
