"""Read-only record of agent-to-agent communication, shown in the admin
portal's "Agent to Agent" tab — see app/lib/argus_agent.py's
message_admin_agent/message_headmaster_agent/invoke_teacher_agent tools,
the only three writers of this table. Same graceful-degrade-on-missing-table
rule every other log in this codebase follows: a missing table reads as
"nothing logged yet", and a failed write is swallowed (never worth failing
the agent's actual answer over).
"""
from datetime import datetime, timezone

from .supabase_clients import create_admin_client

TABLE = "agent_activity_log"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def _to_app(r: dict) -> dict:
    return {
        "id": r["id"], "fromPersona": r["from_persona"], "toLabel": r["to_label"],
        "toTeacherId": r.get("to_teacher_id"), "question": r["question"],
        "answer": r.get("answer"), "status": r["status"], "createdAt": r.get("created_at"),
    }


def record_activity(
    school_id: str, from_persona: str, to_label: str, question: str,
    answer: str = None, status: str = "answered", to_teacher_id: str = None, ac=None,
) -> None:
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).insert({
            "school_id": school_id, "from_persona": from_persona, "to_label": to_label,
            "to_teacher_id": to_teacher_id, "question": question, "answer": answer,
            "status": status, "created_at": _now(),
        }).execute()
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_activity_log] record_activity failed: {e}")


def load_recent_activity(school_id: str, limit: int = 50, ac=None) -> list:
    ac = ac or create_admin_client()
    try:
        rows = (
            ac.table(TABLE).select("*").eq("school_id", school_id)
            .order("created_at", desc=True).limit(limit).execute().data or []
        )
    except Exception as e:
        if not _table_missing(e):
            print(f"[agent_activity_log] load_recent_activity failed: {e}")
        return []
    return [_to_app(r) for r in rows]
