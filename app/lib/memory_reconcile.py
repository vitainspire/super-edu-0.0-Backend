"""Decides what a newly stated preference/fact does to an agent's existing
notes memory, BEFORE it's written — the ADD/UPDATE/DELETE/NOOP step neither
agent_notes.py nor teacher_agent_memory.py had before this. Without it, a
changed preference just piles up as a second, contradicting row forever: an
admin saying "prefer same-department substitutes" and later "cross-department
is fine now" used to produce two permanent notes, both read into every future
prompt with no sign which one is current. Shared by both tables rather than
duplicated, since the decision logic doesn't depend on which one is asking —
only the note list and the candidate text matter.

ONE CHEAP MODEL CALL, NOT A VECTOR SEARCH. Both tables cap their active notes
at 15 (MAX_ACTIVE_NOTES in each), so pasting the whole list into one
comparison prompt is simpler and cheaper than standing up embeddings or a
vector store for a list this small — see the design discussion this followed
(Mem0's own architecture needs that machinery at a scale — hundreds of notes,
cross-entity reasoning — this app isn't at).
"""
import json
import os

from .ai import call_ai

RECONCILE_MODEL = os.environ.get("MEMORY_RECONCILE_MODEL", "anthropic/claude-haiku-4.5")

_PROMPT = """You maintain a short list of remembered notes for an AI assistant. A new
statement just came in that may need to be saved. Decide exactly one action:

- "add": a genuinely new fact/preference, unrelated to anything below.
- "update": this replaces or changes one existing note below (same topic, a
  different answer now) — name which one by its id.
- "delete": this statement CONTRADICTS or CANCELS an existing note below and
  there is nothing further worth remembering beyond that cancellation — name
  which one by its id.
- "noop": this is already covered by an existing note below, word for word
  or in substance — nothing needs to change.

EXISTING NOTES:
{existing}

NEW STATEMENT: "{candidate}"

Return ONLY valid JSON: {{"action": "add"|"update"|"delete"|"noop", "targetId": "<id, only for update/delete, else null>", "reason": "one short phrase"}}"""


async def reconcile_note(existing_notes: list[dict], candidate: str) -> dict:
    """{"action", "targetId"}. Never raises — any failure (bad JSON, model
    error) defaults to "add", since inserting a redundant note is a far
    smaller harm than silently dropping or archiving the wrong one.

    targetId is None unless action is update/delete AND it names a real id
    from `existing_notes` — a hallucinated or missing id is treated as "add"
    instead of risking a wrong note being archived."""
    if not existing_notes:
        return {"action": "add", "targetId": None}

    existing_text = "\n".join(f'- id={n["id"]}: "{n["content"]}"' for n in existing_notes)
    prompt = _PROMPT.format(existing=existing_text, candidate=candidate)
    try:
        raw = await call_ai(
            [{"role": "user", "content": prompt}],
            {"model": RECONCILE_MODEL, "temperature": 0, "max_tokens": 300, "json_mode": True},
        )
        decision = json.loads(raw)
    except Exception as e:
        print(f"[memory_reconcile] reconcile call failed, defaulting to add: {e}")
        return {"action": "add", "targetId": None}

    action = decision.get("action")
    if action not in ("add", "update", "delete", "noop"):
        return {"action": "add", "targetId": None}

    if action in ("update", "delete"):
        target_id = decision.get("targetId")
        valid_ids = {n["id"] for n in existing_notes}
        if target_id not in valid_ids:
            return {"action": "add", "targetId": None}
        return {"action": action, "targetId": target_id}

    return {"action": action, "targetId": None}
