"""Argus's persistent memory — the state that outlives a single run.

The agent loop's scratchpad is working memory: it exists so step 3 can use
what step 1 found, and it is thrown away when the run ends. That is enough to
answer a question and not nearly enough to *run* anything. An agent that
sweeps for problems every morning needs to know what it already said, or it
reports the same six unstaffed classes daily until the admin stops reading.

So findings are keyed by the problem, not by the sighting: a stable
`finding_key` derived from the same inputs every sweep, upserted on each
detection. That gives three things the loop alone can't:

  - suppression — a key already reported isn't re-reported until its cadence
    is up (see should_report)
  - resolution — a key that stops being detected is closed automatically,
    which is what makes "what got fixed this week" a real question
  - dismissal — an admin can silence a still-present finding, recorded
    separately from resolution so the two never get confused

Every function here degrades to a no-op or an empty read if the table isn't
there yet (migration 0008 unapplied), following the same rule
admin_notifications.py's automation-log uses: a missing audit/memory table
should read as "nothing known", never take a feature down.
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from .supabase_clients import create_admin_client

TABLE = "argus_findings"

# How long a still-open finding stays quiet after being reported. Long enough
# that a daily sweep doesn't repeat itself, short enough that something left
# unfixed for days resurfaces rather than being silently forgotten.
REPORT_COOLDOWN_HOURS = 20


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "does not exist" in text or "could not find the table" in text or "42p01" in text


def load_open_findings(school_id: str, ac=None) -> list:
    """Every currently-open finding for this school. Empty list if the table
    isn't there — callers treat that as "no memory yet", not as an error."""
    ac = ac or create_admin_client()
    try:
        rows = (
            ac.table(TABLE).select("*")
            .eq("school_id", school_id).eq("status", "open")
            .order("severity").execute().data or []
        )
    except Exception as e:
        if not _table_missing(e):
            print(f"[argus_memory] load_open_findings failed: {e}")
        return []
    return [_to_app(r) for r in rows]


def _to_app(r: dict) -> dict:
    return {
        "id": r["id"], "findingKey": r["finding_key"], "playbook": r["playbook"],
        "severity": r["severity"], "title": r["title"], "detail": r.get("detail"),
        "suggestedAction": r.get("suggested_action"), "status": r["status"],
        "firstSeenAt": r.get("first_seen_at"), "lastSeenAt": r.get("last_seen_at"),
        "lastReportedAt": r.get("last_reported_at"),
        "resolvedAt": r.get("resolved_at"), "dismissedAt": r.get("dismissed_at"),
    }


def record_findings(school_id: str, findings: list, ac=None) -> dict:
    """Upsert this sweep's detections and close anything that has stopped
    being detected.

    Returns {"new": [...], "recurring": [...], "resolved": [...]} — the split
    the caller needs to decide what is worth telling a human. A finding is
    "new" the first time its key is seen (or the first time since it was
    resolved); "recurring" while it keeps being detected; "resolved" the
    sweep after it disappears.

    Dismissed findings are left dismissed: re-detecting one does not
    un-silence it, otherwise "stop telling me about this" would last exactly
    one sweep.
    """
    ac = ac or create_admin_client()
    result = {"new": [], "recurring": [], "resolved": []}

    try:
        existing_rows = (
            ac.table(TABLE).select("*").eq("school_id", school_id).execute().data or []
        )
    except Exception as e:
        if _table_missing(e):
            # No memory available: every finding reads as new, nothing is
            # suppressed, nothing is persisted. The sweep still works — it
            # just can't be quiet about repeats yet.
            print(f"[argus_memory] {TABLE} missing — run migration 0008 to enable suppression")
            return {"new": list(findings), "recurring": [], "resolved": []}
        print(f"[argus_memory] record_findings read failed: {e}")
        return {"new": list(findings), "recurring": [], "resolved": []}

    by_key = {r["finding_key"]: r for r in existing_rows}
    seen_keys = set()

    for f in findings:
        key = f["findingKey"]
        seen_keys.add(key)
        prior = by_key.get(key)
        payload = {
            "school_id": school_id, "finding_key": key, "playbook": f["playbook"],
            "severity": f.get("severity", "info"), "title": f["title"],
            "detail": f.get("detail"), "suggested_action": f.get("suggestedAction"),
            "status": "open", "last_seen_at": _now(), "updated_at": _now(),
        }

        if prior is None:
            payload["first_seen_at"] = _now()
            try:
                ac.table(TABLE).insert(payload).execute()
            except Exception as e:
                print(f"[argus_memory] insert {key} failed: {e}")
                continue
            result["new"].append({**f, "isNew": True})
            continue

        if prior["status"] == "dismissed":
            # Still record that we saw it, but stay silent about it.
            try:
                ac.table(TABLE).update({"last_seen_at": _now(), "updated_at": _now()}).eq("id", prior["id"]).execute()
            except Exception as e:
                print(f"[argus_memory] touch {key} failed: {e}")
            continue

        reopened = prior["status"] == "resolved"
        if reopened:
            payload["first_seen_at"] = _now()
            payload["resolved_at"] = None
        try:
            ac.table(TABLE).update(payload).eq("id", prior["id"]).execute()
        except Exception as e:
            print(f"[argus_memory] update {key} failed: {e}")
            continue

        enriched = {**f, "isNew": reopened, "lastReportedAt": prior.get("last_reported_at")}
        (result["new"] if reopened else result["recurring"]).append(enriched)

    # Anything previously open that this sweep did not detect is genuinely
    # gone — someone fixed it, here or elsewhere in the app.
    for r in existing_rows:
        if r["status"] == "open" and r["finding_key"] not in seen_keys:
            try:
                ac.table(TABLE).update({
                    "status": "resolved", "resolved_at": _now(), "updated_at": _now(),
                }).eq("id", r["id"]).execute()
            except Exception as e:
                print(f"[argus_memory] resolve {r['finding_key']} failed: {e}")
                continue
            result["resolved"].append(_to_app(r))

    return result


def should_report(finding: dict) -> bool:
    """Whether a finding is worth putting in front of a human on this sweep.

    New findings always are. A recurring one is held quiet until
    REPORT_COOLDOWN_HOURS since it was last reported, so an unfixed problem
    resurfaces on a cadence instead of either spamming daily or vanishing
    after one mention.
    """
    if finding.get("isNew"):
        return True
    last = finding.get("lastReportedAt")
    if not last:
        return True
    try:
        when = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    return datetime.now(timezone.utc) - when >= timedelta(hours=REPORT_COOLDOWN_HOURS)


def mark_reported(school_id: str, finding_keys: list, ac=None) -> None:
    """Stamp the findings actually shown to a human, so the cooldown above
    measures from when it was *said*, not from when it was detected."""
    if not finding_keys:
        return
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).update({"last_reported_at": _now(), "updated_at": _now()}) \
            .eq("school_id", school_id).in_("finding_key", finding_keys).execute()
    except Exception as e:
        if not _table_missing(e):
            print(f"[argus_memory] mark_reported failed: {e}")


def dismiss_finding(school_id: str, finding_key: str, ac=None) -> bool:
    """Silence a finding that is still present. Kept distinct from resolved:
    conflating "don't tell me" with "it's fixed" would make the resolved list
    a lie."""
    ac = ac or create_admin_client()
    try:
        ac.table(TABLE).update({
            "status": "dismissed", "dismissed_at": _now(), "updated_at": _now(),
        }).eq("school_id", school_id).eq("finding_key", finding_key).execute()
        return True
    except Exception as e:
        print(f"[argus_memory] dismiss {finding_key} failed: {e}")
        return False
