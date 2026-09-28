"""Argus's agent loop — phase 1 of the rollout: read-only tools only, no
action tools, no proactive triggers yet. Runs alongside the existing
/ask-intent classifier (admin_misc.py) untouched, as a separate endpoint —
the existing admin assistant keeps working exactly as it does today while
this is tested independently.

Unlike ask-intent, which classifies once and lets hardcoded frontend code
decide what to fetch, this lets the model itself pick which tool to call,
look at the result, and decide whether it needs another one before
answering — the actual reason/act/observe loop. It never generates a fact
itself; every number or status it reports came back from a tool call.
"""
import json
import time
from datetime import datetime, timezone
from typing import Optional

from .ai import call_ai
from .supabase_clients import create_admin_client
from .admin_queries import (
    fetch_school_teachers, fetch_school_classes, fetch_school_announcements,
    fetch_pending_leave_requests, fetch_teacher_availability, fetch_substitutions_for_date,
    fetch_academic_events, fetch_teacher_eligibility_data,
)
from .substitute_finder import find_substitute
from .ask_history import history_block
from .logger import api_log

# Guardrail: hard cap on tool calls per run. A confused chain stops here
# instead of looping indefinitely or running up cost.
MAX_STEPS = 6

LEAVE_REASONS = ("on_leave", "late_arrival", "official_duty", "other")


def _today() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _tool_overview(school_id: str, args: dict) -> dict:
    """Delegates to admin_misc.py's own /overview handler rather than
    recomputing the same counts here. That matters more than the layering:
    an admin reads these numbers off the dashboard and then asks Argus about
    them, so a second implementation that drifted by even one would be worse
    than useless. Imported inside the function (the same way admin_misc.py
    itself imports ask_history) to keep the lib/routes edge loose."""
    from ..routes.admin_misc import get_overview as _admin_overview
    return _admin_overview(school_id)


def _tool_automation_log(school_id: str, args: dict) -> dict:
    """The audit trail the substitutions table can't give: every absence
    recorded, cover assigned, cover cancelled, period left unfilled."""
    from .events import fetch_events
    ac = create_admin_client()
    try:
        events = fetch_events(school_id, ac, date=args.get("date"))
    except Exception as e:
        return {"error": f"automation log unavailable: {e}"}
    return {"events": events[:25]}


def _tool_students(school_id: str, args: dict) -> dict:
    """Search students school-wide by name — the lookup step before
    get_student_record, since records are addressed by id, not name."""
    ac = create_admin_client()
    classes = fetch_school_classes(school_id, ac)
    class_ids = [c["id"] for c in classes]
    if not class_ids:
        return {"students": []}
    class_name = {c["id"]: c.get("name") for c in classes}
    rows = ac.table("students").select("id, name, class_id, student_code").in_("class_id", class_ids).execute().data or []
    name_filter = (args.get("nameContains") or "").strip().lower()
    if name_filter:
        rows = [r for r in rows if name_filter in (r.get("name") or "").lower()]
    return {
        "students": [
            {"id": r["id"], "name": r.get("name"), "className": class_name.get(r["class_id"]), "studentCode": r.get("student_code")}
            for r in rows[:40]
        ],
        "truncated": len(rows) > 40,
    }


def _tool_student_record(school_id: str, args: dict) -> dict:
    """One student's full picture — attendance, marks, mastery, doubts,
    intervention notes. Same handler the admin's own student page uses."""
    student_id = (args.get("studentId") or "").strip()
    if not student_id:
        return {"error": "studentId is required — find it with get_students first."}
    from ..routes.admin_students import get_full_record
    try:
        return get_full_record(school_id, student_id)
    except Exception as e:
        return {"error": str(e)}


def _tool_teachers(school_id: str, args: dict) -> dict:
    ac = create_admin_client()
    teachers = fetch_school_teachers(school_id, ac)
    name_filter = (args.get("nameContains") or "").strip().lower()
    if name_filter:
        teachers = [t for t in teachers if name_filter in (t.get("name") or "").lower()]
    return {"teachers": [{"id": t["id"], "name": t.get("name"), "subject": t.get("subject")} for t in teachers]}


def _tool_classes(school_id: str, args: dict) -> dict:
    ac = create_admin_client()
    classes = fetch_school_classes(school_id, ac)
    return {"classes": [{"id": c["id"], "name": c.get("name"), "grade": c.get("grade"), "section": c.get("section")} for c in classes]}


def _tool_pending_leave_requests(school_id: str, args: dict) -> dict:
    ac = create_admin_client()
    rows = fetch_pending_leave_requests(school_id, ac)
    name_by_id = {t["id"]: t["name"] for t in fetch_school_teachers(school_id, ac)}
    return {"requests": [{**r, "teacherName": name_by_id.get(r["teacherId"], "Teacher")} for r in rows]}


def _tool_substitutes_today(school_id: str, args: dict) -> dict:
    ac = create_admin_client()
    date_str = args.get("date") or datetime.now(timezone.utc).date().isoformat()
    availability = fetch_teacher_availability(school_id, date_str, ac)
    substitutions = fetch_substitutions_for_date(school_id, date_str, ac)
    name_by_id = {t["id"]: t["name"] for t in fetch_school_teachers(school_id, ac)}
    unresolved = [s for s in substitutions if s.get("status") == "unresolved"]
    return {
        "date": date_str,
        "teachersOut": [name_by_id.get(a["teacherId"], "Teacher") for a in availability],
        "unresolvedGapCount": len(unresolved),
        "unresolvedGaps": [{"classId": s["classId"], "periodNumber": s["periodNumber"]} for s in unresolved],
    }


def _tool_announcements(school_id: str, args: dict) -> dict:
    ac = create_admin_client()
    rows = fetch_school_announcements(school_id, ac)
    return {"announcements": rows[:10]}


def _tool_calendar(school_id: str, args: dict) -> dict:
    """Upcoming calendar events only — same fetch_academic_events source
    admin_schools.py's /academic-events uses, filtered to today-forward so a
    past holiday doesn't crowd out what's actually coming up."""
    ac = create_admin_client()
    today = datetime.now(timezone.utc).date().isoformat()
    events = fetch_academic_events(school_id, ac)
    upcoming = [e for e in events if (e.get("endDate") or e.get("startDate") or "") >= today]
    upcoming.sort(key=lambda e: e.get("startDate") or "")
    return {"events": [{"title": e.get("title"), "category": e.get("category"), "startDate": e.get("startDate"), "endDate": e.get("endDate")} for e in upcoming]}


def _tool_exam_schedule(school_id: str, args: dict) -> dict:
    ac = create_admin_client()
    today = datetime.now(timezone.utc).date().isoformat()
    events = fetch_academic_events(school_id, ac)
    exams = [
        e for e in events
        if e.get("category") == "exam" and (e.get("endDate") or e.get("startDate") or "") >= today
    ]
    exams.sort(key=lambda e: e.get("startDate") or "")
    return {"exams": [{"title": e.get("title"), "startDate": e.get("startDate"), "endDate": e.get("endDate")} for e in exams]}


def _tool_syllabus_status(school_id: str, args: dict) -> dict:
    """Completion per SECTION, not one grade-wide number — mirrors
    admin_grade_syllabus.py's /grade-syllabus/status route exactly, since
    sections of the same grade can genuinely be at different paces and
    collapsing that into an average would hide which one is actually behind."""
    grade = args.get("grade")
    subject = args.get("subject")
    if not grade or not subject:
        return {"error": "grade and subject are both required"}

    ac = create_admin_client()
    sections = [c for c in fetch_school_classes(school_id, ac) if c["grade"] == grade]
    if not sections:
        return {"sections": [], "totalTopics": 0}

    class_ids = [s["id"] for s in sections]
    rows = (
        ac.table("syllabus_topics").select("class_id, is_completed")
        .in_("class_id", class_ids).eq("subject", subject)
        .execute().data or []
    )
    by_class: dict[str, list[dict]] = {}
    for r in rows:
        by_class.setdefault(r["class_id"], []).append(r)

    return {
        "grade": grade, "subject": subject,
        "sections": [
            {
                "section": s.get("section") or s.get("name"),
                "completed": sum(1 for r in by_class.get(s["id"], []) if r.get("is_completed")),
                "total": len(by_class.get(s["id"], [])),
            }
            for s in sections
        ],
    }


def _tool_textbook_status(school_id: str, args: dict) -> dict:
    """Mirrors admin_textbooks.py's list_textbooks route — same two queries,
    same chapterCount/publishedCount shape — with an optional grade/subject
    filter applied after, the same way the old ask-intent flow filtered
    client-side in buildTextbookStatusAnswer."""
    ac = create_admin_client()
    books = (
        ac.table("textbook_books").select("id, grade, subject, language")
        .eq("school_id", school_id).order("grade").order("subject")
        .execute().data or []
    )
    if not books:
        return {"books": []}

    grade = args.get("grade")
    subject = args.get("subject")
    if grade:
        books = [b for b in books if b["grade"] == grade]
    if subject:
        books = [b for b in books if (b.get("subject") or "").lower() == subject.lower()]
    if not books:
        return {"books": []}

    chapters = (
        ac.table("textbook_chapters").select("book_uuid, published")
        .in_("book_uuid", [b["id"] for b in books])
        .execute().data or []
    )
    by_book: dict[str, list[dict]] = {}
    for c in chapters:
        by_book.setdefault(c["book_uuid"], []).append(c)

    return {
        "books": [
            {
                "grade": b["grade"], "subject": b["subject"], "language": b.get("language"),
                "chapterCount": len(by_book.get(b["id"], [])),
                "publishedCount": sum(1 for c in by_book.get(b["id"], []) if c.get("published")),
            }
            for b in books
        ]
    }


def _group_consecutive_dates(dates: list) -> list:
    """Merges sorted ISO date strings into contiguous {startDate, endDate}
    ranges — e.g. a teacher on leave Mon-Wed then again Fri becomes two
    ranges, not one, so approving/rejecting always targets a single block."""
    from datetime import date as _date, timedelta as _td
    if not dates:
        return []
    ranges = []
    start = prev = _date.fromisoformat(dates[0])
    for d_str in dates[1:]:
        d = _date.fromisoformat(d_str)
        if d == prev + _td(days=1):
            prev = d
            continue
        ranges.append({"startDate": start.isoformat(), "endDate": prev.isoformat()})
        start = prev = d
    ranges.append({"startDate": start.isoformat(), "endDate": prev.isoformat()})
    return ranges


def _match_pending_teacher(school_id: str, teacher_ref: str, ac) -> dict:
    """Same resolution AdminAskAssistant.tsx's matchPendingTeacher does: only
    match teachers who actually have a pending request (not the whole
    roster), and group their pending dates into contiguous ranges. Returns
    kind "found" | "ambiguous" | "none" — the caller decides what to do with
    each, same three-outcome shape used everywhere else in this app."""
    rows = fetch_pending_leave_requests(school_id, ac)
    name_by_id = {t["id"]: t["name"] for t in fetch_school_teachers(school_id, ac)}
    ref = teacher_ref.strip().lower()

    matched_ids = {r["teacherId"] for r in rows if ref in (name_by_id.get(r["teacherId"], "") or "").lower()}
    if not matched_ids:
        return {"kind": "none"}
    if len(matched_ids) > 1:
        return {"kind": "ambiguous", "candidates": [name_by_id.get(tid, "Teacher") for tid in matched_ids]}

    teacher_id = next(iter(matched_ids))
    dates = sorted(r["date"] for r in rows if r["teacherId"] == teacher_id)
    return {"kind": "found", "teacherId": teacher_id, "teacherName": name_by_id.get(teacher_id, "Teacher"), "ranges": _group_consecutive_dates(dates)}


def _tool_approve_leave(school_id: str, args: dict) -> dict:
    """ACTION tool. Never approves anything itself — resolves the teacher and
    date range, then hands back a "proposal" the loop stops on. The real
    approval only happens if a human confirms it via POST .../argus/confirm,
    which calls the exact same approve_leave_request the admin's own
    Approve button already uses."""
    teacher_ref = (args.get("teacherRef") or "").strip()
    if not teacher_ref:
        return {"error": "teacherRef is required — which teacher?"}
    ac = create_admin_client()
    match = _match_pending_teacher(school_id, teacher_ref, ac)
    if match["kind"] == "none":
        return {"error": f'"{teacher_ref}" has no pending leave request.'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one teacher matches "{teacher_ref}": {", ".join(match["candidates"])} — ask which one.'}
    if len(match["ranges"]) > 1:
        return {"error": f'{match["teacherName"]} has {len(match["ranges"])} separate pending periods — ask which date range.', "ranges": match["ranges"]}
    r = match["ranges"][0]
    date_label = r["startDate"] if r["endDate"] == r["startDate"] else f'{r["startDate"]} to {r["endDate"]}'
    return {
        "proposal": {"tool": "approve_leave", "teacherId": match["teacherId"], "startDate": r["startDate"], "endDate": r["endDate"]},
        "label": f'Approve {match["teacherName"]}\'s leave for {date_label}',
    }


def _tool_reject_leave(school_id: str, args: dict) -> dict:
    """ACTION tool — same propose-only contract as _tool_approve_leave above."""
    teacher_ref = (args.get("teacherRef") or "").strip()
    if not teacher_ref:
        return {"error": "teacherRef is required — which teacher?"}
    ac = create_admin_client()
    match = _match_pending_teacher(school_id, teacher_ref, ac)
    if match["kind"] == "none":
        return {"error": f'"{teacher_ref}" has no pending leave request.'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one teacher matches "{teacher_ref}": {", ".join(match["candidates"])} — ask which one.'}
    if len(match["ranges"]) > 1:
        return {"error": f'{match["teacherName"]} has {len(match["ranges"])} separate pending periods — ask which date range.', "ranges": match["ranges"]}
    r = match["ranges"][0]
    date_label = r["startDate"] if r["endDate"] == r["startDate"] else f'{r["startDate"]} to {r["endDate"]}'
    return {
        "proposal": {"tool": "reject_leave", "teacherId": match["teacherId"], "startDate": r["startDate"], "endDate": r["endDate"]},
        "label": f'Reject {match["teacherName"]}\'s leave for {date_label}',
    }


def _match_teacher(school_id: str, teacher_ref: str, ac) -> dict:
    """Name -> teacher across the whole roster (unlike
    _match_pending_teacher, which only looks at teachers with a pending
    leave request). Same found/ambiguous/none contract."""
    teachers = fetch_school_teachers(school_id, ac)
    ref = teacher_ref.strip().lower()
    hits = [t for t in teachers if ref in (t.get("name") or "").lower()]
    if not hits:
        return {"kind": "none"}
    if len(hits) > 1:
        return {"kind": "ambiguous", "candidates": [t["name"] for t in hits]}
    return {"kind": "found", "teacher": hits[0]}


def _match_class(school_id: str, class_ref: str, ac) -> dict:
    """Name -> class. Matches against the class's own name and against
    "<grade> <section>", since an admin says "5B" or "Grade 5 B" or "5-B"
    interchangeably."""
    classes = fetch_school_classes(school_id, ac)
    ref = "".join(ch for ch in class_ref.lower() if ch.isalnum())
    if not ref:
        return {"kind": "none"}

    def keys(c: dict) -> list:
        grade, section, name = str(c.get("grade") or ""), str(c.get("section") or ""), str(c.get("name") or "")
        return ["".join(ch for ch in k.lower() if ch.isalnum()) for k in (name, f"{grade}{section}", f"grade{grade}{section}")]

    hits = [c for c in classes if any(ref == k or (len(ref) >= 2 and ref in k) for k in keys(c))]
    if not hits:
        return {"kind": "none"}
    if len(hits) > 1:
        return {"kind": "ambiguous", "candidates": [c.get("name") or f'{c.get("grade")} {c.get("section")}' for c in hits]}
    return {"kind": "found", "cls": hits[0]}


def _tool_assign_substitute(school_id: str, args: dict) -> dict:
    """ACTION tool. Resolves an unresolved cover gap to a specific qualified
    teacher using find_substitute — the exact same matcher the absence
    automation already uses (subject match required, workload caps hard
    enforced, ranked by fewest periods that day). Proposes only."""
    date_str = args.get("date") or _today()
    ac = create_admin_client()
    subs = fetch_substitutions_for_date(school_id, date_str, ac)
    unresolved = [s for s in subs if s.get("status") == "unresolved"]
    if not unresolved:
        return {"error": f"No unresolved cover gaps on {date_str} — nothing to assign."}

    class_name = {c["id"]: (c.get("name") or f'{c.get("grade")} {c.get("section")}') for c in fetch_school_classes(school_id, ac)}

    # Exact targeting, used by the sweep (which already knows the row it
    # found) so it never depends on name/period narrowing being unambiguous.
    if args.get("substitutionId"):
        unresolved = [s for s in unresolved if s["id"] == args["substitutionId"]]

    if args.get("periodNumber") is not None:
        try:
            wanted = int(args["periodNumber"])
            unresolved = [s for s in unresolved if s["periodNumber"] == wanted]
        except (TypeError, ValueError):
            return {"error": "periodNumber must be a number."}
    if args.get("classRef"):
        cref = str(args["classRef"]).strip().lower()
        unresolved = [s for s in unresolved if cref in (class_name.get(s["classId"], "")).lower()]

    if not unresolved:
        return {"error": "No unresolved gap matches that class/period."}
    if len(unresolved) > 1:
        return {
            "error": f"{len(unresolved)} gaps are unresolved — ask which one (name the class and period).",
            "gaps": [{"className": class_name.get(s["classId"]), "periodNumber": s["periodNumber"], "subject": s.get("subject")} for s in unresolved],
        }

    gap = unresolved[0]
    candidates = fetch_teacher_eligibility_data(school_id, date_str, ac)
    exclude = {a["teacherId"] for a in fetch_teacher_availability(school_id, date_str, ac)}
    used_this_slot = {
        s["substituteTeacherId"] for s in subs
        if s.get("substituteTeacherId") and s["periodNumber"] == gap["periodNumber"]
    }
    pick = find_substitute(
        {"dayOfWeek": gap["dayOfWeek"], "periodNumber": gap["periodNumber"], "subject": gap.get("subject") or ""},
        candidates, exclude, used_this_slot,
    )
    if not pick:
        return {"error": "No qualified teacher is free for that period without breaching a workload cap — the Substitutes page's swap suggestion is the fallback here."}

    name_by_id = {t["id"]: t["name"] for t in fetch_school_teachers(school_id, ac)}
    subject_note = f' ({gap["subject"]})' if gap.get("subject") else ""
    return {
        "proposal": {"tool": "assign_substitute", "substitutionId": gap["id"], "substituteTeacherId": pick, "date": date_str},
        "label": (
            f'Assign {name_by_id.get(pick, "a teacher")} to cover '
            f'{class_name.get(gap["classId"], "a class")} period {gap["periodNumber"]}{subject_note} on {date_str}'
        ),
    }


def _tool_mark_teacher_unavailable(school_id: str, args: dict) -> dict:
    """ACTION tool. Marking an absence is what *creates* cover needs — on
    confirm it runs apply_teacher_absence, which assigns substitutes and
    notifies everyone affected. Proposes only."""
    teacher_ref = (args.get("teacherRef") or "").strip()
    if not teacher_ref:
        return {"error": "teacherRef is required — which teacher?"}
    reason = args.get("reason") if args.get("reason") in LEAVE_REASONS else "other"
    date_str = args.get("date") or _today()

    ac = create_admin_client()
    match = _match_teacher(school_id, teacher_ref, ac)
    if match["kind"] == "none":
        return {"error": f'No teacher matching "{teacher_ref}" at this school.'}
    if match["kind"] == "ambiguous":
        return {"error": f'More than one teacher matches "{teacher_ref}": {", ".join(match["candidates"])} — ask which one.'}

    teacher = match["teacher"]
    return {
        "proposal": {"tool": "mark_teacher_unavailable", "teacherId": teacher["id"], "date": date_str, "reason": reason},
        "label": f'Mark {teacher["name"]} unavailable on {date_str} ({reason.replace("_", " ")}) — substitutes will be assigned and affected teachers notified',
    }


def _subject_matches(want: str, known: set) -> bool:
    """Subject names are free text and never spelled consistently — an admin
    says "Maths", the teacher row says "Mathematics", a timetable label says
    "MATHS". Compare on a normalised 4-char stem so those all agree, rather
    than the exact set membership that quietly matched nothing."""
    if not want:
        return False

    def stem(s: str) -> str:
        norm = "".join(ch for ch in str(s).lower() if ch.isalnum())
        return norm[:4]

    target = stem(want)
    return bool(target) and any(stem(k) == target for k in known if k)


def _known_subjects_for(teacher_id: str, candidate: dict, profile_subject_by_id: dict) -> set:
    """fetch_teacher_eligibility_data derives subjectsTaught purely from
    existing assignments and timetable labels, so it is empty for a teacher
    who hasn't been assigned anything yet — exactly the situation when a
    class has no teacher. Fold in the teacher's own profile subject so a
    brand-new school still gets a subject-aware match."""
    known = set(candidate.get("subjectsTaught") or set())
    profile = profile_subject_by_id.get(teacher_id)
    if profile:
        known.add(profile)
    return known


def _tool_assign_teacher_to_class(school_id: str, args: dict) -> dict:
    """ACTION tool for the dashboard's "classes with no teacher" gap. If no
    teacher is named, ranks the roster the way the substitute matcher does —
    teachers who demonstrably already teach the subject first, then lightest
    weekly load, then name — and proposes the best fit with its reasoning.
    Never picks a teacher the model invented; the pick comes from
    fetch_teacher_eligibility_data, which derives subjectsTaught from real
    assignments and timetable labels. Proposes only."""
    class_ref = (args.get("classRef") or "").strip()
    if not class_ref:
        return {"error": "classRef is required — which class?"}

    ac = create_admin_client()
    cmatch = _match_class(school_id, class_ref, ac)
    if cmatch["kind"] == "none":
        return {"error": f'No class matching "{class_ref}".'}
    if cmatch["kind"] == "ambiguous":
        return {"error": f'More than one class matches "{class_ref}": {", ".join(cmatch["candidates"])} — ask which one.'}
    cls = cmatch["cls"]
    cls_label = cls.get("name") or f'{cls.get("grade")} {cls.get("section")}'
    subject = (args.get("subject") or "").strip() or None

    # Explicitly named teacher: resolve and propose, no ranking needed.
    if args.get("teacherRef"):
        tmatch = _match_teacher(school_id, str(args["teacherRef"]), ac)
        if tmatch["kind"] == "none":
            return {"error": f'No teacher matching "{args["teacherRef"]}" at this school.'}
        if tmatch["kind"] == "ambiguous":
            return {"error": f'More than one teacher matches "{args["teacherRef"]}": {", ".join(tmatch["candidates"])} — ask which one.'}
        teacher = tmatch["teacher"]
        return {
            "proposal": {"tool": "assign_teacher_to_class", "classId": cls["id"], "teacherId": teacher["id"], "subject": subject},
            "label": f'Assign {teacher["name"]} to {cls_label}{f" for {subject}" if subject else ""}',
        }

    candidates = fetch_teacher_eligibility_data(school_id, _today(), ac)
    if not candidates:
        return {"error": "No teachers on the roster to assign."}

    # A sweep proposing fixes for several unstaffed classes at once computes
    # each one separately, so without this every proposal names the same
    # lightest-loaded teacher and confirming them all would pile every class
    # on one person. The caller passes back who it has already spoken for.
    exclude = set(args.get("excludeTeacherIds") or [])
    if exclude:
        remaining = [c for c in candidates if c["teacherId"] not in exclude]
        if remaining:
            candidates = remaining

    profile_subject_by_id = {t["id"]: t.get("subject") for t in fetch_school_teachers(school_id, ac)}
    under_cap = [
        c for c in candidates
        if c.get("maxPeriodsPerWeek") is None or c.get("weeklyLoad", 0) < c["maxPeriodsPerWeek"]
    ]
    pool = under_cap or candidates

    def matches(c: dict) -> bool:
        return _subject_matches(subject, _known_subjects_for(c["teacherId"], c, profile_subject_by_id))

    if subject:
        pool.sort(key=lambda c: (0 if matches(c) else 1, c.get("weeklyLoad", 0), c["name"]))
    else:
        pool.sort(key=lambda c: (c.get("weeklyLoad", 0), c["name"]))

    best = pool[0]
    teaches_subject = bool(subject) and matches(best)
    why = (
        f'already teaches {subject}, ' if teaches_subject
        else f'does not currently teach {subject}, ' if subject
        else ""
    ) + f'{best.get("weeklyLoad", 0)} periods/week currently'
    return {
        "proposal": {"tool": "assign_teacher_to_class", "classId": cls["id"], "teacherId": best["teacherId"], "subject": subject},
        "label": f'Assign {best["name"]} to {cls_label}{f" for {subject}" if subject else ""} ({why})',
    }


def _tool_post_announcement(school_id: str, args: dict) -> dict:
    """ACTION tool — no lookup needed, but still only proposes. Reaches every
    teacher in the school once confirmed, which is exactly why it doesn't
    fire from this function."""
    title = (args.get("title") or "").strip()
    body = (args.get("body") or "").strip()
    category = args.get("category") if args.get("category") in ("general", "exam", "urgent", "holiday") else "general"
    if not title or not body:
        return {"error": "both title and body are required for an announcement"}
    return {
        "proposal": {"tool": "post_announcement", "title": title, "body": body, "category": category},
        "label": f'Post announcement "{title}"',
    }


# Tool registry — name -> {description, params, fn}. Adding a tool later is
# adding one entry here; the loop and prompt below need no changes. Entries
# tagged side_effect="action" never execute inside this file — their fn only
# resolves/validates and returns a "proposal" dict, which run_argus below
# treats as a stop signal rather than a normal result to keep looping on.
TOOLS = {
    "get_overview": {
        "description": "The admin dashboard's own numbers: teacher/class/student counts, timetable coverage, "
                       "readiness gaps (classes with no teacher, classes with no students, unstaffed periods) and "
                       "today's operations (attendance rate, pending doubts, teachers absent, unresolved cover). "
                       "Start here for any 'what needs attention' question.",
        "params": {},
        "fn": _tool_overview,
    },
    "get_automation_log": {
        "description": "Audit trail of the absence automation: absences recorded, cover assigned/cancelled, periods left unfilled.",
        "params": {"date": "string, optional, ISO date — omit for all recent"},
        "fn": _tool_automation_log,
    },
    "get_students": {
        "description": "Search students school-wide by name, returning their id and class. Use before get_student_record.",
        "params": {"nameContains": "string, optional"},
        "fn": _tool_students,
    },
    "get_student_record": {
        "description": "One student's full record: attendance, marks, topic mastery, doubts, intervention notes.",
        "params": {"studentId": "string, required — get it from get_students"},
        "fn": _tool_student_record,
    },
    "get_teachers": {
        "description": "List teachers, optionally filtered by a name substring.",
        "params": {"nameContains": "string, optional"},
        "fn": _tool_teachers,
    },
    "get_classes": {
        "description": "List all classes with grade and section.",
        "params": {},
        "fn": _tool_classes,
    },
    "get_pending_leave_requests": {
        "description": "Leave requests awaiting admin approval, with teacher names.",
        "params": {},
        "fn": _tool_pending_leave_requests,
    },
    "get_substitutes_today": {
        "description": "Who's marked absent and which periods still lack a substitute, for a date.",
        "params": {"date": "string, optional, ISO date, defaults to today"},
        "fn": _tool_substitutes_today,
    },
    "get_announcements": {
        "description": "The 10 most recent school announcements.",
        "params": {},
        "fn": _tool_announcements,
    },
    "get_calendar": {
        "description": "Upcoming academic calendar events (terms, holidays, exams), soonest first.",
        "params": {},
        "fn": _tool_calendar,
    },
    "get_exam_schedule": {
        "description": "Upcoming exam events specifically — which exams, which dates.",
        "params": {},
        "fn": _tool_exam_schedule,
    },
    "get_syllabus_status": {
        "description": "Syllabus completion per section for one grade+subject — sections can be at different paces.",
        "params": {"grade": "string, required", "subject": "string, required"},
        "fn": _tool_syllabus_status,
    },
    "get_textbook_status": {
        "description": "Textbook ingestion/review progress — chapters processed vs. published, optionally filtered.",
        "params": {"grade": "string, optional", "subject": "string, optional"},
        "fn": _tool_textbook_status,
    },
    "approve_leave": {
        "description": "Approve a teacher's pending leave request.",
        "params": {"teacherRef": "string, required — the teacher's name, exactly as mentioned"},
        "side_effect": "action",
        "fn": _tool_approve_leave,
    },
    "reject_leave": {
        "description": "Reject a teacher's pending leave request.",
        "params": {"teacherRef": "string, required — the teacher's name, exactly as mentioned"},
        "side_effect": "action",
        "fn": _tool_reject_leave,
    },
    "post_announcement": {
        "description": "Post a new school-wide announcement, visible to every teacher.",
        "params": {"title": "string, required", "body": "string, required", "category": "one of general/exam/urgent/holiday, optional"},
        "side_effect": "action",
        "fn": _tool_post_announcement,
    },
    "assign_substitute": {
        "description": "Fill an unresolved cover gap — picks the best-fit qualified, under-cap teacher automatically.",
        "params": {
            "date": "string, optional, ISO date, defaults to today",
            "classRef": "string, optional — narrows to one class if several gaps exist",
            "periodNumber": "number, optional — narrows to one period if several gaps exist",
        },
        "side_effect": "action",
        "fn": _tool_assign_substitute,
    },
    "mark_teacher_unavailable": {
        "description": "Record a teacher as absent for a date; on confirm this assigns substitutes and notifies those affected.",
        "params": {
            "teacherRef": "string, required — the teacher's name as mentioned",
            "date": "string, optional, ISO date, defaults to today",
            "reason": "one of on_leave/late_arrival/official_duty/other, optional",
        },
        "side_effect": "action",
        "fn": _tool_mark_teacher_unavailable,
    },
    "assign_teacher_to_class": {
        "description": "Assign a teacher to a class that has none. If no teacher is named, proposes the best fit "
                       "(already teaches the subject, then lightest weekly load).",
        "params": {
            "classRef": "string, required — e.g. \"5B\" or \"Grade 5 B\"",
            "subject": "string, optional — improves the match",
            "teacherRef": "string, optional — name a specific teacher instead of auto-matching",
            "excludeTeacherIds": "list of teacher ids, optional — skip these when auto-matching",
        },
        "side_effect": "action",
        "fn": _tool_assign_teacher_to_class,
    },
}


def _tool_catalog_text() -> str:
    lines = []
    for name, spec in TOOLS.items():
        params = ", ".join(f"{k} ({v})" for k, v in spec["params"].items()) or "none"
        tag = " [ACTION — calling this only proposes it; nothing happens until a human confirms]" if spec.get("side_effect") == "action" else ""
        lines.append(f'- "{name}"{tag}: {spec["description"]} Params: {params}.')
    return "\n".join(lines)


def _scratchpad_text(scratchpad: list) -> str:
    if not scratchpad:
        return "(none yet)"
    return "\n".join(f"- {r['tool']}({json.dumps(r['args'])}) -> {json.dumps(r['result'])}" for r in scratchpad)


async def run_argus(school_id: str, question: str, history: list, ip: str) -> dict:
    """The agent loop: reason, call a tool, store the result, repeat — capped
    at MAX_STEPS. Returns one of three shapes, always with a "status":
      - {"status": "answered", "response": ..., "trace": [...]}
      - {"status": "pending_confirmation", "proposal": {...}, "label": ..., "trace": [...]}
      - {"status": "error" | "step_limit", "response": ..., "trace": [...]}
    An action tool's result never gets appended to the scratchpad and looped
    on like a read tool's — the moment one resolves cleanly to a "proposal",
    the run stops right there and hands it back for a human to confirm
    separately, via POST .../argus/confirm."""
    scratchpad: list[dict] = []
    trace: list[dict] = []

    for step in range(MAX_STEPS):
        prompt = f"""You are Argus, an operations assistant for a school admin. Your scope is
this one school only ({school_id}) — you have no visibility into any other
school. You never state a fact yourself; every fact in your final answer
must come from a tool result below, not from your own generation.

The admin asked: "{question}"
{history_block(history)}

Tools available — call as many as you need, in any order, and call the same
one again with different args if that's what answering needs:
{_tool_catalog_text()}

Results gathered so far this run:
{_scratchpad_text(scratchpad)}

Decide your next step. Return JSON only, no markdown, one of:
{{"action": "call_tool", "tool": "<name>", "args": {{...}}}}
{{"action": "finish", "response": "<plain, simple-English answer to the admin>"}}
"""
        t0 = time.time()
        try:
            raw = await call_ai([{"role": "user", "content": prompt}])
            decision = json.loads(raw)
        except Exception as e:
            api_log("argus", ip, (time.time() - t0) * 1000, False, "error", error=str(e))
            return {"status": "error", "response": "Something went wrong reasoning about that — try again.", "trace": trace}

        action = decision.get("action")

        if action == "finish":
            api_log("argus", ip, (time.time() - t0) * 1000, False, "ok")
            return {"status": "answered", "response": decision.get("response") or "I didn't find anything that answers that.", "trace": trace}

        if action == "call_tool":
            tool_name = decision.get("tool")
            args = decision.get("args") or {}
            tool = TOOLS.get(tool_name)
            if not tool:
                result = {"error": f'unknown tool "{tool_name}"'}
            else:
                try:
                    result = tool["fn"](school_id, args)
                except Exception as e:
                    result = {"error": str(e)}

            if isinstance(result, dict) and "proposal" in result:
                api_log("argus", ip, (time.time() - t0) * 1000, False, "ok")
                trace.append({"tool": tool_name, "args": args, "ok": True})
                return {"status": "pending_confirmation", "proposal": result["proposal"], "label": result.get("label"), "trace": trace}

            scratchpad.append({"tool": tool_name, "args": args, "result": result})
            trace.append({"tool": tool_name, "args": args, "ok": "error" not in result})
            continue

        # Unrecognized action shape — stop rather than loop on something the
        # model can't recover from on its own.
        return {"status": "error", "response": "I didn't quite understand what to do with that — try rephrasing.", "trace": trace}

    return {"status": "step_limit", "response": "That took more steps than expected — try a more specific question.", "trace": trace}
