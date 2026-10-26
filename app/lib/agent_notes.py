"""Headmaster/Admin Agent persistent memory — short notes saved across
conversations, the same role teacher_agent_memory.py plays for My Agent.
Keyed by (school_id, persona) instead of teacher_id: one shared note pool per
persona per school, since the Headmaster Agent and Admin Agent are each one
assistant serving whichever admin is logged in, not one per admin.

Same graceful-degrade rule every other table-backed module in this codebase
follows: a missing table reads as "nothing remembered yet", never an error.
"""
from datetime import datetime, timezone

from .memory_reconcile import reconcile_note
from .supabase_clients import create_admin_client

TABLE = "agent_notes"
MAX_NOTE_LENGTH = 240

# Capped for the same reason teacher_agent_memory.py caps at 15: the prompt
# reads every active note on every turn, and an unbounded pile would crowd
# out the real instructions.
MAX_ACTIVE_NOTES = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _to_app(r: dict) -> dict:
    return {"id": r["id"], "content": r["content"], "createdAt": r.get("created_at")}


def load_active_notes(school_id: str, persona: str, limit: int = MAX_ACTIVE_NOTES, ac=None) -> list:
    ac = ac or create_admin_client()
    try:
        rows = (
            ac.table(TABLE).select("*")
            .eq("school_id", school_id).eq("persona", persona).is_("archived_at", "null")
            .order("created_at", desc=True).limit(limit).execute().data or []
        )
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_notes] load_active_notes failed: {e}")
        return []
    return [_to_app(r) for r in rows]


async def save_note(school_id: str, persona: str, content: str, ac=None) -> dict:
    """Reconciles against existing active notes before writing (see
    memory_reconcile.py) — a NOOP is dropped rather than stored, an UPDATE/
    DELETE archives the note it supersedes first, so a changed preference
    replaces the old one instead of both sitting in the prompt forever,
    contradicting each other."""
    content = " ".join((content or "").split())[:MAX_NOTE_LENGTH]
    if not content:
        return {"error": "Nothing to remember — the note was empty."}
    ac = ac or create_admin_client()

    existing = load_active_notes(school_id, persona, ac=ac)
    decision = await reconcile_note(existing, content)
    if decision["action"] == "noop":
        return {"saved": False, "reason": "Already remembered — nothing changed."}
    if decision["action"] in ("update", "delete"):
        archive_note_by_id(school_id, persona, decision["targetId"], ac=ac)
    if decision["action"] == "delete":
        return {"saved": False, "forgotten": True, "reason": "Replaced an earlier note; nothing new to add."}

    payload = {"school_id": school_id, "persona": persona, "content": content, "created_at": _now()}
    try:
        row = (ac.table(TABLE).insert(payload).execute().data or [None])[0]
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_notes] save_note failed: {e}")
        return {"saved": False}
    result = {"saved": True, "note": _to_app(row) if row else {"content": content}}
    if decision["action"] == "update":
        result["updated"] = True
    return result


def archive_note_by_id(school_id: str, persona: str, note_id: str, ac=None) -> bool:
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).update({"archived_at": _now()}) \
            .eq("id", note_id).eq("school_id", school_id).eq("persona", persona).execute()
        return True
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_notes] archive_note_by_id failed: {e}")
        return False


def archive_note_by_ref(school_id: str, persona: str, note_ref: str, ac=None) -> dict:
    """Same found/ambiguous/none contract teacher_agent_memory.archive_note_by_ref
    uses — the forget_note tool only ever has the admin's words to go on, not an id."""
    ref = (note_ref or "").strip().lower()
    if not ref:
        return {"kind": "none"}
    ac = ac or create_admin_client()
    notes = load_active_notes(school_id, persona, limit=50, ac=ac)
    hits = [n for n in notes if ref in n["content"].lower()]
    if not hits:
        return {"kind": "none"}
    if len(hits) > 1:
        return {"kind": "ambiguous", "candidates": [n["content"] for n in hits]}
    archive_note_by_id(school_id, persona, hits[0]["id"], ac=ac)
    return {"kind": "found", "note": hits[0]}
