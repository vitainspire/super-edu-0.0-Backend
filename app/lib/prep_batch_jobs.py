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

Topics whose grade+subject has a published textbook chapter covering them are
generated through the newer agentic pipeline (prep_pipeline_bridge.py) instead
of generate_lesson_core — richer, better-grounded material, run once per
matched chapter rather than once per topic. context_flow (per-classroom
adaptation) is never invoked here: shared/opt_in material is pooled across
every section by design, with no one classroom to adapt to. Any topic that
pipeline can't confidently cover (no published textbook for this grade+
subject, no confident title match to one of its chapters, or the pipeline
itself failing) falls straight through to the original engine below — a
school with no textbook ingested for a subject must keep working exactly as
it does today.

save_published_chapter_lessons() is a separate, on-demand entry point: given
just a school_id plus a published-catalog book_id/chapter_number, it runs
that one chapter through the same agentic pipeline and persists the result
directly — no prep_batches row, no backlog draining, no fallback to the old
engine. Use it to seed material for one specific chapter right away rather
than waiting for ensure_stock()'s automatic top-up to reach it.
"""
import asyncio
import datetime
import json
import os
import threading
import time
import uuid
from typing import Optional

from .supabase_clients import create_admin_client, retry_supabase
from .textbook_grounding import MIN_TITLE_OVERLAP, title_score

# Insurance against exactly what save_published_chapter_lessons's own
# real-money spend is exposed to: the DB save is the ONLY part of that
# function that can still fail after every retry (a genuine, sustained
# outage, not just one stale connection) -- and by the time it runs, the
# LLM work is already paid for. Writing the computed result here BEFORE
# attempting to save it means that failure can never cost the run its
# result: recover_chapter_save() below replays only the free DB-write half
# from this file, no LLM call involved, whenever it's run again. Cleared the
# moment persistence actually succeeds -- a leftover file here always means
# "this one didn't finish saving," never a duplicate of one that did.
_BACKUP_DIR = os.path.join("var", "chapter_save_backups")


def _backup_path(school_id: str, book_id: str, chapter_number: int) -> str:
    safe_book_id = "".join(c if c.isalnum() or c in "-_" else "_" for c in book_id)
    return os.path.join(_BACKUP_DIR, f"{school_id}__{safe_book_id}__{chapter_number}.json")


def _write_chapter_backup(school_id: str, book_id: str, chapter_number: int, result: dict) -> str:
    os.makedirs(_BACKUP_DIR, exist_ok=True)
    path = _backup_path(school_id, book_id, chapter_number)
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"school_id": school_id, "book_id": book_id, "chapter_number": chapter_number,
                   "result": result}, f)
    return path


def _clear_chapter_backup(school_id: str, book_id: str, chapter_number: int) -> None:
    try:
        os.remove(_backup_path(school_id, book_id, chapter_number))
    except FileNotFoundError:
        pass


def recover_chapter_save(backup_path: str, teacher_id: Optional[str] = None) -> dict:
    """Replays ONLY the DB-save half of a chapter run whose result was
    computed and backed up but never (or not fully) persisted -- reads the
    real, already-generated `result` back from disk and hands it straight to
    _persist_generated_chapter(), so this costs nothing: no book_id/chapter
    re-fetch, no LLM call, nothing OpenRouter can bill for. Call this by hand
    (or from a small script) with the path a failed save's own log line
    printed. Clears the backup file itself on success, same as a normal run."""
    with open(backup_path, "r", encoding="utf-8") as f:
        saved = json.load(f)
    outcome = _persist_generated_chapter(
        saved["school_id"], saved["book_id"], saved["chapter_number"], saved["result"], teacher_id)
    _clear_chapter_backup(saved["school_id"], saved["book_id"], saved["chapter_number"])
    return outcome

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


def _fetch_published_chapters(ac, school_id: str, grade: str, subject: str) -> list[dict]:
    """Every real chapter available for this grade+subject, whole (not a
    matched slice -- the pipeline bridge wants a whole chapter).

    Sourced the same way textbook_grounding.py's own catalog-mirror fallback
    is: syllabus_chapters (the real state curriculum's chapter list -- not
    scoped by school_id, since it's shared curriculum, not a per-school
    ingestion) joined against textbook_catalog.py's local mirror of the
    published-textbook API for each chapter's actual content. school_id is
    accepted (kept for signature/call-site parity) but unused for the same
    reason. Never raises past this function -- a grade+subject with no
    syllabus_chapters rows, or no matching catalog book, just yields []."""
    from .textbook_catalog import find_textbook_chapter, get_chapter_content

    try:
        rows = (
            ac.table("syllabus_chapters").select("chapter_number, title, page_start, page_end")
            .eq("grade", str(grade)).ilike("subject", subject).execute()
        ).data or []
    except Exception as e:
        print(f"[prep-batch] syllabus_chapters lookup failed: {e}")
        return []

    chapters = []
    for r in rows:
        catalog_chapter = find_textbook_chapter(grade, subject, r.get("chapter_number"), r.get("title"), ac)
        if not catalog_chapter:
            continue
        content = get_chapter_content(catalog_chapter["book_id"], catalog_chapter["chapter_number"], ac)
        if not content or not content.get("content"):
            continue
        chapters.append({
            "id": f"{catalog_chapter['book_id']}:{catalog_chapter['chapter_number']}",
            "chapter_number": catalog_chapter["chapter_number"],
            "chapter_title": content.get("chapter_title") or catalog_chapter.get("chapter_title"),
            "page_start": content.get("page_start") or catalog_chapter.get("page_start"),
            "page_end": content.get("page_end") or catalog_chapter.get("page_end"),
            "content_markdown": content["content"],
        })
    return sorted(chapters, key=lambda c: c["chapter_number"])


def _pipeline_lessons_for_todo(ac, school_id: str, grade: str, subject: str, todo: list[dict]) -> dict[str, dict]:
    """Best-effort: {definitionId: lesson} for whichever of `todo` the new
    agentic pipeline could confidently cover. Never raises -- a chapter that
    fails, or a school with no textbook ingested for this grade+subject, just
    means an empty (or partial) dict, and the caller falls back to the
    original engine for anything missing."""
    import asyncio as _asyncio

    from .prep_pipeline_bridge import PipelineError, generate_chapter_lessons

    try:
        chapters = _fetch_published_chapters(ac, school_id, grade, subject)
    except Exception as e:
        # textbook_books/textbook_chapters aren't provisioned in every
        # deployment (this one included -- see textbook_grounding.py's own
        # catalog-mirror fallback for the same gap). That must not fail the
        # whole batch; it just means the pipeline can't cover anything this
        # round, same as "no textbook ingested for this grade+subject".
        print(f"[prep-batch] local textbook lookup unavailable, skipping agentic pipeline: {e}")
        return {}
    if not chapters:
        return {}

    # Which chapter each todo topic best matches, by the same title-overlap
    # scoring textbook_grounding already uses for live grounding -- one
    # consistent matching scheme across the app rather than a second one.
    by_chapter: dict[str, list[dict]] = {}
    for t in todo:
        best_chapter, best_score = None, 0.0
        for chapter in chapters:
            score = title_score(t["topic"], chapter["chapter_title"])
            if score > best_score:
                best_chapter, best_score = chapter, score
        if best_chapter is not None and best_score >= MIN_TITLE_OVERLAP:
            by_chapter.setdefault(best_chapter["id"], []).append(t)

    covered: dict[str, dict] = {}
    for chapter in chapters:
        matched_topics = by_chapter.get(chapter["id"])
        if not matched_topics:
            continue
        try:
            result = _asyncio.run(generate_chapter_lessons(
                grade=grade, subject=subject, chapter_title=chapter["chapter_title"],
                chapter_markdown=chapter["content_markdown"],
                chapter_number=chapter.get("chapter_number"),
                page_start=chapter.get("page_start"), page_end=chapter.get("page_end"),
            ))
        except PipelineError as e:
            print(f"[prep-batch] agentic pipeline skipped chapter '{chapter['chapter_title']}': {e}")
            continue
        except Exception as e:  # noqa: BLE001 -- one chapter's failure must not cost the others
            print(f"[prep-batch] agentic pipeline failed on chapter '{chapter['chapter_title']}': {e}")
            continue

        pipeline_lessons = result.get("lessons") or []
        used_pipeline_indices: set[int] = set()
        for t in matched_topics:
            best_i, best_score = None, 0.0
            for i, entry in enumerate(pipeline_lessons):
                if i in used_pipeline_indices:
                    continue
                score = title_score(t["topic"], entry["topic"])
                if score > best_score:
                    best_i, best_score = i, score
            if best_i is not None and best_score >= MIN_TITLE_OVERLAP:
                used_pipeline_indices.add(best_i)
                covered[t["definitionId"]] = pipeline_lessons[best_i]["lesson"]

    return covered


async def save_published_chapter_lessons(
    school_id: str, book_id: str, chapter_number: int, teacher_id: Optional[str] = None,
) -> dict:
    """On-demand counterpart to _run_batch's per-topic loop: fetches one
    published-catalog chapter, runs it through the agentic pipeline
    (prep_pipeline_bridge.generate_lessons_from_published_book), and persists
    every shippable lesson into shared_prep_materials right away — no
    prep_batches row, no background thread, since this targets one chosen
    book/chapter directly rather than draining a school's syllabus backlog.

    The real money is spent by the time `result` comes back below -- so it's
    backed up to disk (_write_chapter_backup) BEFORE _persist_generated_chapter
    is even attempted. A save failure that survives retry_supabase's one retry
    (a sustained outage, not just one stale connection) still leaves that
    result sitting in var/chapter_save_backups/, recoverable later via
    recover_chapter_save() for free -- no re-fetch, no LLM call. The backup is
    cleared the moment persistence actually succeeds.

    Raises ValueError if this school has no classes for the chapter's grade
    (nothing to fan a new topic out to). Propagates PublishedBookNotFound /
    PipelineError from the bridge for a bad book_id/chapter_number or a
    chapter the pipeline couldn't generate usable material from.
    """
    from .prep_pipeline_bridge import generate_lessons_from_published_book

    result = await generate_lessons_from_published_book(book_id, chapter_number)
    backup_path = _write_chapter_backup(school_id, book_id, chapter_number, result)
    try:
        outcome = _persist_generated_chapter(school_id, book_id, chapter_number, result, teacher_id)
    except Exception as e:
        print(f"[prep-batch] save failed after generation already completed -- "
              f"result is safe, not lost, at {backup_path}: {e}")
        print(f"[prep-batch] once the underlying issue clears, recover for free with: "
              f"recover_chapter_save({backup_path!r})")
        raise
    _clear_chapter_backup(school_id, book_id, chapter_number)
    return outcome


def _persist_generated_chapter(
    school_id: str, book_id: str, chapter_number: int, result: dict, teacher_id: Optional[str] = None,
) -> dict:
    """The DB-only half of save_published_chapter_lessons -- everything after
    the LLM work is done, split out so recover_chapter_save() can replay just
    this part against an already-computed (and backed-up) `result`.

    A generated topic that title-matches (>= MIN_TITLE_OVERLAP, the same
    scheme _pipeline_lessons_for_todo uses) an existing syllabus_topics
    definition_id for this school+grade+subject is saved under that
    definition_id, extending a topic that's already on the syllabus. A topic
    with no confident match gets a brand new syllabus_topics row, fanned
    across every class of this grade in this school — the same one-row-per-
    section/shared-definition_id pattern admin_schools.post_syllabus_topic
    and syllabus_persist.persist_extraction both use — so the shared material
    has a topic to hang off of at all. This is exactly what manual seeding
    did by hand earlier in this project; here it's the reusable version.
    """
    grade, subject = result["grade"], result["subject"]
    lessons = result.get("lessons") or []

    # retry_supabase() below, not a plain ac.table(...).execute(): by the time
    # execution reaches here, generate_lessons_from_published_book() has
    # already spent the real money (the LLM calls) -- a stale pooled
    # connection dying on the save must not cost that run its result. See
    # supabase_clients.retry_supabase's own docstring for why this happens on
    # a job that runs for minutes between DB touches.
    existing_topics = retry_supabase(
        lambda: _grade_subject_topics(create_admin_client(), school_id, grade, subject))
    order_by_def = {t["definitionId"]: t["orderIndex"] for t in existing_topics}
    next_order = max(order_by_def.values(), default=-1) + 1
    used_definition_ids: set[str] = set()

    class_ids = [
        c["id"] for c in
        (retry_supabase(lambda: create_admin_client().table("classes").select("id")
            .eq("school_id", school_id).eq("grade", grade).execute()).data or [])
    ]

    saved = created_topics = matched_topics = 0
    for entry in lessons:
        title, lesson = entry["topic"], entry["lesson"]

        best_def_id, best_score = None, 0.0
        for t in existing_topics:
            if t["definitionId"] in used_definition_ids:
                continue
            score = title_score(title, t["topic"])
            if score > best_score:
                best_def_id, best_score = t["definitionId"], score

        if best_def_id is not None and best_score >= MIN_TITLE_OVERLAP:
            definition_id = best_def_id
            order_index = order_by_def[best_def_id]
            matched_topics += 1
        else:
            if not class_ids:
                raise ValueError(f"School {school_id} has no Grade {grade} classes to attach a new topic to")
            definition_id = str(uuid.uuid4())
            order_index = next_order
            next_order += 1
            now = _now_iso()
            retry_supabase(lambda: create_admin_client().table("syllabus_topics").insert([{
                "id": str(uuid.uuid4()), "class_id": class_id, "teacher_id": teacher_id,
                "grade": grade, "subject": subject, "definition_id": definition_id,
                "topic": title, "description": "", "order_index": order_index,
                "is_completed": False, "page_start": result.get("pageStart"),
                "page_end": result.get("pageEnd"), "created_at": now,
            } for class_id in class_ids]).execute())
            created_topics += 1

        used_definition_ids.add(definition_id)
        # book_id/chapter_number/chapter_title: migration 024. Lets the
        # browse views group topics by the real chapter they came from
        # instead of one flat list mixing every chapter (and every old-
        # engine topic, which has none of these three) together.
        retry_supabase(lambda: create_admin_client().table("shared_prep_materials").upsert({
            "school_id": school_id, "grade": grade, "subject": subject,
            "topic_definition_id": definition_id, "topic": title, "subtopic": None,
            "order_index": order_index, "lesson": lesson, "batch_id": None,
            "book_id": book_id, "chapter_number": chapter_number,
            "chapter_title": result.get("chapterTitle"),
        }, on_conflict="school_id,grade,subject,topic_definition_id").execute())
        saved += 1

    return {
        "saved": saved, "createdTopics": created_topics, "matchedTopics": matched_topics,
        "grade": grade, "subject": subject, "chapterTitle": result.get("chapterTitle"),
        # `shipped` is what validation positively vouched for; `needsReview` is
        # saved too but flagged (see prep_pipeline_bridge's docstring for why a
        # flagged period beats a missing one), and `refused` never reaches here.
        "shipped": result.get("shipped"), "needsReview": result.get("needsReview"),
        "refused": result.get("refused"), "total": result.get("total"),
        "figureCount": result.get("figureCount"),
    }


# ─── On-demand admin trigger (Admin > Prep Materials > Generate from Textbook) ──
#
# save_published_chapter_lessons() above is a real LLM pipeline run -- a few
# minutes on a paid model, sometimes much longer on OPENROUTER_MODEL's current
# free tier (shared-capacity 502s and slow responses under load) -- too long
# either way for a request/response cycle a browser or gateway will hold
# open. This in-memory job registry is the same pattern syllabus_pdf_jobs.py
# already uses for the same reason: single-admin, one-off, acceptable to lose
# on a server restart (the admin just retries) -- unlike prep_batches above,
# which must survive across worker processes because ensure_stock() can be
# triggered by any teacher's request at any time. Deliberately its own dict
# rather than reusing _PROGRESS/prep_batches: those are keyed and deduped by
# (school, grade, subject) for the background top-up system, and a manual
# one-chapter trigger has no business colliding with that dedup logic.
_CHAPTER_JOBS: dict[str, dict] = {}
_CHAPTER_JOB_TTL_SECONDS = 3600  # prune finished jobs after an hour, same as syllabus_pdf_jobs.py


def _prune_chapter_jobs_locked():
    """Caller holds _LOCK. Same shape as syllabus_pdf_jobs._prune_locked."""
    cutoff = time.time() - _CHAPTER_JOB_TTL_SECONDS
    stale = [
        jid for jid, j in _CHAPTER_JOBS.items()
        if j["status"] in ("done", "error") and j.get("_finishedAtEpoch", 0) < cutoff
    ]
    for jid in stale:
        _CHAPTER_JOBS.pop(jid, None)


def get_chapter_job(job_id: str) -> Optional[dict]:
    with _LOCK:
        job = _CHAPTER_JOBS.get(job_id)
        if not job:
            return None
        return {k: v for k, v in job.items() if k != "_finishedAtEpoch"}


def start_chapter_generation(school_id: str, book_id: str, chapter_number: int,
                              teacher_id: Optional[str] = None) -> str:
    """Kicks off save_published_chapter_lessons() in a background thread and
    returns a job id immediately; the admin panel polls get_chapter_job()."""
    job_id = uuid.uuid4().hex
    with _LOCK:
        _prune_chapter_jobs_locked()
        _CHAPTER_JOBS[job_id] = {
            "status": "running", "bookId": book_id, "chapterNumber": chapter_number,
            "startedAt": _now_iso(), "result": None, "error": None,
        }

    thread = threading.Thread(
        target=_run_chapter_job, args=(job_id, school_id, book_id, chapter_number, teacher_id),
        daemon=True, name=f"chapter-gen-{job_id[:8]}",
    )
    thread.start()
    return job_id


def _run_chapter_job(job_id: str, school_id: str, book_id: str, chapter_number: int,
                      teacher_id: Optional[str]):
    try:
        result = asyncio.run(save_published_chapter_lessons(school_id, book_id, chapter_number, teacher_id))
        with _LOCK:
            _CHAPTER_JOBS[job_id].update(
                status="done", result=result, finishedAt=_now_iso(), _finishedAtEpoch=time.time())
    except Exception as e:
        print(f"[prep-batch] chapter job {job_id} ({book_id}#{chapter_number}) failed: {e}")
        with _LOCK:
            _CHAPTER_JOBS[job_id].update(
                status="error", error=str(e), finishedAt=_now_iso(), _finishedAtEpoch=time.time())


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

        _set_progress(batch_id, message="Checking for a matching textbook…")
        pipeline_lessons = _pipeline_lessons_for_todo(ac, school_id, grade, subject, todo)
        if pipeline_lessons:
            print(f"[prep-batch] agentic pipeline covered {len(pipeline_lessons)}/{len(todo)} topic(s)")

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
            pipeline_lesson = pipeline_lessons.get(t["definitionId"])
            if pipeline_lesson is not None:
                _set_progress(batch_id, message=f"Generating {t['topic']}… ({i + 1}/{len(todo)}, textbook-grounded)")
                lesson = pipeline_lesson
            else:
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
            from .notifications import create_admin_notification
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
