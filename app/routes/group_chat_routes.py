"""Group Chat: the shared room where an admin talks with both the
Headmaster Agent and the Admin Agent in one thread, each a visibly distinct
participant with its own tools. Builds entirely on top of
app/lib/argus_agent.py's run_agent/route_message and
app/lib/group_chat_store.py — nothing here duplicates logic that already
exists for the (still separately working) single-persona /argus route.
"""
import time
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from ..lib.argus_agent import run_agent, route_message, PERSONAS
from ..lib.argus_confirm_dispatch import execute_confirmed_action
from ..lib.agent_scheduler import maybe_run_daily_sweeps
from ..lib.group_chat_store import load_recent_messages, save_message, mark_confirmed
from ..lib.logger import api_log, get_client_ip
from ..lib.rate_limit import check_rate_limit
from ..deps import require_admin

router = APIRouter()


@router.get("/{schoolId}/group-chat/messages")
async def get_group_chat_messages(schoolId: str, limit: int = 50, admin: dict = Depends(require_admin)):
    # Opening either chat view is what triggers a due autonomous sweep (see
    # agent_scheduler.py's module docstring for why there's no real
    # scheduler) — group chat and individual DMs both check, since either one
    # being opened proves the admin is actually using the app right now.
    await maybe_run_daily_sweeps(schoolId)
    return {"messages": load_recent_messages(schoolId, thread="group", limit=limit)}


@router.get("/{schoolId}/agent-messages/{persona}")
async def get_agent_messages(schoolId: str, persona: str, limit: int = 50, admin: dict = Depends(require_admin)):
    if persona not in PERSONAS:
        raise HTTPException(status_code=404, detail=f'Unknown agent "{persona}".')
    await maybe_run_daily_sweeps(schoolId)
    return {"messages": load_recent_messages(schoolId, thread=persona, limit=limit)}


class GroupChatPostBody(BaseModel):
    content: str


def _recent_history(messages: list, max_turns: int = 3) -> list:
    """Same shape history_block() expects elsewhere in this app: the last few
    {question, answer} turns, built from whatever's already persisted so a
    follow-up question ("what about tomorrow") still resolves."""
    turns = []
    pending_question = None
    for m in messages:
        if m["sender"] == "admin":
            pending_question = m["content"]
        elif pending_question is not None:
            turns.append({"question": pending_question, "answer": m["content"]})
            pending_question = None
    return turns[-max_turns:]


async def _run_persona_turn(schoolId: str, persona: str, content: str, history: list, ip: str, thread: str) -> dict:
    """Runs one persona's agent loop for one turn and persists the result on
    `thread`. Shared by Group Chat (where `route_message` already picked the
    persona) and Individual Messages (where the admin picked it by opening
    that DM — no classifier involved)."""
    t0 = time.time()
    try:
        result = await run_agent(persona, schoolId, content, history, ip)
    except Exception as e:
        api_log(f"{thread}-{persona}", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        result = {"status": "error", "response": "Something went wrong answering that — try again.", "trace": []}

    sender = f"{persona}_agent"
    if result["status"] == "pending_confirmation":
        msg_content = result.get("label") or "I'd like to do something — confirm below."
        trace = result.get("trace")
        pending_confirmation = {"tool": result["proposal"]["tool"], "args": result["proposal"], "label": result.get("label")}
    else:
        msg_content = result.get("response") or "I didn't find anything that answers that."
        trace = result.get("trace")
        pending_confirmation = None

    saved = save_message(schoolId, sender=sender, content=msg_content, thread=thread, trace=trace, pending_confirmation=pending_confirmation)
    # The LLM call already happened (real cost, real latency) regardless of
    # whether persistence succeeded — if group_chat_store degraded (migration
    # 0010 not applied yet, same no-op-on-missing-table rule argus_memory.py
    # uses), the admin still gets the answer they paid for, it just won't be
    # there on reload. `id: None` tells the frontend this message can't be
    # referenced for a later /confirm call tied to a persisted id —
    # acceptable since that same table being missing means /confirm has
    # nothing to mark confirmed anyway.
    return saved or {
        "id": None, "thread": thread, "sender": sender, "adminId": None, "content": msg_content,
        "trace": trace or [], "pendingConfirmation": pending_confirmation,
        "confirmedAt": None, "createdAt": None,
    }


@router.post("/{schoolId}/group-chat/messages")
async def post_group_chat_message(schoolId: str, body: GroupChatPostBody, request: Request, admin: dict = Depends(require_admin)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required.")

    prior = load_recent_messages(schoolId, thread="group", limit=20)
    history = _recent_history(prior)

    admin_message = save_message(schoolId, sender="admin", content=content, thread="group", admin_id=admin["id"]) or {
        "id": None, "thread": "group", "sender": "admin", "adminId": admin["id"], "content": content,
        "trace": [], "pendingConfirmation": None, "confirmedAt": None, "createdAt": None,
    }

    t0 = time.time()
    try:
        personas = await route_message(content, history)
    except Exception as e:
        api_log("group-chat-route", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        personas = ["admin"]

    # Sequential, not concurrent — each persona's run is its own reasoning
    # loop over the same question, and there's no reason for two to be racing
    # against each other for a single admin's single message.
    new_messages = [await _run_persona_turn(schoolId, persona, content, history, ip, "group") for persona in personas]

    return {"adminMessage": admin_message, "messages": new_messages}


class AgentMessagePostBody(BaseModel):
    content: str


@router.post("/{schoolId}/agent-messages/{persona}")
async def post_agent_message(schoolId: str, persona: str, body: AgentMessagePostBody, request: Request, admin: dict = Depends(require_admin)):
    if persona not in PERSONAS:
        raise HTTPException(status_code=404, detail=f'Unknown agent "{persona}".')

    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    content = body.content.strip()
    if not content:
        raise HTTPException(status_code=400, detail="content is required.")

    prior = load_recent_messages(schoolId, thread=persona, limit=20)
    history = _recent_history(prior)

    admin_message = save_message(schoolId, sender="admin", content=content, thread=persona, admin_id=admin["id"]) or {
        "id": None, "thread": persona, "sender": "admin", "adminId": admin["id"], "content": content,
        "trace": [], "pendingConfirmation": None, "confirmedAt": None, "createdAt": None,
    }

    # No route_message classifier here — the admin picked this exact agent by
    # opening its DM, so its loop runs directly on the question.
    new_message = await _run_persona_turn(schoolId, persona, content, history, ip, persona)

    return {"adminMessage": admin_message, "messages": [new_message]}


class GroupChatConfirmBody(BaseModel):
    # Optional: a message created while migration 0010 is unapplied has no
    # persisted id (group_chat_store degrades to None — see save_message),
    # so the frontend sends null rather than omitting the field entirely.
    # Nothing to mark confirmed in that case; the action itself still runs.
    messageId: Optional[str] = None
    tool: str
    args: dict


@router.post("/{schoolId}/group-chat/confirm")
def post_group_chat_confirm(schoolId: str, body: GroupChatConfirmBody, request: Request, admin: dict = Depends(require_admin)):
    ip = get_client_ip(request)
    allowed, _ = check_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Too many requests. Please try again later.")

    t0 = time.time()
    try:
        # The proposal dict stored alongside "tool"/"args" by run_agent's
        # action tools carries "tool" as one of its own keys too (see e.g.
        # _tool_approve_leave's {"tool": "approve_leave", ...}); strip it so
        # it isn't passed through twice as a kwarg to the Pydantic body below.
        args = {k: v for k, v in body.args.items() if k != "tool"}
        result = execute_confirmed_action(schoolId, body.tool, args, admin)
        if body.messageId:
            mark_confirmed(body.messageId)
        api_log("group-chat-confirm", ip, (time.time() - t0) * 1000, False, "ok")
        return {"ok": True, "result": result}
    except HTTPException:
        raise
    except Exception as e:
        api_log("group-chat-confirm", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
        raise HTTPException(status_code=500, detail="Couldn't complete that action — try again.")
