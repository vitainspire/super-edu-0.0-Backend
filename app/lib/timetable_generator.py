"""Mirrors lib/timetableGenerator.ts.

Whole-school timetable generator — STABLE/INCREMENTAL by design.

A school's timetable is something teachers and students build daily
routines around. Re-solving from scratch every time an admin makes one
small change (a new teacher, one grade's lineup tweak) would reshuffle a
large number of *unrelated* periods too — technically valid, but
disruptive and untrustworthy in practice.

So `existing_periods` (the currently-published timetable, if any) is
treated as PINNED by default: any period whose (class, subject, teacher)
still matches what's currently required is kept in its exact slot,
untouched. Only the delta — periods for a requirement that's new, whose
assignment changed, or whose required count increased — goes through the
constrained-first + backtracking placement below. Placement can still
relocate OTHER newly-placed periods to resolve a conflict, but it will
never move a pinned one — that's what keeps re-runs stable.

This is a strong heuristic, not a guaranteed-optimal CSP/ILP solver —
genuinely infeasible overcommitment (a teacher needs more periods than
exist) is reported via `teacher_warnings`, not silently dropped.

This produces a repeating WEEKLY template (e.g. "Monday period 3"), same
as before — it has no awareness of specific calendar dates/holidays. That
is a deliberate scope boundary: calendar data belongs in day-level
consumers (attendance, daily briefing), not in this weekly-pattern
generator."""
import random


def _shuffle(arr: list) -> list:
    a = list(arr)
    for i in range(len(a) - 1, 0, -1):
        j = random.randint(0, i)
        a[i], a[j] = a[j], a[i]
    return a


def _slot_key(day: int, period_number: int) -> str:
    return f"{day}|{period_number}"


def generate_school_timetable(
    slots: list[dict],
    working_weekdays: list[int],
    classes: list[dict],           # {classId, className, grade}
    lineup: list[dict],            # {grade, subject, periodsPerWeek, category?}
    assignments: list[dict],       # {classId, subject, teacherId}
    teacher_caps: list[dict],      # {teacherId, teacherName?, maxPeriodsPerDay?, maxPeriodsPerWeek?}
    existing_periods: list[dict] = None,
) -> dict:
    existing_periods = existing_periods or []

    period_slots = [s for s in slots if s.get("type") == "period" and s.get("periodNumber") is not None]
    all_day_slots = [
        {"day": day, "periodNumber": s["periodNumber"], "startTime": s["startTime"], "endTime": s["endTime"]}
        for day in working_weekdays
        for s in period_slots
    ]
    valid_slot_keys = {_slot_key(s["day"], s["periodNumber"]) for s in all_day_slots}
    total_slots_per_week = len(all_day_slots)

    class_by_id = {c["classId"]: c for c in classes}
    cap_by_teacher = {t["teacherId"]: t for t in teacher_caps}
    assignment_by_class_subject = {f"{a['classId']}|{a['subject']}": a["teacherId"] for a in assignments}

    category_by_grade_subject = {f"{l['grade']}|{l['subject']}": l.get("category") or "core" for l in lineup}

    def category_of(class_id: str, subject: str) -> str:
        cls = class_by_id.get(class_id)
        if not cls:
            return "core"
        return category_by_grade_subject.get(f"{cls['grade']}|{subject}", "core")

    # ── 1. How many periods of each (class, subject) are actually required now ──
    required_count: dict[str, int] = {}
    for cls in classes:
        for item in lineup:
            if item["grade"] != cls["grade"]:
                continue
            required_count[f"{cls['classId']}|{item['subject']}"] = max(0, item["periodsPerWeek"])

    # ── 2. State maps, shared by both the "keep pinned" pass and the placement pass ──
    class_used_slots: dict[str, set] = {}
    teacher_used_slots: dict[str, set] = {}
    teacher_day_count: dict[str, dict[int, int]] = {}
    class_subject_days: dict[str, set] = {}
    placed_by_class_slot: dict[str, dict] = {}
    pinned_keys: set = set()

    periods: list[dict] = []
    class_stats = {c["classId"]: {"classId": c["classId"], "className": c["className"], "placed": 0, "kept": 0, "skipped": 0} for c in classes}

    def is_free_for_class(class_id: str, key: str) -> bool:
        return key not in class_used_slots.get(class_id, set())

    def is_free_for_teacher(teacher_id: str, day: int, key: str) -> bool:
        if key in teacher_used_slots.get(teacher_id, set()):
            return False
        cap = cap_by_teacher.get(teacher_id, {}).get("maxPeriodsPerDay")
        if cap is None:
            return True
        return teacher_day_count.get(teacher_id, {}).get(day, 0) < cap

    def mark_occupied(class_id: str, subject: str, teacher_id, day: int, key: str) -> None:
        class_used_slots.setdefault(class_id, set()).add(key)
        if teacher_id:
            teacher_used_slots.setdefault(teacher_id, set()).add(key)
            dc = teacher_day_count.setdefault(teacher_id, {})
            dc[day] = dc.get(day, 0) + 1
        subj_key = f"{class_id}|{subject}"
        class_subject_days.setdefault(subj_key, set()).add(day)

    def place(task: dict, slot: dict) -> None:
        key = _slot_key(slot["day"], slot["periodNumber"])
        mark_occupied(task["classId"], task["subject"], task.get("teacherId"), slot["day"], key)
        period = {
            "classId": task["classId"], "dayOfWeek": slot["day"], "periodNumber": slot["periodNumber"],
            "startTime": slot["startTime"], "endTime": slot["endTime"], "teacherId": task.get("teacherId"), "label": task["subject"],
        }
        periods.append(period)
        placed_by_class_slot[f"{task['classId']}|{key}"] = period
        class_stats[task["classId"]]["placed"] += 1

    def unplace(period: dict) -> None:
        key = _slot_key(period["dayOfWeek"], period["periodNumber"])
        class_used_slots.get(period["classId"], set()).discard(key)
        if period.get("teacherId"):
            teacher_used_slots.get(period["teacherId"], set()).discard(key)
            dc = teacher_day_count.get(period["teacherId"])
            if dc is not None:
                dc[period["dayOfWeek"]] = max(0, dc.get(period["dayOfWeek"], 1) - 1)
        class_subject_days.get(f"{period['classId']}|{period['label']}", set()).discard(period["dayOfWeek"])
        placed_by_class_slot.pop(f"{period['classId']}|{key}", None)
        idx = next((i for i, p in enumerate(periods) if p is period), -1)
        if idx >= 0:
            periods.pop(idx)
        class_stats[period["classId"]]["placed"] -= 1

    # ── 3. Keep every existing period whose (class, subject, teacher) still
    # matches a current, not-yet-fulfilled requirement — pinned, untouched. ──
    for existing in existing_periods:
        if existing["classId"] not in class_by_id:
            continue  # class no longer exists — drop
        key = _slot_key(existing["dayOfWeek"], existing["periodNumber"])
        if key not in valid_slot_keys:
            continue  # schedule template changed under it — drop
        if not is_free_for_class(existing["classId"], key):
            continue  # duplicate/conflicting stale row — drop

        req_key = f"{existing['classId']}|{existing['label']}"
        remaining = required_count.get(req_key, 0)
        if remaining <= 0:
            continue  # subject dropped from lineup, or already fully kept — drop

        current_teacher_id = assignment_by_class_subject.get(req_key)
        if existing.get("teacherId") != current_teacher_id:
            continue  # assignment changed — drop, will be re-placed

        if existing.get("teacherId") and not is_free_for_teacher(existing["teacherId"], existing["dayOfWeek"], key):
            continue  # teacher double-booked by an earlier kept row — drop this one

        mark_occupied(existing["classId"], existing["label"], existing.get("teacherId"), existing["dayOfWeek"], key)
        periods.append(existing)
        placed_by_class_slot[f"{existing['classId']}|{key}"] = existing
        pinned_keys.add(f"{existing['classId']}|{key}")
        class_stats[existing["classId"]]["kept"] += 1
        required_count[req_key] = remaining - 1

    # ── 4. Build tasks for whatever's still needed after keeping pinned periods ──
    tasks: list[dict] = []
    for req_key, count in required_count.items():
        if count <= 0:
            continue
        class_id, subject = req_key.split("|", 1)
        cls = class_by_id.get(class_id)
        if not cls:
            continue
        teacher_id = assignment_by_class_subject.get(req_key)
        for _ in range(count):
            tasks.append({"classId": class_id, "className": cls["className"], "subject": subject, "teacherId": teacher_id})

    # ── 5. Per-teacher load (kept periods so far + the still-to-place delta) + upfront feasibility check ──
    load_by_teacher: dict[str, int] = {}
    for t in tasks:
        if t.get("teacherId"):
            load_by_teacher[t["teacherId"]] = load_by_teacher.get(t["teacherId"], 0) + 1
    # `periods` only holds kept/pinned rows at this point — nothing has been placed yet.
    for p in periods:
        if p.get("teacherId"):
            load_by_teacher[p["teacherId"]] = load_by_teacher.get(p["teacherId"], 0) + 1

    teacher_warnings: list[dict] = []
    ceiling_by_teacher: dict[str, float] = {}
    for teacher_id, required in load_by_teacher.items():
        cap = cap_by_teacher.get(teacher_id)
        weekly_cap = (cap or {}).get("maxPeriodsPerWeek")
        weekly_cap = weekly_cap if weekly_cap is not None else float("inf")
        ceiling = min(weekly_cap, total_slots_per_week)
        ceiling_by_teacher[teacher_id] = ceiling
        if required > ceiling:
            teacher_warnings.append({
                "teacherId": teacher_id, "teacherName": (cap or {}).get("teacherName"),
                "requiredPeriods": required, "availableSlots": ceiling, "overBy": required - ceiling,
            })

    # ── 6. Order the remaining delta: most-constrained teacher first ──
    pressure_by_teacher: dict[str, float] = {}
    for teacher_id, required in load_by_teacher.items():
        ceiling = ceiling_by_teacher.get(teacher_id, total_slots_per_week)
        pressure_by_teacher[teacher_id] = required / max(1, ceiling)

    with_teacher = [t for t in tasks if t.get("teacherId")]
    without_teacher = [t for t in tasks if not t.get("teacherId")]
    teacher_order = sorted(load_by_teacher.keys(), key=lambda t: pressure_by_teacher.get(t, 0), reverse=True)
    ordered_tasks: list[dict] = []
    for teacher_id in teacher_order:
        ordered_tasks.extend(_shuffle([t for t in with_teacher if t.get("teacherId") == teacher_id]))
    ordered_tasks.extend(_shuffle(without_teacher))

    # ── 7. Placement — identical algorithm to before, but the state maps are
    # already pre-populated with pinned periods, and the swap/backtrack below
    # is only ever allowed to relocate a NON-pinned (this-run) period. ──
    unplaced: list[dict] = []

    def has_core_neighbor(class_id: str, day: int, period_number: int) -> bool:
        for neighbor_period_number in (period_number - 1, period_number + 1):
            neighbor = placed_by_class_slot.get(f"{class_id}|{_slot_key(day, neighbor_period_number)}")
            if neighbor and category_of(class_id, neighbor["label"]) == "core":
                return True
        return False

    def find_slot(task: dict, prefer_new_day: bool):
        used_days = class_subject_days.get(f"{task['classId']}|{task['subject']}", set())
        is_core = category_of(task["classId"], task["subject"]) == "core"
        fallback = None
        for slot in _shuffle(all_day_slots):
            key = _slot_key(slot["day"], slot["periodNumber"])
            if not is_free_for_class(task["classId"], key):
                continue
            if prefer_new_day and slot["day"] in used_days:
                continue
            if task.get("teacherId") and not is_free_for_teacher(task["teacherId"], slot["day"], key):
                continue
            if fallback is None:
                fallback = slot
            # Keep scanning for a slot that doesn't stack two core subjects back-to-back —
            # but never let that preference cost us feasibility; `fallback` covers that.
            if is_core and has_core_neighbor(task["classId"], slot["day"], slot["periodNumber"]):
                continue
            return slot
        return fallback

    for task in ordered_tasks:
        chosen = find_slot(task, True) or find_slot(task, False)

        # Bounded backtrack: if every slot that's free for this class is blocked
        # by the SAME teacher's other commitments, try relocating one of that
        # teacher's other (non-pinned) periods elsewhere to free up the slot.
        if not chosen and task.get("teacherId"):
            for slot in _shuffle(all_day_slots):
                key = _slot_key(slot["day"], slot["periodNumber"])
                if not is_free_for_class(task["classId"], key):
                    continue
                if is_free_for_teacher(task["teacherId"], slot["day"], key):
                    continue  # would've been chosen already
                found_relocation = False
                for key2, period in list(placed_by_class_slot.items()):
                    if key2 in pinned_keys:
                        continue  # never relocate a pinned/kept period
                    if period.get("teacherId") != task["teacherId"]:
                        continue
                    if _slot_key(period["dayOfWeek"], period["periodNumber"]) != key:
                        continue
                    relocated = next(
                        (alt for alt in _shuffle(all_day_slots)
                         if _slot_key(alt["day"], alt["periodNumber"]) != key
                         and is_free_for_class(period["classId"], _slot_key(alt["day"], alt["periodNumber"]))
                         and is_free_for_teacher(period["teacherId"], alt["day"], _slot_key(alt["day"], alt["periodNumber"]))),
                        None,
                    )
                    if relocated:
                        relocated_class_name = class_stats.get(period["classId"], {}).get("className", period["classId"])
                        relocated_task = {"classId": period["classId"], "className": relocated_class_name, "subject": period["label"], "teacherId": period["teacherId"]}
                        unplace(period)
                        place(relocated_task, relocated)
                        chosen = slot
                        found_relocation = True
                        break
                if found_relocation:
                    break

        if not chosen:
            class_stats[task["classId"]]["skipped"] += 1
            cap = cap_by_teacher.get(task["teacherId"]) if task.get("teacherId") else None
            if not task.get("teacherId"):
                reason = "No slot free for this class — the lineup likely needs more periods/week than the schedule has slots."
            elif cap and (cap.get("maxPeriodsPerDay") is not None or cap.get("maxPeriodsPerWeek") is not None):
                reason = f"No slot free within {cap.get('teacherName') or 'the teacher'}'s workload cap — consider raising it or reassigning this subject."
            else:
                reason = f"{(cap or {}).get('teacherName') or 'This teacher'} has no free slot across their other classes — likely overcommitted (see teacherWarnings)."
            unplaced.append({
                "classId": task["classId"], "className": task["className"], "subject": task["subject"],
                "teacherId": task.get("teacherId"), "reason": reason,
            })
            continue

        place(task, chosen)

    class_stats_list = list(class_stats.values())
    return {
        "periods": periods,
        "classStats": class_stats_list,
        "teacherWarnings": teacher_warnings,
        "unplaced": unplaced,
        "keptCount": sum(c["kept"] for c in class_stats_list),
        "placedCount": sum(c["placed"] for c in class_stats_list),
    }
