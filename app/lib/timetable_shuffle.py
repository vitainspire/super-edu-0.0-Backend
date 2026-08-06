"""Mirrors lib/timetableShuffle.ts.

Randomized greedy timetable generator — not a full CSP solver. For each
section, it tries to spread each subject across different days first,
avoiding teacher double-bookings (tracked in `busy`, mutated as sections are
placed so later sections in the same call see earlier sections' bookings).
Any subject instance that can't be placed without conflict is skipped and
reported, rather than silently overlapping or dropping the constraint."""
import random

DAYS = [1, 2, 3, 4, 5, 6]


def _shuffle(arr: list) -> list:
    a = list(arr)
    for i in range(len(a) - 1, 0, -1):
        j = random.randint(0, i)
        a[i], a[j] = a[j], a[i]
    return a


def generate_shuffled_timetable(
    slots: list[dict],
    sections: list[dict],          # {classId, className, subjectTeacher: {subject: teacherId|None}}
    lineup: list[dict],            # {subject, periodsPerWeek}
    busy_teacher_slots: set,       # "{teacherId}|{day}|{periodNumber}" already taken outside this batch
) -> dict:
    period_slots = [s for s in slots if s.get("type") == "period" and s.get("periodNumber") is not None]
    all_day_slots = [
        {"day": day, "periodNumber": s["periodNumber"], "startTime": s["startTime"], "endTime": s["endTime"]}
        for day in DAYS
        for s in period_slots
    ]

    busy = set(busy_teacher_slots)
    periods: list[dict] = []
    section_stats: list[dict] = []

    for section in _shuffle(sections):
        bag = _shuffle([
            item["subject"]
            for item in lineup
            for _ in range(max(0, item["periodsPerWeek"]))
        ])
        available_slots = _shuffle(all_day_slots)
        used_slot_keys: set = set()
        used_days_by_subject: dict[str, set] = {}

        placed = 0
        skipped = 0

        for subject in bag:
            teacher_id = section["subjectTeacher"].get(subject)
            used_days = used_days_by_subject.setdefault(subject, set())

            chosen = None
            # First pass: prefer a day this subject hasn't used yet. Second pass: any day.
            for prefer_new_day in (True, False):
                for slot in available_slots:
                    slot_key = f"{slot['day']}|{slot['periodNumber']}"
                    if slot_key in used_slot_keys:
                        continue
                    if prefer_new_day and slot["day"] in used_days:
                        continue
                    if teacher_id and f"{teacher_id}|{slot['day']}|{slot['periodNumber']}" in busy:
                        continue
                    chosen = slot
                    break
                if chosen:
                    break

            if not chosen:
                skipped += 1
                continue

            used_slot_keys.add(f"{chosen['day']}|{chosen['periodNumber']}")
            used_days.add(chosen["day"])
            if teacher_id:
                busy.add(f"{teacher_id}|{chosen['day']}|{chosen['periodNumber']}")

            periods.append({
                "classId": section["classId"],
                "dayOfWeek": chosen["day"],
                "periodNumber": chosen["periodNumber"],
                "startTime": chosen["startTime"],
                "endTime": chosen["endTime"],
                "teacherId": teacher_id,
                "label": subject,
            })
            placed += 1

        section_stats.append({"classId": section["classId"], "className": section["className"], "placed": placed, "skipped": skipped})

    return {"periods": periods, "sectionStats": section_stats}
