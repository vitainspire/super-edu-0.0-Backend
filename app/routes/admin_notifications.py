"""The admin portal's alert inbox.

Mirrors the teacher's /notifications endpoints, with one difference that runs
all the way through: these are addressed to a SCHOOL, not a person. require_admin
already proves the caller belongs to the school in the path, and every query is
scoped by that same school_id — so acknowledging an alert acknowledges it for
every admin there, which is the intent (see migration 0006).
"""
from fastapi import APIRouter, Depends

from ..lib.supabase_clients import create_admin_client
from ..lib.notifications import (
    fetch_admin_notifications, mark_admin_notification_read, mark_all_admin_notifications_read,
)
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
