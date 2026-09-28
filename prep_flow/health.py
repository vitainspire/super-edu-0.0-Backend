"""Is the feedback loop still alive — and is anyone still feeding it?

Three failure modes, and only the first is visible from the code:

  * **broken** — a state was written but cannot reach the model.
    Caught at write time by `feedback.verify_reaches_prompt`, which refuses to
    activate a state that would not render.
  * **starving** — the loop is fine, but nobody is filling the form. Everything
    stays green: the last state is still active, still correct, still reaching
    the prompt. It is just frozen, and it will stay frozen indefinitely because
    nothing about a healthy-but-unfed loop looks wrong.
  * **stale** — evidence has accumulated but no cycle has run against it. The
    optimizer is not scheduled, or its schedule silently stopped.

This module is only about the second and third. They are the ones that produce no
error, no log line and no failed request — which is exactly why they need a
metric rather than an exception.
"""
from datetime import datetime, timezone
from typing import Optional

from . import db
from .agents.feedback import MIN_SAMPLE
from .deps import env_int

# No response for this long means the teachers have stopped, whatever the totals
# say. Two weeks is roughly ten teaching days — long enough to absorb a holiday,
# short enough that a term does not end before anyone notices.
SILENT_DAYS = env_int("PREP_FLOW_SILENT_DAYS", 14)

# Enough new evidence to justify a cycle. Set at the same floor the optimizer
# itself refuses to act below, so "stale" never fires for a window the optimizer
# would decline anyway.
STALE_RESPONSES = MIN_SAMPLE

# A cycle this old, with fresh evidence waiting, means the schedule has stopped.
STALE_DAYS = env_int("PREP_FLOW_STALE_DAYS", 21)


def _age_days(timestamp: Optional[str]) -> Optional[float]:
    if not timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return round((datetime.now(timezone.utc) - parsed).total_seconds() / 86400, 1)


def classify(pulse: dict) -> dict:
    """Turn the raw pulse into a status and a reason a human can act on.

    Statuses are ordered by how much they should worry someone:
    `broken` > `silent` > `stale` > `warming` > `healthy`.
    """
    responses = pulse.get("responsesInWindow") or 0
    ever = pulse.get("responsesEver") or 0
    since_response = _age_days(pulse.get("lastResponseAt"))
    since_cycle = _age_days(pulse.get("lastCycleAt"))
    version = pulse.get("activeVersion")
    state = pulse.get("activeState") or {}

    directives = len(state.get("sectionDirectives") or {})
    lists = len(state.get("prefer") or []) + len(state.get("avoid") or [])
    weights = len(state.get("activityCategoryWeights") or {})
    shift = state.get("difficultyShift") not in (None, "none")
    has_content = bool(directives or lists or weights or shift)

    issues: list[str] = []
    status = "healthy"

    if version is not None and not has_content:
        # A version exists and carries nothing. Legal after a quiet window, but
        # indistinguishable from a state whose content was silently dropped —
        # which is precisely the bug this loop already shipped once.
        status = "broken"
        issues.append(
            f"adaptive state v{version} is active but carries no directives, "
            f"lists, weights or difficulty shift — generation is running exactly "
            f"as it would with no state at all")

    elif ever == 0:
        status = "warming"
        issues.append("no feedback has ever been recorded for this cohort — the "
                      "loop has nothing to learn from yet")

    elif since_response is not None and since_response > SILENT_DAYS:
        status = "silent"
        issues.append(
            f"no teacher response for {since_response:.0f} days (threshold "
            f"{SILENT_DAYS}) — the form may be unreachable, or teachers have "
            f"stopped filling it. The active state will never change again.")

    elif responses < STALE_RESPONSES:
        status = "warming"
        issues.append(
            f"{responses} response(s) in the last {pulse.get('windowDays')} days; "
            f"{STALE_RESPONSES} are needed before the optimizer will act")

    elif since_cycle is None:
        status = "stale"
        issues.append(
            f"{responses} responses are waiting but no optimization cycle has ever "
            f"run — POST /api/prep-flow/feedback/optimize, and put it on a schedule")

    elif since_cycle > STALE_DAYS:
        status = "stale"
        issues.append(
            f"{responses} responses in the window but the last cycle was "
            f"{since_cycle:.0f} days ago (threshold {STALE_DAYS}) — the schedule "
            f"has probably stopped")

    return {
        **pulse,
        "status": status,
        "issues": issues,
        "daysSinceLastResponse": since_response,
        "daysSinceLastCycle": since_cycle,
        "stateContent": {"directives": directives, "listItems": lists,
                         "weights": weights, "difficultyShift": shift},
        "actionable": status in ("broken", "silent", "stale"),
    }


async def feedback_health(school_id: Optional[str], grade: str, subject: str,
                          window_days: int = 40) -> dict:
    """One cohort's loop health."""
    key = db.scope_key(school_id, grade, subject)
    return classify(await db.feedback_pulse(key, window_days))


async def feedback_health_many(cohorts: list[dict], window_days: int = 40) -> dict:
    """Health across several cohorts, worst first.

    Sorted by severity rather than by name because the list exists to be scanned:
    a dashboard that puts a silent cohort below eleven healthy ones is a
    dashboard nobody reads to the bottom of.
    """
    order = {"broken": 0, "silent": 1, "stale": 2, "warming": 3, "healthy": 4}
    results = []
    for cohort in cohorts:
        results.append(await feedback_health(
            cohort.get("schoolId"), str(cohort.get("grade")), cohort.get("subject"),
            window_days))
    results.sort(key=lambda r: (order.get(r["status"], 9), r["scopeKey"]))
    return {
        "cohorts": results,
        "summary": {status: sum(1 for r in results if r["status"] == status)
                    for status in order},
        "needAttention": [r["scopeKey"] for r in results if r["actionable"]],
    }
