"""Background batch-generation of shared prep material — one generation per
(school, grade, subject), consumed by every class/section teaching that
grade+subject, since the syllabus itself is already authored once per
grade+subject (admin_grade_syllabus.py's definition_id fan-out), not per
class. See migrations/0003_shared_prep_batches.sql.

Durable state lives in prep_batches / shared_prep_materials — a batch must be
visible as "in flight" across every teacher's request and every worker
process, unlike the PDF-ingestion job registry (syllabus_pdf_jobs.py), which
is single-admin and can afford to live only in that process's memory. The
in-memory _PROGRESS dict here is purely a nicer live message for a poll; the
durable prep_batches row is the source of truth for "is one already running."
"""
import asyncio
import datetime
import threading
import uuid
from typing import Optional

from .supabase_clients import create_admin_client

_PROGRESS: dict[str, dict] = {}
_LOCK = threading.Lock()

BATCH_SIZE = 15          # topics generated per batch — inside the "10 to 30 days" range at a typical pace
LOW_STOCK_THRESHOLD = 5  # top up once fewer than this many upcoming topics already have material


def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _maybe_single(query):
    """supabase-py's maybe_single().execute() returns None itself (not an
    object with .data = None) when zero rows match — mirrors scanner.py's
    guard of the same name, for the same reason."""
    res = query.execute()
    return res.data if res else None


def _set_progress(batch_id: str, **fields):
    with _LOCK:
        _PROGRESS.setdefault(batch_id, {}).update(fields)


def get_progress(batch_id: str) -> Optional[dict]:
    """Poller-facing: durable status/counts from prep_batches, topped up with
    whatever nicer in-flight message this process happens to have for it (a
    different process handling the batch has none — status/counts still work)."""
    ac = create_admin_client()
    row = _maybe_single(ac.table("prep_batches").select("*").eq("id", batch_id).maybe_single())
    if not row:
        return None
    with _LOCK:
        extra = dict(_PROGRESS.get(batch_id, {}))
    return {
        "id": row["id"], "status": row["status"], "topicCount": row["topic_count"],
        "completedCount": row["completed_count"], "error": row.get("error"),
        "message": extra.get("message"),
    }


def _grade_subject_topics(ac, school_id: str, grade: str, subject: str) -> list[dict]:
    """Same de-dupe-by-definition_id, order_index pattern as
    admin_grade_syllabus.get_grade_syllabus — the canonical topic list for
    this grade+subject, in teaching order.

    Scoped through classes rather than syllabus_topics.school_id directly —
    that column doesn't actually exist on the live table (confirmed against
    real data; admin_grade_syllabus.py's own .eq("school_id", ...) calls would
    raise if they ever ran against it). patch_grade_syllabus_progression
    already works around this the same way, for the same reason: "Ownership
    is derived through the CLASS rather than syllabus_topics' own school_id
    ... going through classes cannot be defeated that way.\""""
    class_ids = [
        c["id"] for c in
        (ac.table("classes").select("id").eq("school_id", school_id).eq("grade", grade).execute().data or [])
    ]
    if not class_ids:
        return []
    rows = (
        ac.table("syllabus_topics").select("id, definition_id, topic, order_index")
        .in_("class_id", class_ids).eq("subject", subject)
        .order("order_index").execute()
    ).data or []
    by_def: dict[str, dict] = {}
    for r in rows:
        def_id = r.get("definition_id") or r["id"]
        if def_id not in by_def:
            by_def[def_id] = {"definitionId": def_id, "topic": r["topic"], "orderIndex": r.get("order_index") or 0}
    return sorted(by_def.values(), key=lambda t: t["orderIndex"])


def stock_ahead(ac, school_id: str, grade: str, subject: str, from_order_index: int) -> int:
    """How many topics from this point in the syllabus onward already have
    shared material generated."""
    topics = [t for t in _grade_subject_topics(ac, school_id, grade, subject) if t["orderIndex"] >= from_order_index]
    if not topics:
        return 0
    def_ids = [t["definitionId"] for t in topics]
    have = (
        ac.table("shared_prep_materials").select("topic_definition_id")
        .eq("school_id", school_id).eq("grade", grade).eq("subject", subject)
        .in_("topic_definition_id", def_ids).execute()
    ).data or []
    have_ids = {r["topic_definition_id"] for r in have}
    return sum(1 for t in topics if t["definitionId"] in have_ids)


def ensure_stock(school_id: str, grade: str, subject: str, from_order_index: int, teacher_id: Optional[str]) -> Optional[str]:
    """Idempotent stock check + top-up trigger. Returns the new batch's id if
    a generation was started, None if stock is fine, a batch for this exact
    (school, grade, subject) is already pending/running, or this grade+subject
    is currently in full_personalization mode — there is no shared pool to
    maintain there, every teacher generates their own on fetch instead (see
    PrepMaterialModal's fetch path)."""
    from .generation_mode import recompute_grade_subject_mode
    ac = create_admin_client()
    if recompute_grade_subject_mode(ac, school_id, grade, subject) == "full_personalization":
        return None
    if stock_ahead(ac, school_id, grade, subject, from_order_index) >= LOW_STOCK_THRESHOLD:
        return None

    in_flight = (
        ac.table("prep_batches").select("id")
        .eq("school_id", school_id).eq("grade", grade).eq("subject", subject)
        .in_("status", ["pending", "running"]).limit(1).execute()
    ).data or []
    if in_flight:
        return None

    batch_id = str(uuid.uuid4())
    ac.table("prep_batches").insert({
        "id": batch_id, "school_id": school_id, "grade": grade, "subject": subject,
        "status": "pending", "topic_count": 0, "completed_count": 0,
    }).execute()

    thread = threading.Thread(
        target=_run_batch, args=(batch_id, school_id, grade, subject, teacher_id),
        daemon=True, name=f"prep-batch-{batch_id[:8]}",
    )
    thread.start()
    return batch_id


async def _pool_feedback(ac, school_id: str, grade: str, subject: str) -> Optional[str]:
    """Fold every class-session feedback for this grade+subject into one
    shared tendency profile — the same idea as classes.feedback_profile
    (migration 0001), but for the whole cohort sharing this batch's material
    rather than one class. Pools every class of this grade+subject in this
    school (not just since the last batch — lesson_feedback for a
    grade+subject is small enough that a full re-pool each batch is simpler
    and more accurate than tracking a "since" cursor, and costs one LLM call
    per batch, not per topic)."""
    from .ai import call_ai

    class_ids = [
        c["id"] for c in
        (ac.table("classes").select("id").eq("school_id", school_id).eq("grade", grade).execute().data or [])
    ]
    if not class_ids:
        return None

    rows = (
        ac.table("lesson_feedback").select("engagement, comprehension, pacing, other_feedback, insight")
        .in_("class_id", class_ids)
        .order("created_at", desc=True).limit(60)
        .execute()
    ).data or []
    if not rows:
        return None

    existing = _maybe_single(
        ac.table("grade_subject_feedback_profiles").select("profile")
        .eq("school_id", school_id).eq("grade", grade).eq("subject", subject)
        .maybe_single()
    )
    current_profile = (existing or {}).get("profile")

    lines = []
    for r in rows:
        bits = [p for p in [
            f"engagement={r['engagement']}" if r.get("engagement") else None,
            f"comprehension={r['comprehension']}" if r.get("comprehension") else None,
            f"pacing={r['pacing']}" if r.get("pacing") else None,
            r.get("insight"), r.get("other_feedback"),
        ] if p]
        if bits:
            lines.append("- " + "; ".join(bits))
    if not lines:
        return current_profile

    prompt = f"""Below is a bunch of post-class feedback entries collected across every section teaching Grade {grade} {subject} in this school — different classes, same syllabus.

{chr(10).join(lines[:60])}

Existing tendency profile for this grade+subject cohort (may be empty): {current_profile or '(none yet)'}

Produce ONE short paragraph (2-3 sentences) describing this cohort's general tendencies — pacing, engagement style, what kind of activities land, common trouble spots — building on the existing profile rather than replacing it wholesale. This will be used to shape the next batch of shared lesson material for this grade+subject.

Return valid JSON only: {{"profile": "..."}}"""

    try:
        result = await call_ai([{"role": "user", "content": prompt}])
        import json
        parsed = json.loads(result)
        profile = parsed.get("profile") or current_profile
    except Exception as e:
        print(f"[prep-batch] feedback pooling failed, keeping existing profile: {e}")
        profile = current_profile

    if profile:
        ac.table("grade_subject_feedback_profiles").upsert({
            "school_id": school_id, "grade": grade, "subject": subject,
            "profile": profile, "updated_at": _now_iso(),
        }, on_conflict="school_id,grade,subject").execute()
    return profile


async def _generate_one(ac, school_id: str, grade: str, subject: str, t: dict,
                         teaching_profile: Optional[dict], previous_topic: Optional[str],
                         avoid_activities: list, pooled_profile: Optional[str]) -> dict:
    from .prep_context import fetch_grounding
    from .textbook_grounding import fetch_textbook_grounding
    from ..routes.smart_lesson_routes import generate_lesson_core, generate_lesson_images

    grounding = await fetch_grounding(ac, t["definitionId"])
    textbook = await fetch_textbook_grounding(ac, school_id, grade, subject, t["topic"], None)

    lesson = await generate_lesson_core(
        topic=t["topic"], subject=subject, grade=grade, subtopic=None,
        total_students=None, class_interests=[], weak_topics=[],
        teaching_profile=teaching_profile, grounding=grounding, textbook=textbook,
        previous_topic=previous_topic, avoid_activities=avoid_activities,
        feedback_context={"topicInsight": None, "classProfile": pooled_profile},
        context_note=None,
    )
    await generate_lesson_images(lesson, textbook, ac, [school_id, grade, subject, t["topic"]])
    return lesson


def _run_batch(batch_id: str, school_id: str, grade: str, subject: str, teacher_id: Optional[str]):
    ac = create_admin_client()
    try:
        ac.table("prep_batches").update({"status": "running"}).eq("id", batch_id).execute()
        _set_progress(batch_id, message="Starting…")

        all_topics = _grade_subject_topics(ac, school_id, grade, subject)
        have = (
            ac.table("shared_prep_materials").select("topic_definition_id")
            .eq("school_id", school_id).eq("grade", grade).eq("subject", subject).execute()
        ).data or []
        have_ids = {r["topic_definition_id"] for r in have}
        todo = [t for t in all_topics if t["definitionId"] not in have_ids][:BATCH_SIZE]

        ac.table("prep_batches").update({"topic_count": len(todo)}).eq("id", batch_id).execute()
        if not todo:
            ac.table("prep_batches").update({"status": "done", "completed_at": _now_iso()}).eq("id", batch_id).execute()
            _set_progress(batch_id, message="Nothing to generate — already fully stocked")
            return

        pooled_profile = asyncio.run(_pool_feedback(ac, school_id, grade, subject))

        teaching_profile = None
        if teacher_id:
            row = _maybe_single(ac.table("teachers").select("teaching_profile").eq("id", teacher_id).maybe_single())
            teaching_profile = (row or {}).get("teaching_profile")

        # First topic of the batch may still have a real predecessor from
        # earlier in the grade+subject's full syllabus — look it up once.
        first_idx = all_topics.index(todo[0])
        previous_topic = all_topics[first_idx - 1]["topic"] if first_idx > 0 else None

        avoid_activities: list = []
        completed = 0
        for i, t in enumerate(todo):
            _set_progress(batch_id, message=f"Generating {t['topic']}… ({i + 1}/{len(todo)})")
            try:
                lesson = asyncio.run(_generate_one(
                    ac, school_id, grade, subject, t, teaching_profile, previous_topic, avoid_activities, pooled_profile,
                ))
            except Exception as e:
                print(f"[prep-batch] topic '{t['topic']}' failed, skipping: {e}")
                previous_topic = t["topic"]
                continue

            ac.table("shared_prep_materials").upsert({
                "school_id": school_id, "grade": grade, "subject": subject,
                "topic_definition_id": t["definitionId"], "topic": t["topic"], "subtopic": None,
                "order_index": t["orderIndex"], "lesson": lesson, "batch_id": batch_id,
            }, on_conflict="school_id,grade,subject,topic_definition_id").execute()

            activity = (lesson.get("challenge") or {}).get("activity")
            if isinstance(activity, str) and activity:
                avoid_activities = ([activity] + avoid_activities)[:5]
            previous_topic = t["topic"]
            completed += 1
            ac.table("prep_batches").update({"completed_count": completed}).eq("id", batch_id).execute()
            _set_progress(batch_id, completedCount=completed)

        ac.table("prep_batches").update({"status": "done", "completed_at": _now_iso()}).eq("id", batch_id).execute()
        _set_progress(batch_id, message=f"{completed} of {len(todo)} lessons ready")
        if completed:
            from .admin_notifications import create_admin_notification
            create_admin_notification(
                school_id, "prep_batch_generated",
                f"Grade {grade} {subject}: {completed} new prep material{'s' if completed != 1 else ''} generated (shared mode).",
                ac,
            )
    except Exception as e:
        print(f"[prep-batch] batch {batch_id} failed: {e}")
        try:
            ac.table("prep_batches").update({"status": "error", "error": str(e)}).eq("id", batch_id).execute()
        except Exception:
            pass
        _set_progress(batch_id, message="Failed", error=str(e))
