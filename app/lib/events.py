"""A tiny in-process event bus for school-automation side effects.

Why this exists: absence handling has three entry points (the teacher's own
daily check-in, the admin's fallback override, and multi-day leave approval)
and every one of them needs the same downstream work — notify the assigned
substitute, tell the absent teacher who's covering, flag periods nobody can
take. Before this, each route hand-rolled its own subset, so the same absence
produced different side effects depending on who recorded it.

The rule is: routes emit facts, handlers react. A route says "this teacher is
unavailable" and never has to know that four notifications fall out of it.

Deliberately synchronous and in-process. There's no broker, no queue, no
worker — emit() runs its handlers before returning, inside the request. That
keeps the whole thing debuggable and means a substitute's notification is
already written by the time the check-in response is serialised. What it buys
in exchange for that simplicity is isolation: every handler is wrapped, so a
notification failure can never turn a successful check-in into a 500. The
absence is recorded either way; the alert is best-effort.

Events are also appended to `automation_events` (migration 0005) so an admin
can answer "why is Mrs. Rao down as covering 4B on Tuesday" months later —
timetable_substitutions only holds the current state, not how it got there.
"""
import json
import uuid
from typing import Callable

# ── Event types ───────────────────────────────────────────────────────────────
# Payload keys are camelCase, matching the DTO shapes the rest of the app
# passes around (fetch_substitutions_for_date et al) so handlers can consume
# substitution rows verbatim without a translation step.

# {schoolId, teacherId, date, reason, source, note, periodCount}
TEACHER_UNAVAILABLE = "teacher.unavailable"
# One per absence-application, after every period has been settled:
# {schoolId, teacherId, date, assigned: [...], unresolved: [...]}. Exists so
# the absent teacher gets a single digest rather than one message per period —
# a week's leave over four periods a day is 20 notifications otherwise.
COVERAGE_PUBLISHED = "coverage.published"
# {schoolId, teacherId, date} — absence withdrawn, teacher is back on duty
TEACHER_AVAILABLE = "teacher.available"
# {schoolId, date, classId, periodNumber, subject, originalTeacherId,
#  substituteTeacherId, status}
SUBSTITUTION_ASSIGNED = "substitution.assigned"
# Same shape minus substituteTeacherId — nobody was free and qualified
SUBSTITUTION_UNRESOLVED = "substitution.unresolved"
# Same shape as assigned — the cover is off (original teacher came back)
SUBSTITUTION_CANCELLED = "substitution.cancelled"


_handlers: dict[str, list[Callable]] = {}


def on(event_type: str):
    """Register a handler. Used as a decorator at import time.

    Handlers receive (payload, ac) and return nothing useful — they exist for
    their side effects. Registration order is dispatch order.
    """
    def decorator(fn: Callable) -> Callable:
        _handlers.setdefault(event_type, []).append(fn)
        return fn
    return decorator


def emit(event_type: str, payload: dict, ac) -> None:
    """Record the event, then run every handler subscribed to it.

    Neither the audit write nor any individual handler can raise into the
    caller: the state change that produced this event has already been
    committed, so failing the request now would report an error for work that
    actually succeeded. Failures are logged and dispatch continues to the next
    handler.
    """
    _record(event_type, payload, ac)

    for handler in _handlers.get(event_type, []):
        try:
            handler(payload, ac)
        except Exception as e:
            print(f"[events] handler {getattr(handler, '__name__', '?')} for {event_type} failed: {type(e).__name__}: {e}")


def emit_all(event_type: str, payloads: list[dict], ac) -> None:
    """emit() over a list — the common shape, since one absence produces one
    event per affected period."""
    for payload in payloads:
        emit(event_type, payload, ac)


def _record(event_type: str, payload: dict, ac) -> None:
    try:
        ac.table("automation_events").insert({
            "id": str(uuid.uuid4()),
            "type": event_type,
            "school_id": payload.get("schoolId"),
            "date": payload.get("date"),
            # json.dumps rather than handing the dict straight to postgrest:
            # payloads can carry sets (subjectsTaught-style) and dates, and a
            # serialisation error here would otherwise surface as a confusing
            # failure of the audit write rather than of the value itself.
            "payload": json.loads(json.dumps(payload, default=str)),
        }).execute()
    except Exception as e:
        print(f"[events] automation_events write failed for {event_type}: {type(e).__name__}: {e}")
