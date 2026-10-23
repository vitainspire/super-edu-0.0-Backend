"""The one dispatch table that turns a confirmed Argus/agent proposal into a
real write — extracted from the old /argus/confirm handler
(app/routes/argus_routes.py) so the new Group Chat confirm endpoint can call
the exact same logic instead of a second copy of it. Behavior is unchanged
from the original inline version; this is a pure extraction.

Reuses the exact same functions the admin's own Approve/Reject/Post buttons
already call — this remains the only place a proposed action can turn into
an actual effect, and it only runs because a human confirmed it, never the
model directly.
"""
from fastapi import HTTPException

from ..routes.admin_substitutes import (
    approve_leave_request, reject_leave_request, LeaveRequestDecisionBody,
    patch_substitutes, UpdateAssignmentBody,
    post_substitutes, MarkUnavailableBody,
)
from ..routes.admin_misc import post_announcement as admin_post_announcement
from ..routes.admin_classes import post_assign_teacher, AssignTeacherBody
from .schemas import AnnouncementSchema


def execute_confirmed_action(school_id: str, tool: str, args: dict, admin: dict) -> dict:
    if tool == "approve_leave":
        return approve_leave_request(school_id, LeaveRequestDecisionBody(**args), admin)
    if tool == "reject_leave":
        return reject_leave_request(school_id, LeaveRequestDecisionBody(**args), admin)
    if tool == "post_announcement":
        return admin_post_announcement(school_id, AnnouncementSchema(**args), admin)
    if tool == "assign_substitute":
        return patch_substitutes(school_id, UpdateAssignmentBody(**args), admin)
    if tool == "mark_teacher_unavailable":
        # Same route the admin's own "mark unavailable" control uses, so
        # apply_teacher_absence runs and the resulting cover + notifications
        # are published exactly as normal.
        return post_substitutes(school_id, MarkUnavailableBody(**args), admin)
    if tool == "assign_teacher_to_class":
        args = dict(args)
        class_id = args.pop("classId", None)
        if not class_id:
            raise HTTPException(status_code=400, detail="classId is required.")
        return post_assign_teacher(school_id, class_id, AssignTeacherBody(**args), admin)
    raise HTTPException(status_code=400, detail=f'"{tool}" is not a confirmable action.')
