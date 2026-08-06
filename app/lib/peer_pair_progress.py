"""Mirrors backend/src/lib/peerPairProgress.ts."""
from datetime import datetime, timezone
from typing import Literal, Optional
from dateutil import parser as date_parser

RECHECK_WINDOW_DAYS = 14
IMPROVEMENT_THRESHOLD = 0.05

ProgressStatus = Literal["too_early", "improving", "no_change", "unknown"]


async def compute_avg_mastery(ac, student_id: str, subject: Optional[str] = None) -> Optional[float]:
    """Average mastery for a student, scoped to a subject if one is given, else overall."""
    q = ac.table("student_topic_mastery").select("mastery").eq("student_id", student_id)
    if subject:
        q = q.eq("subject", subject)
    res = q.execute()
    data = res.data or []
    if len(data) == 0:
        return None
    return sum(r["mastery"] for r in data) / len(data)


def _days_since(date_str: str) -> float:
    then = date_parser.isoparse(date_str)
    now = datetime.now(timezone.utc)
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return (now - then).total_seconds() / 86_400


def compute_progress_status(
    responded_at: str,
    baseline_requester: Optional[float],
    baseline_target: Optional[float],
    current_requester: Optional[float],
    current_target: Optional[float],
) -> ProgressStatus:
    """'Improving' if EITHER student moved up by at least the threshold — the
    pairing is meant to help either direction, not just the weaker student."""
    if _days_since(responded_at) < RECHECK_WINDOW_DAYS:
        return "too_early"

    req_delta = current_requester - baseline_requester if baseline_requester is not None and current_requester is not None else None
    tgt_delta = current_target - baseline_target if baseline_target is not None and current_target is not None else None
    if req_delta is None and tgt_delta is None:
        return "unknown"

    improved = (req_delta is not None and req_delta >= IMPROVEMENT_THRESHOLD) or (
        tgt_delta is not None and tgt_delta >= IMPROVEMENT_THRESHOLD
    )
    return "improving" if improved else "no_change"
