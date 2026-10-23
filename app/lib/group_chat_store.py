"""Persistence for Group Chat AND Individual Messages — both are rows in the
same table, told apart by `thread` ('group', or 'headmaster'/'admin' for a
1:1 DM with just that persona — see migration 0010's header comment).

Same shape as app/lib/argus_memory.py: a thin wrapper over one Supabase
table, degrading to an empty read / silent no-op if the table isn't there
yet (migration 0010 unapplied), so a missing table reads as "no history
yet" rather than taking the feature down.
"""
from datetime import datetime, timezone

from .supabase_clients import create_admin_client

TABLE = "group_chat_messages"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _to_app(r: dict) -> dict:
    return {
        "id": r["id"],
        "thread": r.get("thread") or "group",
        "sender": r["sender"],
        "adminId": r.get("admin_id"),
        "content": r["content"],
        "trace": r.get("trace") or [],
        "pendingConfirmation": r.get("pending_confirmation"),
        "link": r.get("link"),
        "sourcePersona": r.get("source_persona"),
        "feedbackRequest": r.get("feedback_request"),
        "feedbackSubmittedAt": r.get("feedback_submitted_at"),
        "confirmedAt": r.get("confirmed_at"),
        "createdAt": r["created_at"],
    }


def load_recent_messages(school_id: str, thread: str = "group", teacher_id: str | None = None, limit: int = 50, ac=None) -> list:
    """Most recent `limit` messages on one thread, oldest-first (ready to
    render top-to-bottom). Pass `teacher_id` for thread='teacher' — it's the
    only thing that keeps different teachers' own threads apart at the same
    school (school_id + thread alone would merge every teacher's messages
    into one pool)."""
    ac = ac or create_admin_client()
    try:
        query = (
            ac.table(TABLE).select("*")
            .eq("school_id", school_id)
            .eq("thread", thread)
        )
        if teacher_id:
            query = query.eq("teacher_id", teacher_id)
        rows = query.order("created_at", desc=True).limit(limit).execute().data or []
    except Exception as e:
        if not _table_missing(e):
            print(f"[group_chat_store] load_recent_messages failed: {e}")
        return []
    return [_to_app(r) for r in reversed(rows)]


def save_message(
    school_id: str,
    sender: str,
    content: str,
    thread: str = "group",
    teacher_id: str | None = None,
    trace: list | None = None,
    pending_confirmation: dict | None = None,
    link: dict | None = None,
    feedback_request: dict | None = None,
    admin_id: str | None = None,
    source_persona: str | None = None,
    ac=None,
) -> dict | None:
    """Persists one message. Returns the saved row (app-shaped), or None if
    the table isn't there yet — callers should still be able to answer in the
    chat even if history can't be saved, same degrade rule as argus_memory.

    `source_persona` is separate from `sender`: a performance_watch coaching
    message is still sent *as* this teacher's own agent (sender="teacher_agent",
    so it lands in their one thread like everything else), but it originated
    from the Headmaster Agent's own sweep — source_persona="headmaster" lets
    the UI color-code that provenance instead of presenting it as an
    indistinguishable My Agent message."""
    ac = ac or create_admin_client()
    payload = {
        "school_id": school_id,
        "thread": thread,
        "teacher_id": teacher_id,
        "sender": sender,
        "admin_id": admin_id,
        "content": content,
        "trace": trace or [],
        "pending_confirmation": pending_confirmation,
        "link": link,
        "source_persona": source_persona,
        "feedback_request": feedback_request,
        "created_at": _now(),
    }
    try:
        rows = ac.table(TABLE).insert(payload).execute().data or []
    except Exception as e:
        if not _table_missing(e):
            print(f"[group_chat_store] save_message failed: {e}")
        return None
    return _to_app(rows[0]) if rows else None


def mark_feedback_submitted(message_id: str, ac=None) -> bool:
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).update({"feedback_submitted_at": _now()}).eq("id", message_id).execute()
        return True
    except Exception as e:
        if not _table_missing(e):
            print(f"[group_chat_store] mark_feedback_submitted failed: {e}")
        return False


def mark_confirmed(message_id: str, ac=None) -> bool:
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).update({"confirmed_at": _now()}).eq("id", message_id).execute()
        return True
    except Exception as e:
        if not _table_missing(e):
            print(f"[group_chat_store] mark_confirmed failed: {e}")
        return False
