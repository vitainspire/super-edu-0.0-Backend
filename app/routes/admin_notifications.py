"""The admin portal's alert inbox.

Mirrors the teacher's /notifications endpoints, with one difference that runs
all the way through: these are addressed to a SCHOOL, not a person. require_admin
already proves the caller belongs to the school in the path, and every query is
scoped by that same school_id — so acknowledging an alert acknowledges it for
every admin there, which is the intent (see migration 0006).
"""
from fastapi import APIRouter, Depends

from typing import Optional

from ..lib.supabase_clients import create_admin_client
from ..lib.notifications import (
    fetch_admin_notifications, mark_admin_notification_read, mark_all_admin_notifications_read,
)
from ..lib.events import fetch_events
from ..deps import require_admin

router = APIRouter()


@router.get("/{schoolId}/notifications")
def get_admin_notifications(schoolId: str, admin: dict = Depends(require_admin)):
    return {"notifications": fetch_admin_notifications(schoolId, create_admin_client())}


@router.patch("/{schoolId}/notifications/{id}/read")
def read_admin_notification(schoolId: str, id: str, admin: dict = Depends(require_admin)):
    mark_admin_notification_read(id, schoolId, create_admin_client())
    return {"ok": True}


@router.patch("/{schoolId}/notifications/read-all")
def read_all_admin_notifications(schoolId: str, admin: dict = Depends(require_admin)):
    mark_all_admin_notifications_read(schoolId, create_admin_client())
    return {"ok": True}


# GET /{schoolId}/automation-log?date=YYYY-MM-DD
#
# The audit trail behind the absence automation: every absence recorded, cover
# assigned, cover cancelled and period left unfilled, newest first. Answers the
# question the substitutions table cannot — it holds only the current
# assignment for a slot, upserted in place, so it can say who is covering but
# never who was covering before, or why.
@router.get("/{schoolId}/automation-log")
def get_automation_log(schoolId: str, date: Optional[str] = None, admin: dict = Depends(require_admin)):
    try:
        return {"events": fetch_events(schoolId, create_admin_client(), date=date)}
    except Exception as e:
        # Almost certainly migration 0005 not yet applied. An unreadable audit
        # log should read as empty, not take the notifications page down with it.
        print(f"[admin/automation-log] failed: {e}")
        return {"events": []}
