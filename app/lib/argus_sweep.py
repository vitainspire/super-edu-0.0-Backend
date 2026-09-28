"""Argus's playbooks — the standing goals that make it *run* admin work
rather than only answer questions about it.

A question-answering run lets the model choose which tools to call, because
the question isn't known in advance. A sweep is the opposite: "is every class
covered this morning" always looks at the same things in the same order, so
letting an LLM re-derive that sequence every day would buy nothing and cost
a call per step. So detection here is fully deterministic — plain reads and
plain comparisons — and the model is used for exactly one optional job at the
end: phrasing the briefing in simple English.

That split has a property worth stating: a sweep still works with the LLM
completely unavailable. It returns structured findings; only the prose
summary degrades. An operations agent whose morning check silently stops
because a model endpoint is down is not an operations agent.

Each finding carries a stable `findingKey` so argus_memory can tell a new
problem from one it already reported (see that module's docstring), and,
where the fix is derivable from data the system holds, a `suggestedAction`
naming the confirm-gated tool that would resolve it. Storing a proposed
action is not taking it: POST /argus/confirm is still the only write path.
"""
from datetime import datetime, timezone

from .ai import call_ai
from .supabase_clients import create_admin_client
from .admin_queries import (
    fetch_school_classes, fetch_school_teachers, fetch_pending_leave_requests,
    fetch_substitutions_for_date,
)
from .argus_agent import TOOLS, _today
from .argus_memory import record_findings, should_report, mark_reported
from .logger import api_log


def _finding(playbook: str, key: str, severity: str, title: str, detail: str = None, action: dict = None) -> dict:
    return {
        "playbook": playbook, "findingKey": key, "severity": severity,
        "title": title, "detail": detail, "suggestedAction": action,
    }


# ── coverage ────────────────────────────────────────────────────────────────
# The one an admin must see before the first bell: a period whose teacher is
# away and for whom nobody has been found.
def _pb_coverage(school_id: str, date_str: str, ac) -> list:
    subs = fetch_substitutions_for_date(school_id, date_str, ac)
    unresolved = [s for s in subs if s.get("status") == "unresolved"]
    if not unresolved:
        return []

    class_name = {c["id"]: (c.get("name") or f'{c.get("grade")} {c.get("section")}') for c in fetch_school_classes(school_id, ac)}
    findings = []
    for gap in unresolved:
        label = class_name.get(gap["classId"], "a class")
        subject = f' ({gap["subject"]})' if gap.get("subject") else ""

        # Reuse the real matcher rather than re-implementing it: if a
        # qualified, under-cap teacher exists, the finding arrives with a
        # ready-to-confirm fix attached.
        action = None
        proposed = TOOLS["assign_substitute"]["fn"](school_id, {"date": date_str, "substitutionId": gap["id"]})
        if "proposal" in proposed:
            action = {"tool": "assign_substitute", "args": proposed["proposal"], "label": proposed["label"]}

        findings.append(_finding(
            "coverage",
            f'coverage:gap:{gap["id"]}',
            "urgent",
            f'{label} period {gap["periodNumber"]}{subject} has no cover on {date_str}',
            "A qualified teacher is available — confirm to assign." if action
            else "No qualified teacher is free without breaching a workload cap; a schedule swap may be needed.",
            action,
        ))
    return findings


# ── readiness ───────────────────────────────────────────────────────────────
# Counts of the gap, not of what exists: "10 classes" reads healthy while 6 of
# them have nobody teaching them. Reported per class, since the fix is
# per class.
def _pb_readiness(school_id: str, date_str: str, ac) -> list:
    classes = fetch_school_classes(school_id, ac)
    if not classes:
        return []
    class_ids = [c["id"] for c in classes]

    assignments = ac.table("teacher_class_assignments").select("class_id").in_("class_id", class_ids).execute().data or []
    students = ac.table("students").select("class_id").in_("class_id", class_ids).execute().data or []
    with_teacher = {a["class_id"] for a in assignments}
    with_students = {s["class_id"] for s in students}

    findings = []
    # Proposals in one sweep have to know about each other: six unstaffed
    # classes each independently picking "the teacher with the lightest load"
    # picks the same person six times, and confirming them all would hand one
    # teacher every class. Each teacher proposed here is spoken for.
    spoken_for: set = set()

    for c in classes:
        label = c.get("name") or f'{c.get("grade")} {c.get("section")}'

        if c["id"] not in with_teacher:
            # Derivable fix: rank the roster and attach the best match.
            action = None
            proposed = TOOLS["assign_teacher_to_class"]["fn"](
                school_id, {"classRef": label, "excludeTeacherIds": list(spoken_for)},
            )
            if "proposal" in proposed:
                action = {"tool": "assign_teacher_to_class", "args": proposed["proposal"], "label": proposed["label"]}
                spoken_for.add(proposed["proposal"]["teacherId"])
            findings.append(_finding(
                "readiness", f'readiness:no_teacher:{c["id"]}', "attention",
                f"{label} has no teacher assigned",
                "Nobody can open this class on the teacher portal.", action,
            ))

        if c["id"] not in with_students:
            # Deliberately no suggestedAction: which students belong in which
            # section isn't derivable from anything the system holds, so this
            # is a flag for a human, not a proposal.
            findings.append(_finding(
                "readiness", f'readiness:no_students:{c["id"]}', "attention",
                f"{label} has no students enrolled",
                "No attendance, marks or reports are possible until someone is enrolled. Enrolment is an admin decision — Argus can't infer who belongs here.",
            ))
    return findings


# ── approvals ───────────────────────────────────────────────────────────────
def _pb_approvals(school_id: str, date_str: str, ac) -> list:
    rows = fetch_pending_leave_requests(school_id, ac)
    if not rows:
        return []
    name_by_id = {t["id"]: t["name"] for t in fetch_school_teachers(school_id, ac)}

    by_teacher: dict = {}
    for r in rows:
        by_teacher.setdefault(r["teacherId"], []).append(r["date"])

    findings = []
    for teacher_id, dates in by_teacher.items():
        dates.sort()
        span = dates[0] if len(dates) == 1 else f"{dates[0]} to {dates[-1]}"
        urgent = dates[0] <= date_str
        # No suggestedAction on purpose: whether a leave *should* be approved
        # is an HR judgment, not something derivable from the roster. Argus
        # surfaces that a decision is waiting; the admin makes it.
        findings.append(_finding(
            "approvals", f'approvals:leave:{teacher_id}:{dates[0]}',
            "urgent" if urgent else "attention",
            f'{name_by_id.get(teacher_id, "A teacher")} has leave pending approval for {span}',
            "Cover is only computed once this is approved, so an unapproved request close to its date risks an uncovered period."
            if urgent else "Waiting on an approve/reject decision.",
        ))
    return findings


# ── operations ──────────────────────────────────────────────────────────────
def _pb_operations(school_id: str, date_str: str, ac) -> list:
    from ..routes.admin_misc import get_overview as _admin_overview
    ops = (_admin_overview(school_id) or {}).get("operations") or {}
    findings = []

    if ops.get("attendanceRecords", 0) == 0:
        findings.append(_finding(
            "operations", "operations:no_attendance", "attention",
            "No attendance recorded anywhere in the last 7 days",
            "Attendance has to be marked by teachers in class — Argus can flag this but cannot fill it in.",
        ))

    pending = ops.get("pendingDoubts", 0)
    if pending:
        findings.append(_finding(
            "operations", "operations:pending_doubts", "info",
            f"{pending} student doubt(s) still waiting on a reply",
            "Answering these is a teacher's job on the teacher portal; this is visibility only.",
        ))
    return findings


PLAYBOOKS = {
    "coverage": _pb_coverage,
    "readiness": _pb_readiness,
    "approvals": _pb_approvals,
    "operations": _pb_operations,
}

SEVERITY_ORDER = {"urgent": 0, "attention": 1, "info": 2}


def detect(school_id: str, playbooks: list = None, date_str: str = None) -> list:
    """Run playbooks and return findings. Fully deterministic — no LLM, no
    writes. Safe to call as often as you like."""
    ac = create_admin_client()
    date_str = date_str or _today()
    names = playbooks or list(PLAYBOOKS.keys())

    findings = []
    for name in names:
        fn = PLAYBOOKS.get(name)
        if not fn:
            continue
        try:
            findings.extend(fn(school_id, date_str, ac))
        except Exception as e:
            print(f"[argus_sweep] playbook {name} failed: {e}")
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 3))
    return findings


def _plain_briefing(reportable: list, resolved: list) -> str:
    """The summary used when no LLM is available (or it fails). Deliberately
    boring and always correct — it only restates counts and titles."""
    if not reportable and not resolved:
        return "Nothing needs your attention right now."
    parts = []
    urgent = [f for f in reportable if f.get("severity") == "urgent"]
    if urgent:
        parts.append(f"{len(urgent)} urgent: " + "; ".join(f["title"] for f in urgent[:3]))
    others = [f for f in reportable if f.get("severity") != "urgent"]
    if others:
        parts.append(f"{len(others)} to look at: " + "; ".join(f["title"] for f in others[:3]))
    if resolved:
        parts.append(f"{len(resolved)} cleared since the last check")
    return ". ".join(parts) + "."


async def _phrase_briefing(reportable: list, resolved: list) -> str:
    lines = "\n".join(
        f'- [{f.get("severity")}] {f["title"]}' + (f' (fix ready: {f["suggestedAction"]["label"]})' if f.get("suggestedAction") else "")
        for f in reportable
    ) or "- nothing new"
    prompt = f"""You are Argus, briefing a school admin on what needs attention.

Findings detected (these are facts from the school's own records — do not add
anything not listed, do not invent numbers):
{lines}
Cleared since the last check: {len(resolved)}

Write 2-4 short sentences in very simple English telling the admin what
matters most first, and mention when a fix is ready to confirm. Return JSON
only: {{"briefing": "<text>"}}"""
    try:
        import json
        raw = await call_ai([{"role": "user", "content": prompt}])
        text = (json.loads(raw) or {}).get("briefing")
        return text or _plain_briefing(reportable, resolved)
    except Exception as e:
        print(f"[argus_sweep] briefing phrasing unavailable, using plain summary: {e}")
        return _plain_briefing(reportable, resolved)


async def run_sweep(
    school_id: str, playbooks: list = None, date_str: str = None,
    ip: str = "sweep", phrase: bool = True,
) -> dict:
    """A full proactive pass: detect deterministically, reconcile against
    memory, then report only what a human hasn't already been told.

    This is the entrypoint a schedule or an event handler calls. It writes
    nothing except Argus's own findings memory — every actual fix it finds is
    attached as a proposal for POST /argus/confirm.
    """
    t0 = datetime.now(timezone.utc)
    detected = detect(school_id, playbooks, date_str)
    diff = record_findings(school_id, detected)

    candidates = diff["new"] + diff["recurring"]
    reportable = [f for f in candidates if should_report(f)]
    reportable.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 3))

    briefing = await _phrase_briefing(reportable, diff["resolved"]) if phrase else _plain_briefing(reportable, diff["resolved"])
    mark_reported(school_id, [f["findingKey"] for f in reportable])

    api_log("argus-sweep", ip, (datetime.now(timezone.utc) - t0).total_seconds() * 1000, False, "ok")
    return {
        "status": "swept",
        "date": date_str or _today(),
        "briefing": briefing,
        "counts": {
            "detected": len(detected), "new": len(diff["new"]),
            "recurring": len(diff["recurring"]), "resolved": len(diff["resolved"]),
            "reported": len(reportable),
        },
        # Everything detected, so a UI can show the full picture; `reported`
        # is the subset that isn't being suppressed as already-said.
        "reported": reportable,
        "suppressed": [f for f in candidates if f not in reportable],
        "resolved": diff["resolved"],
        "actionable": [f for f in reportable if f.get("suggestedAction")],
    }
