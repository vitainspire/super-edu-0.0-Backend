"""My Agent's persistent memory — short notes saved across conversations, not
just the current chat window (that's ask_history.history_block's job, capped
at the last 3 turns of THIS conversation). This is the thing that lets the
agent say "I know you prefer short feedback on marks" in a conversation that
started five minutes ago with no history at all.

Same graceful-degrade rule every other table-backed module in this codebase
follows: a missing table (migration 0012 unapplied) reads as "nothing
remembered yet", never an error.
"""
from datetime import datetime, timezone

from .memory_reconcile import reconcile_note
from .supabase_clients import create_admin_client

TABLE = "teacher_agent_memory"
MAX_NOTE_LENGTH = 240

# Capped, not unbounded — the prompt reads every active note on every turn,
# and a growing pile of old notes would eventually crowd out the real
# instructions the same way prep_flow/agents/feedback.py's MAX_DIRECTIVES
# exists to stop happening to its own directive list. Older notes are never
# deleted, just no longer shown; a teacher can still see/forget them via the
# full list if a "what I remember" view ever needs to page further back.
MAX_ACTIVE_NOTES = 15


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _to_app(r: dict) -> dict:
    return {"id": r["id"], "content": r["content"], "createdAt": r.get("created_at")}


def load_active_notes(teacher_id: str, limit: int = MAX_ACTIVE_NOTES, ac=None) -> list:
    ac = ac or create_admin_client()
    try:
        rows = (
            ac.table(TABLE).select("*")
            .eq("teacher_id", teacher_id).is_("archived_at", "null")
            .order("created_at", desc=True).limit(limit).execute().data or []
        )
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_agent_memory] load_active_notes failed: {e}")
        return []
    return [_to_app(r) for r in rows]


async def save_note(teacher_id: str, school_id: str, content: str, ac=None) -> dict:
    """Reconciles against existing active notes before writing (see
    memory_reconcile.py) — a NOOP is dropped rather than stored, an UPDATE/
    DELETE archives the note it supersedes first, so a changed preference
    replaces the old one instead of both sitting in the prompt forever,
    contradicting each other."""
    content = " ".join((content or "").split())[:MAX_NOTE_LENGTH]
    if not content:
        return {"error": "Nothing to remember — the note was empty."}
    ac = ac or create_admin_client()

    existing = load_active_notes(teacher_id, ac=ac)
    decision = await reconcile_note(existing, content)
    if decision["action"] == "noop":
        return {"saved": False, "reason": "Already remembered — nothing changed."}
    if decision["action"] in ("update", "delete"):
        archive_note_by_id(teacher_id, decision["targetId"], ac=ac)
    if decision["action"] == "delete":
        return {"saved": False, "forgotten": True, "reason": "Replaced an earlier note; nothing new to add."}

    payload = {"teacher_id": teacher_id, "school_id": school_id, "content": content, "created_at": _now()}
    try:
        row = (ac.table(TABLE).insert(payload).execute().data or [None])[0]
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_agent_memory] save_note failed: {e}")
        return {"saved": False}
    result = {"saved": True, "note": _to_app(row) if row else {"content": content}}
    if decision["action"] == "update":
        result["updated"] = True
    return result


def archive_note_by_id(teacher_id: str, note_id: str, ac=None) -> bool:
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).update({"archived_at": _now()}) \
            .eq("id", note_id).eq("teacher_id", teacher_id).execute()
        return True
    except Exception as e:
        if not _table_missing(e):
            print(f"[teacher_agent_memory] archive_note_by_id failed: {e}")
        return False


def archive_note_by_ref(teacher_id: str, note_ref: str, ac=None) -> dict:
    """Same found/ambiguous/none contract teacher_agent.py's _match_student
    already uses — the agent's own forget_note tool only ever has the
    teacher's words to go on, not an id."""
    ref = (note_ref or "").strip().lower()
    if not ref:
        return {"kind": "none"}
    ac = ac or create_admin_client()
    notes = load_active_notes(teacher_id, limit=50, ac=ac)
    hits = [n for n in notes if ref in n["content"].lower()]
    if not hits:
        return {"kind": "none"}
    if len(hits) > 1:
        return {"kind": "ambiguous", "candidates": [n["content"] for n in hits]}
    archive_note_by_id(teacher_id, hits[0]["id"], ac=ac)
    return {"kind": "found", "note": hits[0]}
