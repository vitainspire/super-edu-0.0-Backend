"""Mirrors the subset of backend/src/lib/admin-queries.ts needed by the
routes ported so far."""
from typing import Optional


def fetch_admin(user_id: str, ac) -> Optional[dict]:
    res = ac.table("admins").select("*").eq("user_id", user_id).maybe_single().execute()
    data = res.data if res else None
    if not data:
        return None
    return {
        "id": data["id"],
        "userId": data["user_id"],
        "name": data["name"],
        "email": data["email"],
        "schoolId": data["school_id"],
        "createdAt": data["created_at"],
    }


def upsert_admin(a: dict, ac) -> None:
    ac.table("admins").upsert(
        {
            "id": a["id"],
            "user_id": a["userId"],
            "name": a["name"],
            "email": a["email"],
            "school_id": a["schoolId"],
            "created_at": a["createdAt"],
        }
    ).execute()


def fetch_school(school_id: str, ac) -> Optional[dict]:
    res = ac.table("schools").select("*").eq("id", school_id).maybe_single().execute()
    data = res.data if res else None
    if not data:
        return None
    return {
        "id": data["id"],
        "name": data["name"],
        "joinCode": data.get("join_code") or "",
        "createdBy": data["created_by"],
        "createdAt": data["created_at"],
    }


def create_school(school: dict, ac) -> None:
    ac.table("schools").insert(
        {
            "id": school["id"],
            "name": school["name"],
            "join_code": school["joinCode"],
            "created_by": school["createdBy"],
            "created_at": school["createdAt"],
        }
    ).execute()


def fetch_grade_subjects(school_id: str, grade: str, ac) -> list[dict]:
    res = (
        ac.table("grade_subjects")
        .select("*")
        .eq("school_id", school_id)
        .eq("grade", grade)
        .order("order_index")
        .execute()
    )
    data = res.data or []
    return [
        {
            "id": r["id"],
            "schoolId": r["school_id"],
            "grade": r["grade"],
            "subject": r["subject"],
            "periodsPerWeek": r.get("periods_per_week") or 0,
            "category": r["category"] if r.get("category") == "special" else "core",
            "orderIndex": r.get("order_index") or 0,
            "createdAt": r["created_at"],
        }
        for r in data
    ]


def fetch_all_grade_subjects(school_id: str, ac) -> list[dict]:
    res = ac.table("grade_subjects").select("*").eq("school_id", school_id).order("order_index").execute()
    data = res.data or []
    return [
        {
            "id": r["id"],
            "schoolId": r["school_id"],
            "grade": r["grade"],
            "subject": r["subject"],
            "periodsPerWeek": r.get("periods_per_week") or 0,
            "category": r["category"] if r.get("category") == "special" else "core",
            "orderIndex": r.get("order_index") or 0,
            "createdAt": r["created_at"],
        }
        for r in data
    ]


def upsert_grade_subject(s: dict, ac) -> None:
    ac.table("grade_subjects").upsert(
        {
            "id": s["id"],
            "school_id": s["schoolId"],
            "grade": s["grade"],
            "subject": s["subject"],
            "periods_per_week": s["periodsPerWeek"],
            "category": s["category"],
            "order_index": s["orderIndex"],
            "created_at": s["createdAt"],
        }
    ).execute()


def delete_grade_subject(id_: str, ac) -> None:
    ac.table("grade_subjects").delete().eq("id", id_).execute()


def _map_event(r: dict, force_published: Optional[bool] = None) -> dict:
    return {
        "id": r["id"],
        "schoolId": r["school_id"],
        "title": r["title"],
        "category": r["category"],
        "holidaySubtype": r.get("holiday_subtype"),
        "countsAsNonWorking": r.get("counts_as_non_working", True),
        "published": force_published if force_published is not None else r.get("published", False),
        "startDate": r["start_date"],
        "endDate": r["end_date"],
        "description": r.get("description"),
        "createdAt": r["created_at"],
    }


def fetch_academic_events(school_id: str, ac) -> list[dict]:
    res = (
        ac.table("academic_events")
        .select("*")
        .eq("school_id", school_id)
        .order("start_date", desc=False)
        .execute()
    )
    return [_map_event(r) for r in (res.data or [])]


def fetch_published_academic_events(school_id: str, ac) -> list[dict]:
    """Only the published (circulated) events — what teachers should see."""
    res = (
        ac.table("academic_events")
        .select("*")
        .eq("school_id", school_id)
        .eq("published", True)
        .order("start_date", desc=False)
        .execute()
    )
    return [_map_event(r, force_published=True) for r in (res.data or [])]


def upsert_academic_event(e: dict, ac) -> None:
    ac.table("academic_events").upsert(
        {
            "id": e["id"],
            "school_id": e["schoolId"],
            "title": e["title"],
            "category": e["category"],
            "holiday_subtype": e.get("holidaySubtype"),
            "counts_as_non_working": e["countsAsNonWorking"],
            "published": e["published"],
            "start_date": e["startDate"],
            "end_date": e["endDate"],
            "description": e.get("description"),
            "created_at": e["createdAt"],
        }
    ).execute()


def delete_academic_event(id_: str, school_id: str, ac) -> None:
    ac.table("academic_events").delete().eq("id", id_).eq("school_id", school_id).execute()


# ── Teachers ────────────────────────────────────────────────────────────────

def fetch_school_teachers(school_id: str, ac) -> list[dict]:
    data = ac.table("teachers").select("*").eq("school_id", school_id).execute().data or []
    return [
        {
            "id": r["id"], "userId": r["user_id"], "name": r["name"],
            "schoolName": r.get("school_name") or "", "schoolId": r.get("school_id"),
            "subject": r.get("subject") or "",
            "subjects": r.get("subjects") or ([r["subject"]] if r.get("subject") else []),
            "grade": r.get("grade") or "", "phone": r.get("phone") or "",
            "maxPeriodsPerDay": r.get("max_periods_per_day"), "maxPeriodsPerWeek": r.get("max_periods_per_week"),
            "teacherCode": r.get("teacher_code"),
        }
        for r in data
    ]


def update_teacher_subjects(teacher_id: str, subjects: list[str], ac) -> None:
    ac.table("teachers").update({"subjects": subjects, "subject": subjects[0] if subjects else ""}).eq("id", teacher_id).execute()


def update_teacher_workload_limits(teacher_id: str, max_periods_per_day: Optional[int], max_periods_per_week: Optional[int], ac) -> None:
    ac.table("teachers").update({"max_periods_per_day": max_periods_per_day, "max_periods_per_week": max_periods_per_week}).eq("id", teacher_id).execute()


def remove_teacher_from_school(teacher_id: str, ac) -> None:
    ac.table("teachers").update({"school_id": None}).eq("id", teacher_id).execute()


# ── Classes ─────────────────────────────────────────────────────────────────

def fetch_school_classes(school_id: str, ac) -> list[dict]:
    data = ac.table("classes").select("*").eq("school_id", school_id).execute().data or []
    return [_map_class(r) for r in data]


def _map_class(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r.get("teacher_id"), "schoolName": r.get("school_name") or "",
        "schoolId": r.get("school_id"), "name": r["name"], "grade": r.get("grade") or "",
        "section": r.get("section") or "", "academicYear": r.get("academic_year") or "",
        "createdAt": r.get("created_at"), "classCode": r.get("class_code"),
    }


def admin_create_class(cls: dict, ac) -> None:
    ac.table("classes").insert(
        {
            "id": cls["id"], "teacher_id": cls.get("teacherId"), "school_name": cls.get("schoolName"),
            "school_id": cls["schoolId"], "name": cls["name"], "grade": cls["grade"], "section": cls["section"],
            "academic_year": cls.get("academicYear"), "created_at": cls["createdAt"], "class_code": cls.get("classCode"),
        }
    ).execute()


def admin_delete_class(class_id: str, ac) -> None:
    ac.table("classes").delete().eq("id", class_id).execute()


# ── Students ────────────────────────────────────────────────────────────────

def _map_student(r: dict) -> dict:
    return {
        "id": r["id"], "teacherId": r.get("teacher_id"), "classId": r.get("class_id"),
        "name": r["name"], "rollNumber": r["roll_number"], "isActive": r.get("is_active", True),
        "interests": r.get("interests") or [], "goal": r.get("goal") or "",
        "pin": r.get("pin"), "studentCode": r.get("student_code"),
    }


def fetch_class_students(class_id: str, ac) -> list[dict]:
    data = ac.table("students").select("*").eq("class_id", class_id).order("roll_number").execute().data or []
    return [_map_student(r) for r in data]


def bulk_insert_students(class_id: str, rows: list[dict], ac) -> None:
    records = [
        {
            "id": r["id"], "teacher_id": None, "class_id": class_id, "name": r["name"],
            "roll_number": r["rollNumber"], "is_active": True, "interests": [], "goal": "",
            "student_code": r["studentCode"],
        }
        for r in rows
    ]
    ac.table("students").upsert(records, on_conflict="id").execute()


def delete_student(student_id: str, ac) -> None:
    ac.table("students").delete().eq("id", student_id).execute()


# ── Teacher-class assignments ────────────────────────────────────────────────

def assign_teacher_to_class(id_: str, teacher_id: str, class_id: str, ac, subject: Optional[str] = None) -> None:
    from datetime import datetime, timezone
    ac.table("teacher_class_assignments").upsert(
        {"id": id_, "teacher_id": teacher_id, "class_id": class_id, "subject": subject, "created_at": datetime.now(timezone.utc).isoformat()},
        on_conflict="teacher_id,class_id",
    ).execute()


def remove_teacher_from_class(teacher_id: str, class_id: str, ac) -> None:
    ac.table("teacher_class_assignments").delete().eq("teacher_id", teacher_id).eq("class_id", class_id).execute()


def fetch_class_assignments(class_id: str, ac) -> list[dict]:
    data = ac.table("teacher_class_assignments").select("*").eq("class_id", class_id).execute().data or []
    return [{"teacherId": r["teacher_id"], "classId": r["class_id"], "subject": r.get("subject")} for r in data]


# ── School timetable (draft periods) ─────────────────────────────────────────

def _map_timetable_period(r: dict) -> dict:
    return {
        "id": r["id"], "schoolId": r.get("school_id"), "dayOfWeek": r["day_of_week"],
        "periodNumber": r["period_number"], "startTime": r["start_time"], "endTime": r["end_time"],
        "classId": r["class_id"], "teacherId": r.get("teacher_id"), "label": r.get("label"),
        "createdAt": r.get("created_at"),
    }


def fetch_school_timetable(school_id: str, ac) -> list[dict]:
    data = ac.table("school_timetable_periods").select("*").eq("school_id", school_id).execute().data or []
    return [_map_timetable_period(r) for r in data]


def upsert_school_timetable_period(p: dict, ac) -> None:
    ac.table("school_timetable_periods").upsert(
        {
            "id": p["id"], "school_id": p["schoolId"], "day_of_week": p["dayOfWeek"],
            "period_number": p["periodNumber"], "start_time": p["startTime"], "end_time": p["endTime"],
            "class_id": p["classId"], "teacher_id": p.get("teacherId"), "label": p.get("label"),
            "created_at": p["createdAt"],
        }
    ).execute()


def delete_school_timetable_period(period_id: str, ac) -> None:
    ac.table("school_timetable_periods").delete().eq("id", period_id).execute()


def delete_school_timetable_periods_for_classes(class_ids: list[str], ac) -> None:
    if not class_ids:
        return
    ac.table("school_timetable_periods").delete().in_("class_id", class_ids).execute()


def bulk_insert_school_timetable_periods(periods: list[dict], ac) -> None:
    if not periods:
        return
    ac.table("school_timetable_periods").insert(
        [
            {
                "id": p["id"], "school_id": p["schoolId"], "day_of_week": p["dayOfWeek"],
                "period_number": p["periodNumber"], "start_time": p["startTime"], "end_time": p["endTime"],
                "class_id": p["classId"], "teacher_id": p.get("teacherId"), "label": p.get("label"),
                "created_at": p["createdAt"],
            }
            for p in periods
        ]
    ).execute()


def publish_timetable_for_classes(class_ids: list[str], ac) -> int:
    """Publish just the given classes' current draft periods to their
    teachers, leaving every other class/teacher's already-published
    timetable untouched. Scoped by class_id (not teacher_id) so a teacher
    who also teaches classes outside this set keeps those entries intact."""
    import uuid

    if not class_ids:
        return 0

    data = ac.table("school_timetable_periods").select("*").in_("class_id", class_ids).execute().data or []
    rows = [
        {
            "id": str(uuid.uuid4()), "teacher_id": p["teacher_id"], "class_id": p["class_id"],
            "day_of_week": p["day_of_week"], "period_number": p["period_number"],
            "start_time": p["start_time"], "end_time": p["end_time"], "label": p.get("label"),
        }
        for p in data if p.get("teacher_id")
    ]

    ac.table("timetable").delete().in_("class_id", class_ids).execute()
    if rows:
        ac.table("timetable").insert(rows).execute()
    return len(rows)


# ── Exam plan ─────────────────────────────────────────────────────────────────

def fetch_exam_plan_items(school_id: str, ac) -> list[dict]:
    data = ac.table("exam_plan_items").select("*").eq("school_id", school_id).order("order_index").execute().data or []
    return [
        {"id": r["id"], "schoolId": r["school_id"], "name": r["name"], "count": r.get("count", 1),
         "orderIndex": r.get("order_index", 0), "createdAt": r.get("created_at")}
        for r in data
    ]


def upsert_exam_plan_item(item: dict, ac) -> None:
    ac.table("exam_plan_items").upsert({
        "id": item["id"], "school_id": item["schoolId"], "name": item["name"],
        "count": item["count"], "order_index": item["orderIndex"], "created_at": item["createdAt"],
    }).execute()


def delete_exam_plan_item(id_: str, ac) -> None:
    ac.table("exam_plan_items").delete().eq("id", id_).execute()


# ── Announcements ─────────────────────────────────────────────────────────────

def fetch_school_announcements(school_id: str, ac) -> list[dict]:
    data = (
        ac.table("announcements").select("*").eq("school_id", school_id)
        .order("created_at", desc=True).execute().data or []
    )
    return [
        {
            "id": r["id"], "schoolId": r["school_id"], "adminId": r.get("admin_id"),
            "adminName": r.get("admin_name") or "Admin", "title": r["title"], "body": r["body"],
            "category": r.get("category") or "general", "createdAt": r.get("created_at"),
        }
        for r in data
    ]


def create_announcement(a: dict, ac) -> None:
    ac.table("announcements").insert({
        "id": a["id"], "school_id": a["schoolId"], "admin_id": a["adminId"], "admin_name": a["adminName"],
        "title": a["title"], "body": a["body"], "category": a["category"], "created_at": a["createdAt"],
    }).execute()


def delete_announcement(id_: str, school_id: str, ac) -> None:
    ac.table("announcements").delete().eq("id", id_).eq("school_id", school_id).execute()


# ── Academic events: publish ─────────────────────────────────────────────────

def publish_academic_events(school_id: str, ac) -> int:
    res = ac.table("academic_events").update({"published": True}).eq("school_id", school_id).eq("published", False).execute()
    return len(res.data or [])


# ── School schedule template ─────────────────────────────────────────────────

def fetch_school_schedule(school_id: str, ac) -> Optional[dict]:
    res = ac.table("school_schedule").select("*").eq("school_id", school_id).maybe_single().execute()
    data = res.data if res else None
    if not data:
        return None
    return {"id": data["id"], "schoolId": data["school_id"], "slots": data.get("slots") or [], "createdAt": data.get("created_at")}


def upsert_school_schedule(schedule: dict, ac) -> None:
    from datetime import datetime, timezone
    ac.table("school_schedule").upsert({
        "id": schedule["id"], "school_id": schedule["schoolId"], "slots": schedule["slots"],
        "created_at": schedule["createdAt"], "updated_at": datetime.now(timezone.utc).isoformat(),
    }).execute()


# ── Teacher availability & substitutes ───────────────────────────────────────

def fetch_teacher_availability(school_id: str, date: str, ac) -> list[dict]:
    data = ac.table("teacher_availability").select("*").eq("school_id", school_id).eq("date", date).execute().data or []
    return [
        {
            "id": r["id"], "schoolId": r["school_id"], "teacherId": r["teacher_id"], "date": r["date"],
            "reason": r["reason"], "note": r.get("note"), "source": r.get("source") or "admin",
            "status": r.get("status") or "approved",
        }
        for r in data
    ]


def fetch_teacher_availability_for_teacher(teacher_id: str, from_date: str, ac) -> list[dict]:
    """The calling teacher's own upcoming availability rows (today onward),
    for the self-service leave-request view — school-wide `fetch_teacher_availability`
    is scoped by date+school, this is scoped by teacher across a date range instead."""
    data = (
        ac.table("teacher_availability").select("*")
        .eq("teacher_id", teacher_id).gte("date", from_date).order("date").execute().data or []
    )
    return [
        {
            "id": r["id"], "schoolId": r["school_id"], "teacherId": r["teacher_id"], "date": r["date"],
            "reason": r["reason"], "note": r.get("note"), "source": r.get("source") or "admin",
            "status": r.get("status") or "approved",
        }
        for r in data
    ]


def upsert_teacher_availability(a: dict, ac) -> None:
    from datetime import datetime, timezone
    ac.table("teacher_availability").upsert(
        {
            "id": a["id"], "school_id": a["schoolId"], "teacher_id": a["teacherId"], "date": a["date"],
            "reason": a["reason"], "note": a.get("note"), "source": a["source"],
            "status": a.get("status") or "approved",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        on_conflict="teacher_id,date",
    ).execute()


def create_pending_leave_request(school_id: str, teacher_id: str, date: str, reason: str, note: Optional[str], ac) -> None:
    """The teacher-submitted multi-day /leaves request, before admin review —
    unlike mark_teacher_unavailable, this deliberately does NOT touch
    substitute assignments yet; that only happens once an admin approves."""
    upsert_teacher_availability(
        {"id": f"{teacher_id}-{date}", "schoolId": school_id, "teacherId": teacher_id, "date": date,
         "reason": reason, "note": note, "source": "teacher", "status": "pending"},
        ac,
    )


def fetch_pending_leave_requests(school_id: str, ac) -> list[dict]:
    """Every teacher-submitted, not-yet-decided leave row across all dates
    (today onward is enforced at submission time, so nothing here is stale)."""
    data = (
        ac.table("teacher_availability").select("*")
        .eq("school_id", school_id).eq("status", "pending").order("date").execute().data or []
    )
    return [
        {
            "id": r["id"], "schoolId": r["school_id"], "teacherId": r["teacher_id"], "date": r["date"],
            "reason": r["reason"], "note": r.get("note"), "source": r.get("source") or "admin",
            "status": r.get("status") or "approved",
        }
        for r in data
    ]


def delete_teacher_availability(teacher_id: str, date: str, ac) -> None:
    ac.table("teacher_availability").delete().eq("teacher_id", teacher_id).eq("date", date).execute()


def fetch_substitutions_for_date(school_id: str, date: str, ac) -> list[dict]:
    data = ac.table("timetable_substitutions").select("*").eq("school_id", school_id).eq("date", date).execute().data or []
    return [
        {
            "id": r["id"], "schoolId": r["school_id"], "date": r["date"], "dayOfWeek": r["day_of_week"],
            "periodNumber": r["period_number"], "classId": r["class_id"], "subject": r.get("subject"),
            "originalTeacherId": r["original_teacher_id"], "substituteTeacherId": r.get("substitute_teacher_id"),
            "status": r["status"],
        }
        for r in data
    ]


def upsert_substitutions(rows: list[dict], ac) -> None:
    if not rows:
        return
    from datetime import datetime, timezone
    ac.table("timetable_substitutions").upsert(
        [
            {
                "id": r["id"], "school_id": r["schoolId"], "date": r["date"], "day_of_week": r["dayOfWeek"],
                "period_number": r["periodNumber"], "class_id": r["classId"], "subject": r.get("subject"),
                "original_teacher_id": r["originalTeacherId"], "substitute_teacher_id": r.get("substituteTeacherId"),
                "status": r["status"], "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for r in rows
        ],
        on_conflict="class_id,date,period_number",
    ).execute()


def delete_substitutions_for_teacher_on_date(teacher_id: str, date: str, ac) -> None:
    ac.table("timetable_substitutions").delete().eq("original_teacher_id", teacher_id).eq("date", date).execute()


def update_substitute_assignment(substitution_id: str, substitute_teacher_id: str, ac) -> None:
    from datetime import datetime, timezone
    ac.table("timetable_substitutions").update({
        "substitute_teacher_id": substitute_teacher_id, "status": "manual",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }).eq("id", substitution_id).execute()


def _week_range_of(date: str) -> tuple[str, str]:
    """Monday-Saturday range containing `date` (school days are 1=Mon..6=Sat)."""
    from datetime import datetime, timedelta
    d = datetime.strptime(date, "%Y-%m-%d")
    dow = d.isoweekday()  # 1=Mon..7=Sun, matches JS's `d.getDay() || 7` treatment of Sunday
    monday = d - timedelta(days=dow - 1)
    saturday = monday + timedelta(days=5)
    return monday.strftime("%Y-%m-%d"), saturday.strftime("%Y-%m-%d")


def fetch_teacher_eligibility_data(school_id: str, date: str, ac) -> list[dict]:
    """Builds one SubstituteCandidate per school teacher. Teacher.subject/grade
    aren't reliable (grade is never actually set), so `subjectsTaught` is
    derived empirically from teacher_class_assignments and the labels on the
    teacher's own published timetable rows. `weeklyLoad` is their regular
    weekly period count plus any substitute periods they've already picked up
    this week, so workload caps account for both."""
    teachers = fetch_school_teachers(school_id, ac)
    teacher_ids = [t["id"] for t in teachers]
    if not teacher_ids:
        return []

    start, end = _week_range_of(date)

    asg_data = ac.table("teacher_class_assignments").select("teacher_id, subject").in_("teacher_id", teacher_ids).execute().data or []
    tt_data = ac.table("timetable").select("teacher_id, day_of_week, period_number, label").in_("teacher_id", teacher_ids).execute().data or []
    subs_data = (
        ac.table("timetable_substitutions").select("substitute_teacher_id")
        .in_("substitute_teacher_id", teacher_ids).gte("date", start).lte("date", end).execute().data or []
    )

    subjects_by_teacher: dict[str, set] = {}
    busy_by_teacher: dict[str, set] = {}
    extra_load_by_teacher: dict[str, int] = {}

    for r in asg_data:
        if r.get("subject"):
            subjects_by_teacher.setdefault(r["teacher_id"], set()).add(r["subject"])
    for r in tt_data:
        if r.get("label"):
            subjects_by_teacher.setdefault(r["teacher_id"], set()).add(r["label"])
        busy_by_teacher.setdefault(r["teacher_id"], set()).add(f"{r['day_of_week']}|{r['period_number']}")
    for r in subs_data:
        tid = r.get("substitute_teacher_id")
        if tid:
            extra_load_by_teacher[tid] = extra_load_by_teacher.get(tid, 0) + 1

    result = []
    for t in teachers:
        busy_slots = busy_by_teacher.get(t["id"], set())
        result.append({
            "teacherId": t["id"], "name": t["name"],
            "subjectsTaught": subjects_by_teacher.get(t["id"], set()),
            "busySlots": busy_slots,
            "maxPeriodsPerDay": t.get("maxPeriodsPerDay"), "maxPeriodsPerWeek": t.get("maxPeriodsPerWeek"),
            "weeklyLoad": len(busy_slots) + extra_load_by_teacher.get(t["id"], 0),
        })
    return result


def mark_teacher_unavailable(
    school_id: str, teacher_id: str, date: str, reason: str, source: str, note: Optional[str], ac,
    status: str = "approved",
) -> list[dict]:
    """Marks a teacher unavailable for a date and (re)computes substitute
    assignments for their affected periods. Shared by the admin "set status"
    flow, the teacher's own daily check-in, and (with status="approved",
    called only once the admin approves) the multi-day leave-request flow.
    Returns the newly-computed substitution rows (camelCase, same shape as
    fetch_substitutions_for_date) so a caller can notify freshly-assigned
    substitutes — every row here is a fresh computation, never a pre-existing
    one, since this function is the only thing that (re)writes them."""
    from datetime import datetime
    from .substitute_finder import find_substitute

    upsert_teacher_availability({"id": f"{teacher_id}-{date}", "schoolId": school_id, "teacherId": teacher_id, "date": date, "reason": reason, "note": note, "source": source, "status": status}, ac)

    day_of_week = datetime.strptime(date, "%Y-%m-%d").isoweekday() % 7  # JS getDay(): 0=Sun..6=Sat

    need_rows = ac.table("timetable").select("*").eq("teacher_id", teacher_id).eq("day_of_week", day_of_week).execute().data or []
    availability = fetch_teacher_availability(school_id, date, ac)
    existing_subs = fetch_substitutions_for_date(school_id, date, ac)
    candidates = fetch_teacher_eligibility_data(school_id, date, ac)

    exclude_teacher_ids = {teacher_id, *(a["teacherId"] for a in availability)}

    # Seed "already used this slot today" from substitutions already assigned
    # for OTHER teachers' absences today, keyed by day|period, so two absent
    # teachers don't get double-booked to the same substitute for one period.
    used_by_slot: dict[str, set] = {}
    for s in existing_subs:
        if s["originalTeacherId"] == teacher_id:
            continue  # being recomputed below
        if not s.get("substituteTeacherId"):
            continue
        key = f"{s['dayOfWeek']}|{s['periodNumber']}"
        used_by_slot.setdefault(key, set()).add(s["substituteTeacherId"])

    new_subs = []
    for row in need_rows:
        need = {"dayOfWeek": day_of_week, "periodNumber": row["period_number"], "subject": row.get("label") or ""}
        slot_key = f"{need['dayOfWeek']}|{need['periodNumber']}"
        used_this_slot = used_by_slot.get(slot_key, set())

        # Prefer a subject-qualified substitute; if none is free, fall back to
        # any available teacher (still respecting busy slots and workload
        # caps) rather than leaving the period unresolved.
        substitute_id = find_substitute(need, candidates, exclude_teacher_ids, used_this_slot)
        subject_matched = substitute_id is not None
        if not substitute_id:
            substitute_id = find_substitute(need, candidates, exclude_teacher_ids, used_this_slot, require_subject_match=False)
        if substitute_id:
            used_by_slot.setdefault(slot_key, set()).add(substitute_id)

        if substitute_id:
            status = "assigned" if subject_matched else "assigned_fallback"
        else:
            status = "unresolved"
        new_subs.append({
            "id": f"{row['class_id']}-{date}-{row['period_number']}",
            "schoolId": school_id, "date": date, "dayOfWeek": day_of_week, "periodNumber": row["period_number"],
            "classId": row["class_id"], "subject": row.get("label"),
            "originalTeacherId": teacher_id, "substituteTeacherId": substitute_id,
            "status": status,
        })

    upsert_substitutions(new_subs, ac)
    return new_subs


def revert_teacher_availability(teacher_id: str, date: str, ac) -> None:
    delete_teacher_availability(teacher_id, date, ac)
    delete_substitutions_for_teacher_on_date(teacher_id, date, ac)
