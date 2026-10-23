"""The Teacher Agent's own endpoint — one private thread per teacher with
their own agent (app/lib/teacher_agent.py). No routing classifier (unlike
the admin side's Group Chat): there's only one agent here, so every message
goes straight to its loop. Mounted at the same /api/teacher prefix
app/routes/teacher.py already uses — this is a sibling file, not a
replacement for anything there.
"""
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..lib.teacher_agent import run_teacher_agent
from ..lib.teacher_confirm_dispatch import execute_confirmed_teacher_action
from ..lib.teacher_context import get_teacher_context
from ..lib.agent_scheduler import maybe_run_teacher_daily_sweep
from ..lib.teacher_period_briefing import maybe_run_period_briefings
from ..lib.teacher_class_feedback import maybe_ask_class_feedback
from ..lib.teacher_agent_memory import load_active_notes, archive_note_by_id
from ..lib.group_chat_store import load_recent_messages, save_message, mark_confirmed, mark_feedback_submitted
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit
from ..deps import require_teacher

router = APIRouter()


def _recent_history(messages: list, max_turns: int = 3) -> list:
    turns = []
    pending_question = None
    for m in messages:
        if m["sender"] == "teacher":
            pending_question = m["content"]
        elif pending_question is not None:
            turns.append({"question": pending_question, "answer": m["content"]})
            pending_question = None
    return turns[-max_turns:]


@router.get("/agent/messages")
async def get_agent_messages(limit: int = 50, teacher_id: str = Depends(require_teacher)):
    ctx = get_teacher_context(teacher_id)
    # Opening My Agent is what triggers its due autonomous sweep (see
    # agent_scheduler.py's module docstring for why there's no real
    # scheduler) — same "triggered by activity" pattern group_chat_routes.py
    # already uses for the admin side.
    await maybe_run_teacher_daily_sweep(teacher_id, ctx)
    await maybe_run_period_briefings(teacher_id, ctx)
    await maybe_ask_class_feedback(teacher_id, ctx)
    return {"messages": load_recent_messages(ctx["schoolId"] or "", thread="teacher", teacher_id=teacher_id, limit=limit)}


class TeacherAgentPostBody(BaseModel):
    content: str


@router.post("/agent/messages")
async def post_agent_message(body: TeacherAgentPostBody, request: Request, teacher_id: str = Depends(require_teacher)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required.")

    ctx = get_teacher_context(teacher_id)
    school_id = ctx["schoolId"] or ""

    prior = load_recent_messages(school_id, thread="teacher", teacher_id=teacher_id, limit=20)
    history = _recent_history(prior)

    admin_message = save_message(school_id, sender="teacher", content=content, thread="teacher", teacher_id=teacher_id) or {
        "id": None, "thread": "teacher", "sender": "teacher", "adminId": None, "content": content,
        "trace": [], "pendingConfirmation": None, "confirmedAt": None, "createdAt": None,
    }

    t0 = time.time()
    try:
        result = await run_teacher_agent(teacher_id, content, history, ip)
    except Exception as e:
        api_log("teacher-agent", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        result = {"status": "error", "response": "Something went wrong answering that — try again.", "trace": []}

    if result["status"] == "pending_confirmation":
        msg_content = result.get("label") or "I'd like to do something — confirm below."
        trace = result.get("trace")
        pending_confirmation = {"tool": result["proposal"]["tool"], "args": result["proposal"], "label": result.get("label")}
    else:
        msg_content = result.get("response") or "I didn't find anything that answers that."
        trace = result.get("trace")
        pending_confirmation = None

    saved = save_message(
        school_id, sender="teacher_agent", content=msg_content, thread="teacher", teacher_id=teacher_id,
        trace=trace, pending_confirmation=pending_confirmation,
    )
    new_message = saved or {
        "id": None, "thread": "teacher", "sender": "teacher_agent", "adminId": None, "content": msg_content,
        "trace": trace or [], "pendingConfirmation": pending_confirmation,
        "confirmedAt": None, "createdAt": None,
    }

    return {"adminMessage": admin_message, "messages": [new_message]}


@router.get("/agent/memory")
def get_agent_memory(teacher_id: str = Depends(require_teacher)):
    return {"notes": load_active_notes(teacher_id)}


class TeacherAgentForgetBody(BaseModel):
    noteId: str


@router.post("/agent/memory/forget")
def post_agent_memory_forget(body: TeacherAgentForgetBody, teacher_id: str = Depends(require_teacher)):
    ok = archive_note_by_id(teacher_id, body.noteId)
    return {"ok": ok}


class TeacherAgentFeedbackSubmittedBody(BaseModel):
    messageId: str


@router.post("/agent/feedback-submitted")
def post_agent_feedback_submitted(body: TeacherAgentFeedbackSubmittedBody, teacher_id: str = Depends(require_teacher)):
    """Marks a feedback-request card answered. The actual save (lesson_feedback,
    classes.feedback_profile, the interpret-feedback cost rule) already
    happened client-side via the real saveLessonFeedback (lib/context.tsx) —
    this only stops the card re-rendering as interactive."""
    ok = mark_feedback_submitted(body.messageId)
    return {"ok": ok}


class TeacherAgentConfirmBody(BaseModel):
    messageId: Optional[str] = None
    tool: str
    args: dict


@router.post("/agent/confirm")
async def post_agent_confirm(body: TeacherAgentConfirmBody, request: Request, teacher_id: str = Depends(require_teacher)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        args = {k: v for k, v in body.args.items() if k != "tool"}
        result = await execute_confirmed_teacher_action(teacher_id, body.tool, args)
        # Most actions here raise on real failure (caught below), so {"ok": True}
        # has always been safe to assume — but generate_prep_material can
        # legitimately refuse (no textbook match) and return {"ok": False,
        # "error": ...} without raising. Only mark the message confirmed, and
        # only report success, when that's not what happened.
        succeeded = not (isinstance(result, dict) and result.get("ok") is False)
        if body.messageId and succeeded:
            mark_confirmed(body.messageId)
        api_log("teacher-agent-confirm", ip, (time.time() - t0) * 1000, False, "ok")
        return {"ok": succeeded, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        api_log("teacher-agent-confirm", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Couldn't complete that action — try again.")
