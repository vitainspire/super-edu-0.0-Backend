"""Mirrors backend/src/lib/academic-calendar.ts (only the pieces the ported
routes actually use — computeSubjectSessionAvailability)."""
from datetime import datetime, timedelta, timezone
from typing import Optional, TypedDict

SIX_DAY_WEEK: list[int] = [1, 2, 3, 4, 5, 6]
FIVE_DAY_WEEK: list[int] = [1, 2, 3, 4, 5]


class EventLike(TypedDict, total=False):
    category: str
    startDate: str
    endDate: str
    countsAsNonWorking: Optional[bool]


def _to_date_str(d: datetime) -> str:
    return d.strftime("%Y-%m-%d")


def compute_subject_session_availability(
    from_date: str,
    to_date: str,
    events: list[EventLike],
    periods_per_weekday: dict[int, int],  # dayOfWeek 1=Mon..6=Sat -> periods that day (0=Sun)
) -> int:
    """How many real class sessions a specific subject actually has left this
    year — grounded in that subject's own timetable (which weekdays, how many
    periods each) with holidays/exams subtracted out."""
    blocked_ranges = [
        (e["startDate"], e["endDate"])
        for e in events
        if e.get("category") in ("holiday", "exam") and e.get("countsAsNonWorking") is not False
    ]

    start = datetime.strptime(from_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)
    end = datetime.strptime(to_date, "%Y-%m-%d").replace(tzinfo=timezone.utc)

    sessions = 0
    d = start
    while d <= end:
        # Python's weekday(): Mon=0..Sun=6. JS getUTCDay(): Sun=0..Sat=6 — convert.
        dow = (d.weekday() + 1) % 7
        count = periods_per_weekday.get(dow)
        if count:
            date_str = _to_date_str(d)
            blocked = any(r[0] <= date_str <= r[1] for r in blocked_ranges)
            if not blocked:
                sessions += count
        d += timedelta(days=1)
    return sessions
