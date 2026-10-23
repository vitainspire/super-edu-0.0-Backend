"""The Teacher Agent's own playbooks — what makes "My Agent" act like a
personal secretary instead of only answering when asked. Same split as
app/lib/argus_sweep.py (deterministic detection, LLM only for the closing
phrasing) scoped down from a whole school to one teacher's own classes; this
module reuses that file's generic helpers (_finding, _phrase_briefing,
_plain_briefing, SEVERITY_ORDER) rather than duplicating them, and reuses its
_pb_syllabus_pace directly via the class_ids filter that function now takes.

Every finding's findingKey is prefixed `teacher:{teacher_id}:` before it
reaches argus_memory, which stores all findings for a school in one table
keyed only by (school_id, finding_key). Without that prefix, a teacher-scoped
finding and the Headmaster's school-wide version of the same real-world
problem (e.g. syllabus_pace for the same class) would collide on one row —
whichever persona swept first would mark it "reported" and the other would
silently never mention it. The prefix keeps every persona's "have I already
said this" bookkeeping independent, same way thread+teacher_id already keeps
chat messages independent per teacher.
"""
from datetime import datetime, timedelta, timezone

from .supabase_clients import create_admin_client
from .argus_sweep import _finding, _phrase_briefing, _plain_briefing, SEVERITY_ORDER, _pb_syllabus_pace
from .argus_memory import record_findings, should_report, mark_reported
from .admin_queries import fetch_teacher_availability_for_teacher
from .logger import api_log

LOOKBACK_DAYS_ATTENDANCE = 7
ATTENDANCE_CONCERN_THRESHOLD = 3
LOOKBACK_DAYS_LEAVE = 14


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _prefixed(teacher_id: str, findings: list) -> list:
    out = []
    for f in findings:
        out.append({**f, "findingKey": f"teacher:{teacher_id}:{f['findingKey']}"})
    return out


# ── pending_doubts ────────────────────────────────────────────────────────
# No suggestedAction: the content of an answer requires judgment, same
# restraint argus_sweep._pb_operations already uses for the school-wide
# "N doubts pending" finding.
def _pb_pending_doubts(teacher_id: str, ctx: dict, date_str: str, ac) -> list:
    class_ids = ctx["classIds"]
    if not class_ids:
        return []
    rows = (
        ac.table("student_doubts").select("id, student_name, subject, question, class_id")
        .in_("class_id", class_ids).eq("status", "pending").execute().data or []
    )
    if not rows:
        return []
    findings = []
    for r in rows:
        who = r.get("student_name") or "A student"
        subject = f' in {r["subject"]}' if r.get("subject") else ""
        findings.append(_finding(
            "pending_doubts", f'doubts:{r["id"]}', "attention",
            f'{who} is still waiting on an answer{subject}',
            r.get("question"),
        ))
    return _prefixed(teacher_id, findings)


# ── pending_marks ─────────────────────────────────────────────────────────
# Server-side version of components/nav/SideNav.tsx's pendingTestCount badge:
# a conducted test whose entered marks don't yet cover every active student
# in that class.
def _pb_pending_marks(teacher_id: str, ctx: dict, date_str: str, ac) -> list:
    tests = (
        ac.table("tests").select("id, class_id, subject, topic, conducted_on")
        .eq("teacher_id", teacher_id).lte("conducted_on", date_str).execute().data or []
    )
    if not tests:
        return []
    class_ids = {t["class_id"] for t in tests if t.get("class_id")}
    if not class_ids:
        return []

    students = (
        ac.table("students").select("id, class_id").in_("class_id", list(class_ids))
        .eq("is_active", True).execute().data or []
    )
    enrolled_by_class: dict = {}
    for s in students:
        enrolled_by_class.setdefault(s["class_id"], set()).add(s["id"])

    test_ids = [t["id"] for t in tests]
    marks = ac.table("marks").select("test_id, student_id").in_("test_id", test_ids).execute().data or []
    entered_by_test: dict = {}
    for m in marks:
        entered_by_test.setdefault(m["test_id"], set()).add(m["student_id"])

    findings = []
    for t in tests:
        enrolled = enrolled_by_class.get(t.get("class_id"), set())
        if not enrolled:
            continue
        entered = entered_by_test.get(t["id"], set())
        missing = len(enrolled - entered)
        if missing == 0:
            continue
        label = t.get("topic") or t.get("subject") or "a test"
        findings.append(_finding(
            "pending_marks", f'marks:{t["id"]}', "attention",
            f'{missing} mark(s) still pending for "{label}" ({t.get("conducted_on")})',
            f'{len(entered)}/{len(enrolled)} students entered so far.',
        ))
    return _prefixed(teacher_id, findings)


# ── attendance_concern ────────────────────────────────────────────────────
# Deterministic threshold, same style as _pb_syllabus_pace's 30% cutoff: a
# student absent 3+ times in the last 7 days is worth a look, not an
# auto-fix — flags for the teacher to add their own intervention note.
def _pb_attendance_concern(teacher_id: str, ctx: dict, date_str: str, ac) -> list:
    class_ids = ctx["classIds"]
    if not class_ids:
        return []
    since = (datetime.fromisoformat(date_str) - timedelta(days=LOOKBACK_DAYS_ATTENDANCE)).date().isoformat()
    rows = (
        ac.table("attendance").select("student_id, status, date")
        .in_("class_id", class_ids).eq("status", "absent").gte("date", since).execute().data or []
    )
    if not rows:
        return []
    counts: dict = {}
    for r in rows:
        counts[r["student_id"]] = counts.get(r["student_id"], 0) + 1
    flagged = {sid: n for sid, n in counts.items() if n >= ATTENDANCE_CONCERN_THRESHOLD}
    if not flagged:
        return []

    students = ac.table("students").select("id, name").in_("id", list(flagged.keys())).execute().data or []
    name_by_id = {s["id"]: s["name"] for s in students}

    findings = []
    for sid, n in flagged.items():
        name = name_by_id.get(sid, "A student")
        findings.append(_finding(
            "attendance_concern", f'attendance:{sid}', "attention",
            f'{name} has been absent {n} time(s) in the last {LOOKBACK_DAYS_ATTENDANCE} days',
            "Worth an intervention note or a check-in.",
        ))
    return _prefixed(teacher_id, findings)


# ── syllabus_pace ─────────────────────────────────────────────────────────
# Reuses argus_sweep._pb_syllabus_pace wholesale, scoped to this teacher's
# own classes via its class_ids param, then re-keyed with the teacher prefix.
def _next_incomplete_topic(class_id: str, subject: str, ac) -> dict:
    rows = (
        ac.table("syllabus_topics").select("topic")
        .eq("class_id", class_id).eq("subject", subject).eq("is_completed", False)
        .order("order_index").limit(1).execute().data or []
    )
    return rows[0] if rows else None


def _pb_syllabus_pace_teacher(teacher_id: str, ctx: dict, date_str: str, ac) -> list:
    if not ctx["schoolId"] or not ctx["classIds"]:
        return []
    findings = _pb_syllabus_pace(ctx["schoolId"], date_str, ac, class_ids=ctx["classIds"])
    if not findings:
        return []

    # Proactive half of below-grade-level prep material: a class genuinely
    # behind pace is exactly the signal worth turning into a one-tap
    # "generate simpler material for what's next" action, through the same
    # generate_prep_material propose/confirm path a teacher would use by
    # asking directly — this just resolves the proposal for them.
    class_names = {
        c["id"]: c["name"]
        for c in (ac.table("classes").select("id, name").in_("id", ctx["classIds"]).execute().data or [])
    }
    for f in findings:
        # findingKey format: "syllabus_pace:{class_id}:{subject}"
        _, class_id, subject = f["findingKey"].split(":", 2)
        next_topic = _next_incomplete_topic(class_id, subject, ac)
        class_name = class_names.get(class_id)
        if not next_topic or not class_name:
            continue
        f["suggestedAction"] = {
            "tool": "generate_prep_material",
            "args": {
                "classRef": class_name, "subject": subject, "topic": next_topic["topic"],
                "belowGradeLevel": True,
            },
            "label": f'Generate simpler prep material for "{next_topic["topic"]}" ({class_name}, {subject})',
        }

    return _prefixed(teacher_id, findings)


# ── leave_status ──────────────────────────────────────────────────────────
# Not "a problem that resolves" like the other playbooks — a one-time
# notification. The status is baked into the finding key itself
# (teacher:{tid}:leave:{row_id}:approved vs ...:rejected), so a status change
# produces a genuinely new key and reports exactly once; a still-pending
# request produces no finding at all (nothing to tell the teacher yet).
def _pb_leave_status(teacher_id: str, ctx: dict, date_str: str, ac) -> list:
    since = (datetime.fromisoformat(date_str) - timedelta(days=LOOKBACK_DAYS_LEAVE)).date().isoformat()
    rows = fetch_teacher_availability_for_teacher(teacher_id, since, ac)
    findings = []
    for r in rows:
        status = r.get("status")
        if status not in ("approved", "rejected"):
            continue
        verb = "approved" if status == "approved" else "rejected"
        findings.append(_finding(
            "leave_status", f'leave:{r["id"]}:{status}', "info",
            f'Your leave request for {r["date"]} was {verb}',
            r.get("note"),
        ))
    return _prefixed(teacher_id, findings)


PLAYBOOKS = {
    "pending_doubts": _pb_pending_doubts,
    "pending_marks": _pb_pending_marks,
    "attendance_concern": _pb_attendance_concern,
    "syllabus_pace": _pb_syllabus_pace_teacher,
    "leave_status": _pb_leave_status,
}


def detect(teacher_id: str, ctx: dict, playbooks: list = None, date_str: str = None) -> list:
    ac = create_admin_client()
    date_str = date_str or _today()
    names = playbooks or list(PLAYBOOKS.keys())

    findings = []
    for name in names:
        fn = PLAYBOOKS.get(name)
        if not fn:
            continue
        try:
            findings.extend(fn(teacher_id, ctx, date_str, ac))
        except Exception as e:
            print(f"[teacher_sweep] playbook {name} failed: {e}")
    findings.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 3))
    return findings


async def run_teacher_sweep(
    teacher_id: str, ctx: dict, playbooks: list = None, date_str: str = None,
    ip: str = "sweep", phrase: bool = True,
) -> dict:
    """Same pipeline as argus_sweep.run_sweep (detect -> reconcile against
    memory -> report only what's new or due for its cooldown), parallel
    rather than shared because the playbook signature differs (teacher_id,
    ctx vs school_id). argus_memory itself is generic — it only needs a
    school_id and a finding_key, both of which this module supplies."""
    t0 = datetime.now(timezone.utc)
    school_id = ctx["schoolId"]
    if not school_id:
        return {"status": "swept", "briefing": "", "reported": [], "actionable": []}

    detected = detect(teacher_id, ctx, playbooks, date_str)
    # key_prefix isolates this teacher's own rows from every other teacher's
    # and from argus_sweep.py's school-wide rows — every finding this module
    # produces is already prefixed teacher:{teacher_id}:, so this alone is
    # enough scope; no playbooks filter needed (see
    # argus_memory.record_findings's docstring).
    diff = record_findings(school_id, detected, key_prefix=f"teacher:{teacher_id}:")

    candidates = diff["new"] + diff["recurring"]
    reportable = [f for f in candidates if should_report(f)]
    reportable.sort(key=lambda f: SEVERITY_ORDER.get(f.get("severity"), 3))

    briefing = await _phrase_briefing(reportable, diff["resolved"]) if phrase else _plain_briefing(reportable, diff["resolved"])
    mark_reported(school_id, [f["findingKey"] for f in reportable])

    api_log("teacher-sweep", ip, (datetime.now(timezone.utc) - t0).total_seconds() * 1000, False, "ok")
    return {
        "status": "swept", "date": date_str or _today(), "briefing": briefing,
        "reported": reportable, "resolved": diff["resolved"],
        "actionable": [f for f in reportable if f.get("suggestedAction")],
    }
