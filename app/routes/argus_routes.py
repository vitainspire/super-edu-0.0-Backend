"""Argus's agentic endpoint — phase 1 of the agent-loop rollout (see
app/lib/argus_agent.py's module docstring). Additive: /ask-intent in
admin_misc.py is untouched and keeps serving the existing admin assistant.
This is a separate path so the two can be compared and this one can be
turned off with zero risk to what's already live."""
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..lib.argus_agent import run_argus
from ..lib.argus_sweep import run_sweep, PLAYBOOKS
from ..lib.argus_memory import load_open_findings, dismiss_finding
from ..lib.schemas import AdminAskIntentSchema, AnnouncementSchema
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit
from ..deps import require_admin
# Reusing the exact same functions the admin's own Approve/Reject/Post
# buttons already call — the confirm step below is the ONLY place that
# executes a write, and it does so through this app's one real
# implementation of each action, never a second copy of that logic.
from .admin_substitutes import (
    approve_leave_request, reject_leave_request, LeaveRequestDecisionBody,
    patch_substitutes, UpdateAssignmentBody,
    post_substitutes, MarkUnavailableBody,
)
from .admin_misc import post_announcement as admin_post_announcement
from .admin_classes import post_assign_teacher, AssignTeacherBody

router = APIRouter()


@router.post("/{schoolId}/argus")
async def argus_ask(schoolId: str, body: AdminAskIntentSchema, request: Request, admin: dict = Depends(require_admin)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        result = await run_argus(schoolId, body.question, body.history, ip)
        api_log("argus-run", ip, (time.time() - t0) * 1000, False, "ok")
        return result
    except Exception as e:
        api_log("argus-run", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Argus failed to answer that — try again.")


class ArgusConfirmBody(BaseModel):
    tool: str
    args: dict


# The confirm gate. Nothing above this line, and nothing in argus_agent.py,
# ever writes data — this is the one path a proposed action can turn into a
# real effect, and it only runs because a human (not the model) called it.
@router.post("/{schoolId}/argus/confirm")
def argus_confirm(schoolId: str, body: ArgusConfirmBody, request: Request, admin: dict = Depends(require_admin)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        if body.tool == "approve_leave":
            result = approve_leave_request(schoolId, LeaveRequestDecisionBody(**body.args), admin)
        elif body.tool == "reject_leave":
            result = reject_leave_request(schoolId, LeaveRequestDecisionBody(**body.args), admin)
        elif body.tool == "post_announcement":
            result = admin_post_announcement(schoolId, AnnouncementSchema(**body.args), admin)
        elif body.tool == "assign_substitute":
            result = patch_substitutes(schoolId, UpdateAssignmentBody(**body.args), admin)
        elif body.tool == "mark_teacher_unavailable":
            # Goes through the same route the admin's own "mark unavailable"
            # control uses, so apply_teacher_absence runs and the resulting
            # cover + notifications are published exactly as normal.
            result = post_substitutes(schoolId, MarkUnavailableBody(**body.args), admin)
        elif body.tool == "assign_teacher_to_class":
            args = dict(body.args)
            class_id = args.pop("classId", None)
            if not class_id:
                raise HTTPException(status_code=400, detail="classId is required.")
            result = post_assign_teacher(schoolId, class_id, AssignTeacherBody(**args), admin)
        else:
            raise HTTPException(status_code=400, detail=f'"{body.tool}" is not a confirmable action.')
        api_log("argus-confirm", ip, (time.time() - t0) * 1000, False, "ok")
        return {"ok": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        api_log("argus-confirm", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Couldn't complete that action — try again.")


# ─── Proactive sweeps ──────────────────────────────────────────────────────────
# The difference between an assistant and an operations agent: nobody has to
# ask. A sweep runs the standing playbooks, reconciles what it finds against
# Argus's own memory of what it already said, and reports only what's new or
# has gone stale-unfixed. It still writes nothing but that memory — each fix
# it finds arrives as a proposal for /argus/confirm.
class ArgusSweepBody(BaseModel):
    # Omit to run every playbook; name a subset to run just those.
    playbooks: Optional[list] = None
    date: Optional[str] = None
    # Set false to skip the LLM phrasing step entirely and take the
    # deterministic summary — also what happens automatically if the model
    # is unavailable.
    phrase: bool = True


@router.post("/{schoolId}/argus/sweep")
async def argus_sweep(schoolId: str, body: ArgusSweepBody, request: Request, admin: dict = Depends(require_admin)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    unknown = [p for p in (body.playbooks or []) if p not in PLAYBOOKS]
    if unknown:
        raise HTTPException(status_code=400, detail=f'Unknown playbook(s): {", ".join(unknown)}. Available: {", ".join(PLAYBOOKS)}')

    t0 = time.time()
    try:
        return await run_sweep(schoolId, body.playbooks, body.date, ip, body.phrase)
    except Exception as e:
        api_log("argus-sweep", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Sweep failed — try again.")


@router.get("/{schoolId}/argus/findings")
def argus_findings(schoolId: str, admin: dict = Depends(require_admin)):
    """Everything Argus currently considers open, whether or not it was
    reported this cycle — the standing worklist behind the briefings."""
    findings = load_open_findings(schoolId)
    return {
        "findings": findings,
        "actionable": [f for f in findings if f.get("suggestedAction")],
        "playbooks": list(PLAYBOOKS),
    }


class ArgusDismissBody(BaseModel):
    findingKey: str


@router.post("/{schoolId}/argus/findings/dismiss")
def argus_dismiss(schoolId: str, body: ArgusDismissBody, admin: dict = Depends(require_admin)):
    """Stop reporting a finding that is still present. Recorded as dismissed,
    never as resolved — otherwise the resolved list would be a lie."""
    ok = dismiss_finding(schoolId, body.findingKey)
    if not ok:
        raise HTTPException(status_code=500, detail="Couldn't dismiss that finding.")
    return {"ok": True, "findingKey": body.findingKey}
