"""Node 1 over HTTP.

    FRONTEND -> FASTAPI -> LANGGRAPH ORCHESTRATOR -> {tools, agent graph, postgres}

WHAT A RUN NOW RETURNS. Node 1 ends at the academic contract, not at a chapter of
prep sheets: generation runs downstream of Node 2 and validation is Node 3 (see
prep_flow/graph.py). So `GET /runs/{id}` reports contract readiness, and
`GET /runs/{id}/contract` is the artefact — the document Node 2 consumes.

`/runs/{id}/materials`, `/runs/{id}/markdown` and `/runs/{id}/review` ARE STILL
HERE and still work, and that is deliberate rather than an oversight. They read
stored rows, and every run made before the node split has real sheets, findings
and verdicts in those rows. Removing the endpoints would make that history
unreadable to keep the API tidy. On a Node 1 run they return an empty set, which
each of them says explicitly rather than implying the chapter came out blank.

Two things about the shape of this API follow from the pipeline rather than from
preference.

**A chapter run is asynchronous, always.** Thirty to forty topics is a dozen or
more sequential LLM calls — minutes, not seconds — so POST returns 202 with a
run id and the client polls. Offering a synchronous variant would mostly produce
timeouts at whatever proxy sits in front of this.

**A run's status lives in Postgres, not in memory.** The graph is checkpointed
there anyway, and a status kept in a process dict disappears on the deploy that
happens halfway through a chapter.

Registered parallel to /api/prep-material, not in place of it: the single-topic
pilot is what the prompt iteration happens against, and this must not be able to
break it.
"""
import os
import time
import uuid
from datetime import date
from typing import Literal, Optional

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

# The real backend's auth dependencies. A flat prep-material checkout has no
# `deps` module, and this file must still be importable there (the CLI and the
# offline tests import the graph through it) — so the fallback FAILS CLOSED
# rather than waving requests through. An unauthenticated write path is not an
# acceptable price for import convenience.
try:
    from ...deps import require_admin, require_user
except ImportError:  # pragma: no cover — flat checkout
    def _no_auth_module():
        raise HTTPException(
            status_code=503,
            detail="auth is unavailable in this deployment (app.deps not importable), "
                   "so prep-flow routes are disabled. Use prep_flow/cli.py.")

    def require_user() -> dict:
        _no_auth_module()

    def require_admin() -> dict:
        _no_auth_module()

try:
    from ...lib.rate_limit import check_vision_rate_limit
    from ...lib.logger import get_client_ip
except ImportError:  # pragma: no cover
    def check_vision_rate_limit(_ip):
        return True, None

    def get_client_ip(_request):
        return "unknown"

# PLAIN RELATIVE IMPORTS, because this file now lives INSIDE the package it
# serves. It used to sit at the repo root (and at app/routes/ in the real
# backend), which is why it carried a two-layout try/except reaching for
# `..lib.prep_flow` and then for `prep_flow` — the import site had to know where
# the package was. From in here it does not: `.` is the package, in both trees.
from . import db, gate, render, review as review_packet, tools
from .graph import run_chapter, run_feedback_cycle
from .health import feedback_health, feedback_health_many
from .state import DEFAULT_CONFIG

router = APIRouter()


# ── Schemas ──────────────────────────────────────────────────────────────────

class TeacherSettings(BaseModel):
    duration: int = Field(30, ge=10, le=120, description="minutes per period")
    classSize: int = Field(40, ge=1, le=200)
    resourceLevel: int = Field(0, ge=0, le=2)
    language: str = "English"
    learningObjective: str = "new lesson"
    teachingStyle: str = "interactive"
    teacherId: Optional[str] = None
    # Wording only — same numbers, same thinking demand (bands.py already gates
    # that by grade, separately). This just pushes the sentence-and-vocabulary
    # level in engagement_level_guidance simpler than the grade default.
    plainLanguage: bool = False


class ChapterRunRequest(BaseModel):
    grade: str
    subject: str
    chapterNumber: Optional[int] = None
    chapterTitle: Optional[str] = None
    schoolId: Optional[str] = None
    classId: Optional[str] = None
    # Escape hatch for a school whose textbook is not ingested yet: paste the
    # chapter. Everything downstream treats it identically, minus page markers.
    chapterMarkdown: Optional[str] = None
    localFolder: Optional[str] = None
    settings: TeacherSettings = Field(default_factory=TeacherSettings)
    teacherPreferences: Optional[str] = None

    targetTopics: int = Field(30, ge=1, le=40)
    minTopics: int = Field(8, ge=1, le=40)
    maxTopics: int = Field(40, ge=1, le=40)
    movesWindow: int = Field(4, ge=1, le=8,
                             description="topics per move-extraction call")

    # `windowSize`, `includeVisuals` and `maxRepairRounds` were here. All three
    # tuned generation or the repair loop, which are no longer part of Node 1.
    # Pydantic ignores unknown fields by default, so a client still sending them
    # gets a normal 202 rather than a 422 — an old caller should not break on a
    # knob that has moved to a different node.


class FeedbackItem(BaseModel):
    """One YES / SOMEWHAT / NO. Per section, because a single verdict on a whole
    sheet cannot tell the Challenge from the Concept — and section level is the
    granularity the optimizer needs to say anything useful."""
    section: Optional[Literal[
        "refresher", "concept", "realLife", "challenge", "levelSet", "explore"]] = None
    rating: Literal["yes", "somewhat", "no"]
    note: Optional[str] = Field(None, max_length=2000)


class FeedbackRequest(BaseModel):
    materialId: Optional[str] = None
    runId: Optional[str] = None
    # Which period of the chapter this form is about. Optional, and the loop's
    # value depends on it: adaptations are stored per (run, topic, section), so a
    # rating without a topic can say "the Challenge sections are weak this term"
    # and can never say WHICH adaptation that is evidence about. Nullable rather
    # than required because every row written before this existed has no topic,
    # and a required field would reject the forms already in flight.
    topicIndex: Optional[int] = Field(None, ge=0)
    teacherId: Optional[str] = None
    schoolId: Optional[str] = None
    grade: str
    subject: str
    taughtOn: Optional[date] = None
    responses: list[FeedbackItem] = Field(..., min_length=1, max_length=12)


class OptimizeRequest(BaseModel):
    grade: str
    subject: str
    schoolId: Optional[str] = None
    windowDays: int = Field(40, ge=7, le=180)


# ── Chapter generation ───────────────────────────────────────────────────────

def _config_from(body: ChapterRunRequest) -> dict:
    maximum = max(body.minTopics, body.maxTopics)
    return {
        **DEFAULT_CONFIG,
        "min_topics": body.minTopics,
        "max_topics": maximum,
        "target_topics": min(max(body.targetTopics, body.minTopics), maximum),
        "moves_window": body.movesWindow,
    }


async def _resolve_chapter(body: ChapterRunRequest) -> dict:
    if body.chapterMarkdown and body.chapterMarkdown.strip():
        return {
            "chapterNumber": body.chapterNumber,
            "chapterTitle": body.chapterTitle or "Pasted chapter",
            "pageStart": None, "pageEnd": None,
            "markdown": body.chapterMarkdown,
            "source": "pasted",
        }
    folder = body.localFolder or os.environ.get("PREP_FLOW_TEXTBOOK_FOLDER")
    try:
        return await tools.load_chapter(
            school_id=body.schoolId, grade=body.grade, subject=body.subject,
            chapter_number=body.chapterNumber, chapter_title=body.chapterTitle,
            local_folder=folder,
        )
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


async def _run_in_background(run_id: str, thread_id: str, chapter: dict,
                             body: ChapterRunRequest) -> None:
    try:
        await run_chapter(
            grade=body.grade, subject=body.subject,
            chapter_title=chapter["chapterTitle"],
            chapter_number=chapter.get("chapterNumber"),
            chapter_markdown=chapter["markdown"],
            page_start=chapter.get("pageStart"), page_end=chapter.get("pageEnd"),
            chapter_figures=chapter.get("figures") or [],
            school_id=body.schoolId, class_id=body.classId,
            teacher_settings=body.settings.model_dump(),
            teacher_preferences=body.teacherPreferences,
            config=_config_from(body), run_id=run_id, thread_id=thread_id,
            # This coroutine is literally what background.add_task() below
            # schedules — by the time it runs, the 202 has already gone back to
            # the caller. See provenance.py for why this fact is recorded.
            background_task=True,
        )
    except Exception as exc:
        # persist_node handles every failure the graph can express; reaching here
        # means the graph itself could not run (a checkpointer that would not
        # open, a recursion limit). Recording it on the run row is the only way
        # the poller ever learns, since there is no response to attach it to.
        print(f"[prep_flow] run {run_id} crashed outside the graph: {exc}")
        await db.finish_run(run_id, "failed", error=f"{type(exc).__name__}: {exc}")


@router.post("/prep-flow/chapters", status_code=202)
async def start_chapter(
    body: ChapterRunRequest, request: Request, background: BackgroundTasks,
    user: dict = Depends(require_user),
):
    """Kick off a chapter batch. Returns 202 and a run id to poll.

    The chapter is loaded and checked synchronously, before the 202, so that
    "there is no published chapter 7" is a 400 the caller can act on rather than
    a background run that fails a minute later.
    """
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    chapter = await _resolve_chapter(body)
    if not (chapter.get("markdown") or "").strip():
        raise HTTPException(
            status_code=400,
            detail="that chapter has no ingested text to generate from — see "
                   "GET /api/prep-flow/chapters for which chapters are usable")

    run_id, thread_id = str(uuid.uuid4()), f"chapter:{uuid.uuid4()}"

    # Both checks are about the same thing — a background run must be reachable
    # afterwards — but they fail differently and so are reported differently.
    if not db.configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured (NEXT_PUBLIC_SUPABASE_URL / "
                   "SUPABASE_SERVICE_ROLE_KEY), so a background run could not be "
                   "stored or polled. Use prep_flow/cli.py to run in the "
                   "foreground instead.")
    schema = await db.verify_schema()
    if not schema["ok"]:
        raise HTTPException(
            status_code=503,
            detail=f"missing table(s) {', '.join(schema['missing'])} — apply "
                   f"{db.MIGRATION} in the Supabase SQL editor before generating a "
                   f"chapter, or the material would have nowhere to land.")

    background.add_task(_run_in_background, run_id, thread_id, chapter, body)

    return {
        "runId": run_id,
        "threadId": thread_id,
        "status": "running",
        # Surfaced rather than logged: a client that knows this run is not
        # resumable knows that an interruption means starting over, and can say
        # so instead of offering a Resume button that restarts the chapter.
        "resumable": db.checkpoints_durable(),
        "chapter": {
            "number": chapter.get("chapterNumber"),
            "title": chapter["chapterTitle"],
            "pages": [chapter.get("pageStart"), chapter.get("pageEnd")],
            "source": chapter.get("source"),
            "chars": len(chapter["markdown"]),
        },
        "config": _config_from(body),
        "poll": f"/api/prep-flow/runs/{run_id}",
    }


@router.get("/prep-flow/chapters")
async def list_chapters(
    grade: Optional[str] = None, subject: Optional[str] = None,
    schoolId: Optional[str] = None, localFolder: Optional[str] = None,
    user: dict = Depends(require_user),
):
    """Which chapters can actually be generated from.

    `usable` is the field that matters. A partial ingest lists every chapter in
    the book but only has text for some, and starting a run on one of the others
    produces forty sheets grounded in nothing.
    """
    folder = localFolder or os.environ.get("PREP_FLOW_TEXTBOOK_FOLDER")
    if folder:
        try:
            return {"source": "local", "chapters": tools.list_local_chapters(folder)}
        except (FileNotFoundError, ValueError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    if not (schoolId and grade and subject):
        raise HTTPException(
            status_code=400,
            detail="pass localFolder, or schoolId + grade + subject to list "
                   "published chapters")

    chapter = await tools.load_db_chapter(schoolId, grade, subject)
    if not chapter:
        return {"source": "supabase", "chapters": []}
    return {"source": "supabase", "chapters": [{
        "chapterNumber": chapter["chapterNumber"],
        "chapterTitle": chapter["chapterTitle"],
        "pageStart": chapter.get("pageStart"), "pageEnd": chapter.get("pageEnd"),
        "usable": bool((chapter.get("markdown") or "").strip()),
    }]}


@router.get("/prep-flow/runs/{run_id}")
async def get_run(run_id: str, user: dict = Depends(require_user)):
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")

    # `readiness` alone, not the whole contract: this is the POLLING endpoint and
    # a client hits it every few seconds. A 40-topic contract is hundreds of
    # kilobytes and the poller's question is only "is it done and is it usable".
    # The document itself is one fetch away at /runs/{id}/contract.
    readiness = ((run.get("contract") or {}).get("readiness") or {})

    # Still counted, and still from the stored rows, for a run made before the
    # node split — those have real sheets and real findings and a client showing
    # a historical run should still see them. Both are 0 on a Node 1 run.
    materials = await db.fetch_run_materials(run_id)
    blocking = sum(
        1 for m in materials
        for f in ((m.get("validation") or {}).get("findings") or [])
        if f.get("severity") == "blocking"
    )
    return {
        "runId": run["id"],
        "threadId": run["thread_id"],
        "status": run["status"],
        "chapter": {"number": run["chapter_number"], "title": run["chapter_title"]},
        "grade": run["grade"], "subject": run["subject"],
        "arc": run.get("chapter_arc"),
        "topicCount": run["topic_count"],
        # What Node 1 actually produced. `generationReady` is the field a
        # scheduler reads to decide whether this run can be handed to Node 2.
        "contract": {
            "present": bool(run.get("contract")),
            "topicsReady": readiness.get("topicsReady"),
            "topicsTotal": readiness.get("topicsTotal"),
            "generationReady": readiness.get("generationReady"),
            "notReady": readiness.get("notReady") or [],
            "fetch": f"/api/prep-flow/runs/{run_id}/contract",
        },
        "generated": len(materials),
        "blockingFindings": blocking,
        "adaptiveStateVersion": run.get("adaptive_state_version"),
        "metrics": run.get("metrics") or {},
        # Stage-by-stage: what ran, what data it used or seeded, and whether this
        # run executed in a background task or the foreground. See provenance.py.
        "provenance": run.get("provenance") or {},
        "error": run.get("error"),
        "createdAt": run["created_at"],
        "completedAt": run.get("completed_at"),
    }


@router.get("/prep-flow/runs/{run_id}/contract")
async def get_run_contract(
    run_id: str,
    fromIndex: int = Query(1, ge=1), limit: int = Query(60, ge=1, le=60),
    user: dict = Depends(require_user),
):
    """Node 1's output for this run — the document Node 2 consumes.

    Windowed by topic like `/materials` is, and for the same reason: a 40-topic
    contract carrying every topic's book move order, trajectory and section spec
    is large enough that a client rendering one topic at a time should not have
    to pull all of it. The chapter-wide half (prerequisites, misconceptions,
    anchors, delivery assumptions, `preserve`, `integrity`) is returned WHOLE on
    every page — it is small, and a window of topics without the invariants they
    were derived under is not a contract, it is a list.

    404 rather than an empty document when the run predates Node 1: a client
    that gets `{}` cannot tell "this run has no contract" from "this run has an
    empty one", and the two mean different things — the first is a run to read
    materials from, the second is a run that failed.
    """
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    document = run.get("contract") or {}
    if not document:
        raise HTTPException(
            status_code=404,
            detail=("this run produced no contract. Runs made before the master "
                    "orchestration was split into nodes ended in generated "
                    "material instead — read those at "
                    f"/api/prep-flow/runs/{run_id}/materials. A run that failed "
                    "before persist has neither."))

    rows = document.get("topics") or []
    window = [r for r in rows if int(r.get("index") or 0) >= fromIndex][:limit]
    return {
        "runId": run_id,
        "status": run["status"],
        **{k: v for k, v in document.items() if k != "topics"},
        "total": len(rows),
        "fromIndex": fromIndex,
        "topics": window,
    }


@router.get("/prep-flow/runs/{run_id}/materials")
async def get_run_materials(
    run_id: str,
    fromIndex: int = Query(1, ge=1), limit: int = Query(50, ge=1, le=60),
    user: dict = Depends(require_user),
):
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    rows = await db.fetch_run_materials(run_id)
    window = [r for r in rows if r["idx"] >= fromIndex][:limit]
    return {
        "runId": run_id,
        # Empty on a Node 1 run, and it says so rather than leaving a caller to
        # infer it from `total: 0` — which reads identically to a chapter whose
        # every sheet failed. Kept for the runs that DO have material: everything
        # generated before the node split is still stored and still readable.
        "note": (None if rows else
                 "this run produced no material: Node 1 ends at the academic "
                 f"contract — see /api/prep-flow/runs/{run_id}/contract"),
        "status": run["status"],
        "total": len(rows),
        "materials": [{
            "index": r["idx"],
            "materialId": str(r["material_id"]),
            "topic": r["topic"],
            "subtopic": r.get("subtopic"),
            "pages": [r.get("page_start"), r.get("page_end")],
            "revision": r["revision"],
            "material": r["material"],
            "markdown": r["markdown"],
            "findings": (r.get("validation") or {}).get("findings") or [],
        } for r in window],
    }


@router.get("/prep-flow/runs/{run_id}/markdown")
async def get_run_markdown(run_id: str, user: dict = Depends(require_user)):
    """The whole chapter as one Markdown document — contents table, every sheet
    in teaching order, review findings printed beside the material they concern."""
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    rows = await db.fetch_run_materials(run_id)
    state = {
        "chapter_title": run["chapter_title"], "chapter_number": run["chapter_number"],
        "grade": run["grade"], "subject": run["subject"],
        "chapter_arc": run.get("chapter_arc"), "status": run["status"],
        "metrics": run.get("metrics") or {},
        "run_provenance": run.get("provenance") or {},
        "topics": [{"index": r["idx"], "topic": r["topic"], "subtopic": r.get("subtopic"),
                    "page_start": r.get("page_start"), "page_end": r.get("page_end")}
                   for r in rows],
        "materials": {r["idx"]: r["material"] for r in rows},
        "issues": {r["idx"]: (r.get("validation") or {}).get("findings") or [] for r in rows},
    }
    return {"runId": run_id, "markdown": render.render_chapter(state)}


@router.post("/prep-flow/runs/{run_id}/resume", status_code=202)
async def resume_run(
    run_id: str, background: BackgroundTasks, user: dict = Depends(require_admin),
):
    """Continue a run from its last checkpoint.

    The point of the Postgres checkpointer: a 40-topic run interrupted at topic
    31 by a deploy or a timeout picks up at 31. Re-invoking the same thread_id
    replays the checkpoint rather than the work.
    """
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    if run["status"] in ("validated",):
        raise HTTPException(status_code=409, detail="this run already completed cleanly")
    if not db.checkpoints_durable():
        # Without a checkpointer this endpoint would silently regenerate the whole
        # chapter — the same cost as a new run, minus the caller's consent.
        raise HTTPException(
            status_code=409,
            detail="this deployment has no Supabase Postgres connection string "
                   "(SUPABASE_DB_URL), so no checkpoint exists to resume from. "
                   "Resuming would regenerate the chapter from topic 1 — start a "
                   "new run instead if that is what you want.")

    config = run.get("config") or {}
    body = ChapterRunRequest(
        grade=run["grade"], subject=run["subject"],
        chapterNumber=run["chapter_number"], chapterTitle=run["chapter_title"],
        schoolId=str(run["school_id"]) if run.get("school_id") else None,
        classId=str(run["class_id"]) if run.get("class_id") else None,
        targetTopics=int(config.get("target_topics", 30)),
        minTopics=int(config.get("min_topics", 8)),
        maxTopics=int(config.get("max_topics", 40)),
        movesWindow=int(config.get("moves_window", 4)),
    )
    chapter = await _resolve_chapter(body)
    await db.finish_run(run_id, "running")
    background.add_task(_run_in_background, run_id, run["thread_id"], chapter, body)
    return {"runId": run_id, "status": "running", "resumedFrom": run["thread_id"]}


# ── Feedback loop ────────────────────────────────────────────────────────────

@router.post("/prep-flow/feedback", status_code=201)
async def submit_feedback(body: FeedbackRequest, user: dict = Depends(require_user)):
    """The teacher's form, after teaching from a sheet.

    Scoped to the cohort (school + grade + subject), not the class: a prep
    material is generated once per grade+subject and taught by every teacher of
    it, so that is the unit whose feedback is comparable.
    """
    if not db.configured():
        raise HTTPException(
            status_code=503,
            detail="Supabase is not configured — feedback has nowhere to be stored")
    key = db.scope_key(body.schoolId, body.grade, body.subject)
    rows = [{
        "id": str(uuid.uuid4()),
        "material_id": body.materialId,
        "run_id": body.runId,
        "teacher_id": body.teacherId,
        "scope_key": key,
        "section": item.section,
        "rating": item.rating,
        "note": item.note,
        "taught_on": body.taughtOn,
        "topic_index": body.topicIndex,
    } for item in body.responses]

    written = await db.record_feedback(rows)
    if written < len(rows):
        raise HTTPException(
            status_code=500,
            detail=f"stored {written} of {len(rows)} responses — check the server log")
    return {"stored": written, "scopeKey": key}


@router.post("/prep-flow/feedback/optimize")
async def optimize(body: OptimizeRequest, admin: dict = Depends(require_admin)):
    """Run one turn of the feedback loop: telemetry -> patterns -> adaptive state.

    Idempotent in the way that matters — thin data and an unchanged picture both
    resolve to "no new version" rather than to a version that says the same thing
    as the last one. Safe to put on a schedule.
    """
    started = time.time()
    result = await run_feedback_cycle(
        grade=body.grade, subject=body.subject, school_id=body.schoolId,
        window_days=body.windowDays,
    )
    return {
        "scopeKey": db.scope_key(body.schoolId, body.grade, body.subject),
        "windowDays": body.windowDays,
        "sampleSize": result.get("sample_size", 0),
        "applied": result.get("applied", False),
        "version": (result.get("adaptive_state") or {}).get("_version"),
        "patterns": result.get("patterns") or [],
        "aggregates": result.get("aggregates") or {},
        "adaptiveState": result.get("adaptive_state") or {},
        "changelog": result.get("changelog") or [],
        # Both are why an "applied: false" happened. Without them the caller sees
        # a refusal with no reason, which is how the silent-drop bug survived.
        "liveness": result.get("liveness") or {},
        "gate": result.get("gate"),
        "elapsedMs": int((time.time() - started) * 1000),
    }


@router.get("/prep-flow/adaptive-state")
async def get_adaptive_state(
    grade: str, subject: str, schoolId: Optional[str] = None,
    history: bool = False, user: dict = Depends(require_user),
):
    """What the next chapter generated for this cohort will be told."""
    key = db.scope_key(schoolId, grade, subject)
    state = await db.fetch_active_adaptive_state(key)
    payload = {"scopeKey": key, "version": state.get("_version"),
               "sampleSize": state.get("_sampleSize"), "state": state}
    if history:
        payload["history"] = await db.list_adaptive_states(key)
    return payload


class RollbackRequest(BaseModel):
    grade: str
    subject: str
    schoolId: Optional[str] = None
    version: int = Field(..., ge=1)


@router.post("/prep-flow/adaptive-state/rollback")
async def rollback_adaptive_state(
    body: RollbackRequest, admin: dict = Depends(require_admin),
):
    """Make an earlier adaptive state active again.

    The answer to an optimization cycle that made the material worse. Deliberately
    a human action: a bad state is something someone notices in the sheets, and
    the reversal should be as blunt as the observation. Nothing is deleted — the
    superseded version stays in the history.
    """
    key = db.scope_key(body.schoolId, body.grade, body.subject)
    restored = await db.rollback_adaptive_state(key, body.version)
    if not restored:
        raise HTTPException(
            status_code=404,
            detail=f"no version {body.version} for {key} — GET "
                   f"/api/prep-flow/adaptive-state?history=true to see what exists")
    return {"scopeKey": key, "activeVersion": restored["version"],
            "state": restored.get("state") or {}}


@router.get("/prep-flow/runs")
async def list_runs(
    grade: Optional[str] = None, subject: Optional[str] = None,
    schoolId: Optional[str] = None, limit: int = Query(20, ge=1, le=100),
    user: dict = Depends(require_user),
):
    return {"runs": await db.list_runs(
        grade=grade, subject=subject, school_id=schoolId, limit=limit)}


@router.get("/prep-flow/feedback/health")
async def feedback_loop_health(
    grade: str, subject: str, schoolId: Optional[str] = None,
    windowDays: int = Query(40, ge=7, le=180), user: dict = Depends(require_user),
):
    """Is the feedback loop alive, and is anyone still feeding it?

    Distinct from `/prep-flow/health`, which reports whether the app can run at
    all. This reports the failures that produce no error and no log line: a
    cohort whose teachers stopped filling the form, or a schedule that stopped
    running cycles. Both leave the loop looking perfectly healthy — the last
    state stays active, correct and reaching the prompt — while it quietly stops
    learning anything.
    """
    if not db.configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured")
    return await feedback_health(schoolId, grade, subject, windowDays)


class CohortRef(BaseModel):
    grade: str
    subject: str
    schoolId: Optional[str] = None


class CohortHealthRequest(BaseModel):
    cohorts: list[CohortRef] = Field(..., min_length=1, max_length=60)
    windowDays: int = Field(40, ge=7, le=180)


@router.post("/prep-flow/feedback/health")
async def feedback_loop_health_many(
    body: CohortHealthRequest, user: dict = Depends(require_user),
):
    """Loop health across many cohorts, worst first — the dashboard query."""
    if not db.configured():
        raise HTTPException(status_code=503, detail="Supabase is not configured")
    return await feedback_health_many(
        [c.model_dump() for c in body.cohorts], body.windowDays)


@router.get("/prep-flow/health")
async def health(user: dict = Depends(require_user)):
    """Is this deployment able to run a chapter, and what degrades if not.

    Worth its own endpoint because the two failure modes look identical from the
    outside and have different fixes: a missing migration means material has
    nowhere to land, while a missing Postgres URL only means runs cannot be
    resumed.
    """
    schema = await db.verify_schema()
    golden = gate.golden_path()
    reasoning_cached = await db.reasoning_available()
    learner_evidence = await db.learner_diagnoses_available()
    review_ready = await db.review_available()
    return {
        "supabaseConfigured": db.configured(),
        "schema": schema,
        "resumable": db.checkpoints_durable(),
        # A capability, not a requirement — reported the same way resumability is,
        # and for the same reason: without it every run still generates, it just
        # pays again for something it already worked out.
        "reasoningCached": reasoning_cached,
        # Also a capability. Without it every learner failure is still recorded,
        # in the material row it belongs to; what is missing is the ability to
        # count the same failure across runs.
        "learnerEvidenceQueryable": learner_evidence,
        # Generation does not need these; a teacher opening a needs_review
        # chapter does.
        "teacherReviewAvailable": review_ready,
        "activationGate": {
            "enabled": gate.enabled(),
            "goldenFixture": str(golden),
            "goldenPresent": golden.exists(),
            "threshold": gate.REGRESSION_THRESHOLD,
        },
        "notes": [
            note for note in (
                None if schema.get("ok") else
                f"apply {db.MIGRATION} — generated material has nowhere to be stored",
                None if db.checkpoints_durable() else
                "no SUPABASE_DB_URL — runs complete and persist, but an interrupted "
                "run restarts from topic 1 instead of resuming",
                None if reasoning_cached else
                f"apply {db.REASONING_MIGRATION} — optional; without it every run "
                "re-derives the same chapter's curriculum reasoning",
                None if review_ready else
                f"apply {db.REVIEW_MIGRATION} — without it a chapter can reach "
                f"needs_review with no way for a teacher to see the attempts or "
                f"record a decision",
                None if learner_evidence else
                f"apply {db.LEARNER_DIAGNOSES_MIGRATION} — optional; without it "
                f"learner failures are kept per run but cannot be counted across "
                f"runs",
                None if not gate.enabled() or golden.exists() else
                f"PREP_FLOW_ACTIVATION_GATE is on but there is no golden fixture at "
                f"{golden} — adaptive states will activate ungated",
            ) if note
        ],
    }


# ── Teacher review ────────────────────────────────────────────────────────────

class ReviewRequest(BaseModel):
    """One decision. `index` narrows it to a single sheet.

    A teacher can accept a chapter while rejecting two of its forty sheets, so
    the decision is not forced to be whole-chapter — omitting `index` means the
    chapter, giving one means that topic.
    """
    action: Literal["approve", "reject", "revise", "comment"]
    index: Optional[int] = Field(default=None, ge=1, le=60)
    note: Optional[str] = Field(default=None, max_length=4000)


@router.get("/prep-flow/runs/{run_id}/review")
async def get_review(run_id: str, user: dict = Depends(require_user)):
    """Everything needed to decide what to do with a chapter the loop gave up on.

    Available for any run, not only `needs_review` ones: the same packet is the
    honest answer to "what did this chapter actually do", and gating it on status
    would mean the one view that explains a run is unavailable for every run that
    went well.
    """
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")

    available = await db.review_available()
    materials = await db.fetch_run_materials(run_id)
    attempts = await db.fetch_material_attempts(run_id) if available else []
    reviews = await db.fetch_reviews(run_id) if available else []

    packet = review_packet.build(run, materials, attempts, reviews,
                                 attempts_available=available)
    if not available:
        # Served rather than refused: the findings, verdicts and diagnoses all
        # live on the material rows and are the substance of a review. What is
        # missing is the earlier attempts and the ability to record a decision,
        # and saying which migration supplies them is more use than a 503.
        packet["degraded"] = (
            f"the review tables are not installed, so earlier attempts are not "
            f"shown and decisions cannot be recorded — apply {db.REVIEW_MIGRATION}")
    return packet


@router.post("/prep-flow/runs/{run_id}/review", status_code=201)
async def submit_review(run_id: str, body: ReviewRequest,
                        user: dict = Depends(require_user)):
    """Record a decision, and move the run's status when the decision implies one.

    A 503 rather than a 201 when the write fails. Everywhere else in this
    pipeline a lost bookkeeping row is the right trade against losing generated
    material — here it is the opposite: a teacher who believes they approved a
    chapter and did not is worse off than one who is told to try again.
    """
    run = await db.fetch_run(run_id)
    if not run:
        raise HTTPException(status_code=404, detail="no such run")
    if not await db.review_available():
        raise HTTPException(
            status_code=503,
            detail=f"reviews cannot be recorded — apply {db.REVIEW_MIGRATION}")

    review_id = await db.save_review(
        str(uuid.uuid4()), run_id, action=body.action, idx=body.index,
        teacher_id=user.get("id"), note=body.note)
    if not review_id:
        raise HTTPException(status_code=503,
                            detail="the review could not be stored")

    # Only a whole-chapter decision moves the run. Approving one sheet of forty
    # says nothing about the other thirty-nine, and letting it mark the chapter
    # publishable would be the review equivalent of last-write-wins.
    status = (review_packet.status_after(body.action, run.get("status"))
              if body.index is None else None)
    if status:
        await db.set_run_status(run_id, status)

    return {"reviewId": review_id, "runId": run_id, "action": body.action,
            "index": body.index, "status": status or run.get("status"),
            "statusChanged": bool(status)}
