"""Durable record of every action the Headmaster/Admin Agent has actually
carried out on its own — the "auto" tier of tools (see PERSONAS in
argus_agent.py) that execute immediately instead of waiting on a human
confirm. Two jobs this makes possible that the agent loop's scratchpad alone
can't (scratchpad is thrown away the moment a turn ends):

  - an admin can ask "what have you done today" and get a real answer instead
    of the model guessing from its own prior chat messages
  - the agent itself can see what it already did this run/session and avoid
    re-proposing or re-describing the same fix

Same graceful-degrade rule every other table-backed module here follows: a
missing table reads as "nothing logged yet", never an error.
"""
from datetime import datetime, timezone

from .supabase_clients import create_admin_client

TABLE = "agent_action_log"

# Read into every run_agent prompt — capped so a busy day doesn't crowd out
# the actual question, same reasoning agent_notes.MAX_ACTIVE_NOTES uses.
MAX_RECENT_ACTIONS = 10


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _to_app(r: dict) -> dict:
    return {
        "id": r["id"], "persona": r["persona"], "tool": r["tool"], "label": r.get("label"),
        "status": r["status"], "source": r.get("source"), "createdAt": r.get("created_at"),
    }


def record_action(
    school_id: str, persona: str, tool: str, args: dict, label: str,
    status: str, source: str, outcome: dict = None, error: str = None, ac=None,
) -> None:
    """status is "succeeded" or "failed"; source is "chat" (a live agent run
    acted on its own tool call) or "sweep" (a proactive sweep finding acted
    on itself rather than posting a pending_confirmation card)."""
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).insert({
            "school_id": school_id, "persona": persona, "tool": tool, "args": args,
            "label": label, "status": status, "source": source,
            "outcome": outcome, "error": error, "created_at": _now(),
        }).execute()
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_action_log] record_action failed: {e}")


def load_recent_actions(school_id: str, persona: str, limit: int = MAX_RECENT_ACTIONS, ac=None) -> list:
    ac = ac or create_admin_client()
    try:
        rows = (
            ac.table(TABLE).select("*")
            .eq("school_id", school_id).eq("persona", persona)
            .order("created_at", desc=True).limit(limit).execute().data or []
        )
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_action_log] load_recent_actions failed: {e}")
        return []
    return [_to_app(r) for r in rows]
