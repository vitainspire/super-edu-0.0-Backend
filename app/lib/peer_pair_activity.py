"""Mirrors backend/src/lib/peerPairActivity.ts."""
import json
import re
from typing import Optional

from .ai import call_ai

FALLBACK_ACTIVITY = "Take turns explaining one tricky problem to each other and see what clicks."


async def generate_peer_activity(subject: Optional[str], name_a: str, name_b: str, shared_interest: Optional[str] = None) -> str:
    """Generates a short, concrete activity for two students who just became
    study buddies. Best-effort — a generation failure falls back to a generic
    activity rather than blocking the pairing from being confirmed."""
    subject_line = f"Subject: {subject}" if subject else "General study partnership (no single subject)."
    interest_line = f"They both like: {shared_interest}." if shared_interest else ""

    prompt = f"""Two schoolchildren, {name_a} and {name_b}, just became study buddies.
{subject_line}
{interest_line}

Suggest ONE short, concrete activity they can do together in class, in one simple sentence (max 15 words). Make it specific and fun, not generic advice like "help each other."

Return ONLY valid JSON: {{ "activity": "string" }}"""

    try:
        text = await call_ai([{"role": "user", "content": prompt}], {"max_tokens": 100, "timeout_s": 20})
        cleaned = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        cleaned = re.sub(r"\s*```$", "", cleaned, flags=re.IGNORECASE).strip()
        parsed = json.loads(cleaned)
        activity = parsed.get("activity")
        return activity.strip() if isinstance(activity, str) and activity.strip() else FALLBACK_ACTIVITY
    except Exception:
        return FALLBACK_ACTIVITY
