"""Supabase persistence for the chapter pipeline, plus the graph's checkpointer.

Everything this package stores goes through `supabase_clients.create_admin_client()`
— the same service-role client `pedagogy_library`, `canonical_mapping` and
`prep_context` use. No second connection mechanism, no second pool, no second set
of credentials to keep in sync.

There is exactly one thing that cannot go through it: **LangGraph's
checkpointer**. `AsyncPostgresSaver` speaks the Postgres wire protocol, not
PostgREST, so it needs Supabase's direct connection string. That splits
persistence in two, along a line worth understanding:

  * **Domain rows** (runs, topics, materials, feedback, patterns, adaptive state)
    → Supabase client. Available whenever the app is, which is why a chapter run
    can be started, polled and read back with nothing else configured.
  * **Graph checkpoints** → Supabase's Postgres URL, when one is set. Only
    *resuming* an interrupted run needs it. Without it a run still completes and
    still persists; it just cannot be picked up from topic 31 after a crash.

DDL is NOT applied from here. PostgREST cannot run `CREATE TABLE`, and a service
that quietly migrates its own schema on first write is a worse idea than one that
tells you which migration to apply — so `verify_schema()` checks and reports, and
`migrations/030_prep_flow.sql` is applied through the Supabase SQL editor or your
migration runner, alongside migrations 020/022 that the canonical and pedagogy
libraries already depend on.
"""
import asyncio
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from .deps import create_admin_client, database_url

TABLES = (
    "prep_flow_runs",
    "prep_flow_topics",
    "prep_flow_materials",
    "prep_flow_feedback",
    "prep_flow_patterns",
    "prep_flow_adaptive_state",
)

MIGRATION = "migrations/030_prep_flow.sql"
# Adds prep_flow_runs.provenance. Optional in the same sense migration 031 is
# (see REASONING_MIGRATION below) — nothing breaks without it, finish_run()
# just cannot store the trace it already computed.
PROVENANCE_MIGRATION = "migrations/032_prep_flow_provenance.sql"
# Adds prep_flow_runs.contract and prep_flow_topics.contract — Node 1's output.
# Optional in the same sense as 032: a run without it still derives its contract
# and still returns it to the caller, it just cannot be stored, which costs the
# ability to replay Node 2 against it later. See migrations/035.
CONTRACT_MIGRATION = "migrations/035_prep_flow_contract.sql"
# Node 2's stored plan, and prep_flow_feedback.topic_index. Optional in the same
# sense as the two above: without it a run still generates, teachers still file
# feedback, and the wording loop still works — what stops is the reinforcement
# loop, which cannot attribute a rating to an adaptation it has no row for.
ADAPTATIONS_MIGRATION = "migrations/036_context_adaptations.sql"


def scope_key(school_id: Optional[str], grade: str, subject: str) -> str:
    """Feedback and adaptive state are scoped to a cohort, not a class: a prep
    material is generated once per grade+subject and taught by every teacher of
    it, so that is the unit whose feedback is comparable."""
    return f"{school_id or 'global'}:{grade}:{(subject or '').strip().lower()}"


def configured() -> bool:
    """Whether domain rows can be stored at all.

    True whenever the backend's own Supabase credentials are present — so in any
    real deployment. Deliberately NOT gated on the Postgres URL: that is only
    needed for resumability, and refusing to persist a chapter because runs
    cannot be *resumed* would be the wrong trade.
    """
    return bool(os.environ.get("NEXT_PUBLIC_SUPABASE_URL")
                and os.environ.get("SUPABASE_SERVICE_ROLE_KEY"))


def checkpoints_durable() -> bool:
    """Whether an interrupted run can be resumed rather than restarted."""
    return bool(database_url())


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _jsonable(value: Any) -> Any:
    """PostgREST sends JSON, so a date/datetime has to be a string by the time it
    gets there — a raw `date` in a payload is a serialisation error at request
    time, which surfaces as a confusing 500 rather than as a bad column."""
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value


async def _run(fn, *args, **kwargs):
    """Run a blocking Supabase call off the event loop.

    supabase-py is synchronous. Awaiting it directly would stall every other
    topic in the batch — the same reason tools.py threads its lookups.
    """
    return await asyncio.to_thread(fn, *args, **kwargs)


async def _write(label: str, fn) -> Optional[Any]:
    """Perform a Supabase call, logging rather than raising on failure.

    Losing a chapter of generated material because a bookkeeping insert failed
    would be the worst possible trade, so every call here is best-effort and the
    caller is never asked to handle it. A missing table is called out specially,
    because that failure has one specific fix.

    CONVENTION: `None` means failed. `fn` must therefore return something truthy
    on success — the `.execute()` result satisfies this naturally, and a callable
    doing several steps should return True explicitly rather than falling off the
    end, or its success is indistinguishable from an exception.
    """
    if not configured():
        return None
    try:
        return await _run(fn)
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "PGRST205" in message or "42P01" in message:
            print(f"[prep_flow:db] {label} failed — the prep_flow_* tables are "
                  f"missing. Apply {MIGRATION}. ({message[:160]})")
        else:
            print(f"[prep_flow:db] {label} failed: {message[:300]}")
        return None


async def verify_schema() -> dict:
    """Which prep_flow tables exist. Cheap: one bounded select per table.

    Called by the HTTP layer before starting a run, so "the migration was never
    applied" is a 503 naming the file rather than forty topics of generated
    material with nowhere to land.
    """
    if not configured():
        return {"configured": False, "missing": list(TABLES), "ok": False}

    def probe(table: str) -> bool:
        try:
            create_admin_client().table(table).select("id").limit(1).execute()
            return True
        except Exception:
            return False

    present = await _run(lambda: {t: probe(t) for t in TABLES})
    missing = sorted(t for t, ok in present.items() if not ok)
    return {"configured": True, "missing": missing, "ok": not missing,
            "migration": MIGRATION if missing else None}


# ── Checkpointer ──────────────────────────────────────────────────────────────

@asynccontextmanager
async def checkpointer():
    """The graph's checkpointer: Supabase's Postgres when reachable, memory else.

    Resumability is the whole point. A chapter run is a dozen-plus sequential LLM
    calls, and without a durable checkpoint a timeout at topic 31 means paying for
    topics 1–30 a second time.

    Falls back rather than failing: a missing connection string, or a psycopg that
    was never installed, degrades to in-memory checkpoints. The run still
    completes and still persists through the Supabase client — it just cannot be
    resumed, which is worth a warning and not worth refusing the work over.
    """
    url = database_url()
    if url:
        try:
            from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        except ImportError:
            print("[prep_flow] a Postgres URL is set but psycopg/"
                  "langgraph-checkpoint-postgres is not installed — runs will not be "
                  "resumable. pip install -r requirements-prep-flow.txt")
        else:
            try:
                async with AsyncPostgresSaver.from_conn_string(url) as saver:
                    await saver.setup()
                    yield saver
                return
            except Exception as exc:
                print(f"[prep_flow] could not open the Postgres checkpointer "
                      f"({str(exc)[:200]}) — falling back to in-memory. Runs will "
                      f"complete but will not be resumable.")

    from langgraph.checkpoint.memory import InMemorySaver

    if not url:
        print("[prep_flow] No SUPABASE_DB_URL/DATABASE_URL — using an in-memory "
              "checkpointer. Material still persists via Supabase; interrupted runs "
              "restart from the beginning rather than resuming.")
    yield InMemorySaver()


# ── Runs ──────────────────────────────────────────────────────────────────────

async def create_run(run: dict) -> None:
    row = {
        "id": run["id"],
        "thread_id": run["thread_id"],
        "school_id": run.get("school_id"),
        "class_id": run.get("class_id"),
        "grade": str(run["grade"]),
        "subject": run["subject"],
        "chapter_number": run.get("chapter_number"),
        "chapter_title": run["chapter_title"],
        "status": run.get("status", "running"),
        "config": _jsonable(run.get("config") or {}),
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    await _write(
        "create_run",
        lambda: create_admin_client().table("prep_flow_runs")
        .upsert(row, on_conflict="id").execute(),
    )


async def finish_run(run_id: str, status: str, *, arc: str = None, topic_count: int = None,
                     metrics: dict = None, error: str = None,
                     adaptive_version: int = None, provenance: dict = None) -> None:
    patch: dict = {"status": status, "updated_at": now_iso()}
    # COALESCE-by-omission: a resume that has not re-derived the arc must not
    # blank the one the first attempt recorded, and `error=None` on a successful
    # retry SHOULD clear a stale message — hence arc/metrics are conditional and
    # error is always written.
    if arc:
        patch["chapter_arc"] = arc
    if topic_count is not None:
        patch["topic_count"] = topic_count
    if metrics is not None:
        patch["metrics"] = _jsonable(metrics)
    if adaptive_version is not None:
        patch["adaptive_state_version"] = adaptive_version
    patch["error"] = error
    if status in ("validated", "needs_review", "failed"):
        patch["completed_at"] = now_iso()

    await _write(
        "finish_run",
        lambda: create_admin_client().table("prep_flow_runs")
        .update(patch).eq("id", run_id).execute(),
    )

    # A SEPARATE update, deliberately not folded into `patch` above. `provenance`
    # (migration 032) can lag behind a deployment that already has 030/031, and
    # PostgREST rejects an UPDATE naming ANY unknown column as one atomic failure
    # — folded in, a missing `provenance` column would silently take the run's
    # status/metrics/completed_at down with it, which is a real regression for an
    # optional field that exists purely for provenance display.
    if provenance is not None:
        try:
            await _run(
                lambda: create_admin_client().table("prep_flow_runs")
                .update({"provenance": _jsonable(provenance)}).eq("id", run_id).execute()
            )
        except Exception as exc:
            message = str(exc)
            if "does not exist" in message or "PGRST204" in message or "42703" in message:
                print(f"[prep_flow:db] prep_flow_runs.provenance is not installed — the "
                      f"pipeline trace was computed but could not be stored. Apply "
                      f"{PROVENANCE_MIGRATION} to keep it. ({message[:160]})")
            else:
                print(f"[prep_flow:db] finish_run(provenance) failed: {message[:300]}")


async def fetch_run(run_id: str) -> Optional[dict]:
    result = await _write(
        "fetch_run",
        lambda: create_admin_client().table("prep_flow_runs")
        .select("*").eq("id", run_id).limit(1).execute(),
    )
    rows = (result.data if result else None) or []
    return rows[0] if rows else None


async def list_runs(*, grade: str = None, subject: str = None, school_id: str = None,
                    limit: int = 20) -> list[dict]:
    def query():
        q = (create_admin_client().table("prep_flow_runs")
             .select("id, chapter_number, chapter_title, grade, subject, status, "
                     "topic_count, created_at, completed_at")
             .order("created_at", desc=True).limit(limit))
        if grade:
            q = q.eq("grade", str(grade))
        if subject:
            q = q.ilike("subject", subject)
        if school_id:
            q = q.eq("school_id", school_id)
        return q.execute()

    result = await _write("list_runs", query)
    return (result.data if result else None) or []


# ── Topics and materials ──────────────────────────────────────────────────────

async def save_topics(run_id: str, specs: list[dict]) -> None:
    """Upsert every topic in one round trip.

    One call rather than forty: at chapter scale the per-row latency is the whole
    cost of this step, and `(run_id, idx)` makes the upsert idempotent so a
    resumed run overwrites its own rows instead of creating a second T7.
    """
    if not specs:
        return
    rows = [{
        "id": spec["id"],
        "run_id": run_id,
        "idx": spec["index"],
        "topic": spec["topic"],
        "subtopic": spec.get("subtopic") or None,
        "page_start": spec.get("page_start"),
        "page_end": spec.get("page_end"),
        "excerpt": spec.get("excerpt"),
        "knowledge": _jsonable(spec.get("knowledge") or {}),
        "plan": _jsonable(spec.get("plan") or {}),
        "selection": _jsonable(spec.get("selection") or {}),
        "contract": _jsonable(spec.get("contract") or {}),
        "created_at": now_iso(),
    } for spec in specs]

    result = await _write(
        "save_topics",
        lambda: create_admin_client().table("prep_flow_topics")
        .upsert(rows, on_conflict="run_id,idx").execute(),
    )
    if result is not None or not any(row["contract"] for row in rows):
        return

    # RETRIED WITHOUT THE CONTRACT COLUMN, once. PostgREST rejects an upsert
    # naming an unknown column as one atomic failure, so a database that has 030
    # but not 035 would lose the whole topic spine — pages, excerpts, plans,
    # everything — to a column added later for an optional field. The topics
    # matter more than the slice; the whole document is still written to the run
    # row by save_contract(), which reports its own failure.
    stripped = [{k: v for k, v in row.items() if k != "contract"} for row in rows]
    if await _write(
        "save_topics(without contract)",
        lambda: create_admin_client().table("prep_flow_topics")
        .upsert(stripped, on_conflict="run_id,idx").execute(),
    ) is not None:
        print(f"[prep_flow:db] prep_flow_topics.contract is not installed — the "
              f"topics were stored without their per-topic contract slice. Apply "
              f"{CONTRACT_MIGRATION} to keep it.")


async def save_contract(run_id: str, document: dict) -> None:
    """Store Node 1's whole contract on the run row.

    A SEPARATE update rather than a field on `finish_run`'s patch, for the same
    reason `provenance` is one: PostgREST rejects an UPDATE naming any unknown
    column as one atomic failure, and folded in, a database missing migration 035
    would take the run's status and metrics down with an optional column.

    Best-effort, and that is a deliberate ranking rather than laziness. The
    contract is already in the state the caller holds — `persist_node` returns it
    — so a failure here costs the ability to replay Node 2 against this run
    later, not the run.
    """
    if not document:
        return
    try:
        await _run(
            lambda: create_admin_client().table("prep_flow_runs")
            .update({"contract": _jsonable(document)}).eq("id", run_id).execute()
        )
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "PGRST204" in message or "42703" in message:
            print(f"[prep_flow:db] prep_flow_runs.contract is not installed — Node "
                  f"1's contract was derived but could not be stored, so this run "
                  f"cannot be handed to Node 2 from the database. Apply "
                  f"{CONTRACT_MIGRATION}. ({message[:160]})")
        else:
            print(f"[prep_flow:db] save_contract failed: {message[:300]}")


async def fetch_contract(run_id: str) -> Optional[dict]:
    """One run's contract, for the node that consumes it.

    Returns None both for a run that has no contract and for a database without
    the column — the caller's question is "can I hand this to Node 2", and the
    answer is no either way. The two are distinguished in the log, not in the
    return value.
    """
    try:
        result = await _run(
            lambda: create_admin_client().table("prep_flow_runs")
            .select("contract").eq("id", run_id).limit(1).execute()
        )
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "PGRST204" in message or "42703" in message:
            print(f"[prep_flow:db] prep_flow_runs.contract is not installed. Apply "
                  f"{CONTRACT_MIGRATION}.")
        else:
            print(f"[prep_flow:db] fetch_contract failed: {message[:300]}")
        return None
    rows = getattr(result, "data", None) or []
    return (rows[0] or {}).get("contract") or None if rows else None


async def save_materials(run_id: str, records: list[dict]) -> None:
    """Upsert every generated sheet in one round trip.

    `activity` is denormalised onto the row from `material.challenge.activity`.
    PostgREST cannot select a JSON path (`material -> 'challenge' ->> 'activity'`),
    and the feedback loop needs per-activity satisfaction rates — so the choice is
    a column here or pulling every material JSON back to compute it. A column.
    """
    if not records:
        return
    rows = []
    for record in records:
        material = record["material"] or {}
        challenge = material.get("challenge")
        activity = (challenge.get("activity") if isinstance(challenge, dict) else None) or None
        rows.append({
            "id": record["id"],
            "run_id": run_id,
            "topic_id": record["topic_id"],
            "idx": record["index"],
            "material": _jsonable(material),
            "markdown": record.get("markdown"),
            "validation": _jsonable(record.get("validation") or {}),
            "activity": activity,
            "revision": int(record.get("revision") or 0),
            "created_at": now_iso(),
        })

    await _write(
        "save_materials",
        lambda: create_admin_client().table("prep_flow_materials")
        .upsert(rows, on_conflict="run_id,idx").execute(),
    )


async def fetch_run_materials(run_id: str) -> list[dict]:
    """Every sheet for a run, with its topic, ordered by teaching position.

    The topic fields come back through PostgREST's embedded-resource syntax,
    which resolves the materials -> topics foreign key server-side.
    """
    result = await _write(
        "fetch_run_materials",
        lambda: create_admin_client().table("prep_flow_materials")
        .select("id, idx, material, markdown, validation, revision, activity, "
                "prep_flow_topics(topic, subtopic, page_start, page_end)")
        .eq("run_id", run_id).order("idx").execute(),
    )
    rows = (result.data if result else None) or []

    flattened = []
    for row in rows:
        # A one-to-one embed arrives as an object, but PostgREST returns a list
        # when it cannot prove the relationship is to-one — handle both rather
        # than depending on which it decides.
        topic = row.get("prep_flow_topics") or {}
        if isinstance(topic, list):
            topic = topic[0] if topic else {}
        flattened.append({
            "material_id": row["id"],
            "idx": row["idx"],
            "material": row.get("material") or {},
            "markdown": row.get("markdown"),
            "validation": row.get("validation") or {},
            "revision": row.get("revision", 0),
            "activity": row.get("activity"),
            "topic": topic.get("topic"),
            "subtopic": topic.get("subtopic"),
            "page_start": topic.get("page_start"),
            "page_end": topic.get("page_end"),
        })
    return flattened


# ── Feedback telemetry ────────────────────────────────────────────────────────

async def record_feedback(rows: list[dict]) -> int:
    """Store a teacher's responses. One insert for the whole form."""
    if not rows:
        return 0
    payload = [{
        "id": row["id"],
        "material_id": row.get("material_id"),
        "run_id": row.get("run_id"),
        "teacher_id": row.get("teacher_id"),
        "scope_key": row["scope_key"],
        "section": row.get("section"),
        "rating": row["rating"],
        "note": row.get("note"),
        "taught_on": _jsonable(row.get("taught_on")),
        # The reinforcement loop's join key. Absent on rows written before
        # migration 036, which is why it is nullable and never back-filled: a
        # guessed topic is a rating attributed to an adaptation it was never
        # about, and that is worse than a rating attributed to nothing.
        "topic_index": row.get("topic_index"),
        "created_at": now_iso(),
    } for row in rows]

    result = await _write(
        "record_feedback",
        lambda: create_admin_client().table("prep_flow_feedback")
        .insert(payload).execute(),
    )
    if result is None and any(row.get("topic_index") is not None for row in payload):
        # `topic_index` arrived with migration 036, and PostgREST rejects the
        # WHOLE insert when it names a column the table does not have. Retrying
        # without it is the difference between a deployment that has not applied
        # 036 losing the topic and losing the entire form — and the form is the
        # input to both feedback loops, so the second is not a trade worth making
        # to keep this code shorter.
        #
        # Only on a payload that actually carried one, so an insert that failed
        # for any other reason is not retried into the same failure.
        degraded = [{k: v for k, v in row.items() if k != "topic_index"}
                    for row in payload]
        result = await _write(
            "record_feedback(no topic_index)",
            lambda: create_admin_client().table("prep_flow_feedback")
            .insert(degraded).execute(),
        )
        if result is not None:
            print(f"[prep_flow:db] feedback stored WITHOUT topic_index — apply "
                  f"{ADAPTATIONS_MIGRATION} to keep it. Ratings recorded from now "
                  f"until then can be counted, but not attributed to the "
                  f"adaptations they are about.")
    if result is None:
        return 0
    return len(result.data or payload)


async def fetch_feedback_window(key: str, window_days: int) -> list[dict]:
    """Every response for a cohort inside the window.

    The cutoff is computed here rather than in SQL — PostgREST has no
    `make_interval`, and a Python-side cutoff is one fewer thing that behaves
    differently between the two persistence paths. The activity name rides along
    on the embedded material row, which is why it is a column there.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, window_days))).isoformat()
    result = await _write(
        "fetch_feedback_window",
        lambda: create_admin_client().table("prep_flow_feedback")
        .select("section, rating, note, created_at, material_id, "
                "prep_flow_materials(idx, activity)")
        .eq("scope_key", key).gte("created_at", cutoff)
        .order("created_at", desc=True).limit(5000).execute(),
    )
    rows = (result.data if result else None) or []

    out = []
    for row in rows:
        material = row.get("prep_flow_materials") or {}
        if isinstance(material, list):
            material = material[0] if material else {}
        out.append({
            "section": row.get("section"),
            "rating": row.get("rating"),
            "note": row.get("note"),
            "created_at": row.get("created_at"),
            "material_id": row.get("material_id"),
            "idx": material.get("idx"),
            "activity": material.get("activity"),
        })
    return out


async def feedback_pulse(key: str, window_days: int = 40) -> dict:
    """Raw signals for the loop's health check — is anything still arriving?

    Liveness proves a state that WAS written reaches the model. This answers the
    other half: whether the loop is still being fed. A cohort whose teachers
    quietly stopped filling the form stays green forever otherwise — the last
    state remains active and correct, and nobody notices there has been no new
    evidence for two months.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, window_days))).isoformat()

    async def _count_since(since: Optional[str]) -> int:
        def query():
            q = (create_admin_client().table("prep_flow_feedback")
                 .select("id").eq("scope_key", key).limit(5000))
            if since:
                q = q.gte("created_at", since)
            return q.execute()
        result = await _write("feedback_pulse(count)", query)
        return len((result.data if result else None) or [])

    latest = await _write(
        "feedback_pulse(latest)",
        lambda: create_admin_client().table("prep_flow_feedback")
        .select("created_at").eq("scope_key", key)
        .order("created_at", desc=True).limit(1).execute())
    last_cycle = await _write(
        "feedback_pulse(cycle)",
        lambda: create_admin_client().table("prep_flow_patterns")
        .select("created_at, sample_size").eq("scope_key", key)
        .order("created_at", desc=True).limit(1).execute())
    state = await _write(
        "feedback_pulse(state)",
        lambda: create_admin_client().table("prep_flow_adaptive_state")
        .select("version, created_at, sample_size, state").eq("scope_key", key)
        .eq("active", True).order("version", desc=True).limit(1).execute())

    latest_rows = (latest.data if latest else None) or []
    cycle_rows = (last_cycle.data if last_cycle else None) or []
    state_rows = (state.data if state else None) or []

    return {
        "scopeKey": key,
        "windowDays": window_days,
        "responsesInWindow": await _count_since(cutoff),
        "responsesEver": await _count_since(None),
        "lastResponseAt": latest_rows[0]["created_at"] if latest_rows else None,
        "lastCycleAt": cycle_rows[0]["created_at"] if cycle_rows else None,
        "lastCycleSample": cycle_rows[0].get("sample_size") if cycle_rows else None,
        "activeVersion": state_rows[0]["version"] if state_rows else None,
        "activeSince": state_rows[0]["created_at"] if state_rows else None,
        "activeState": (state_rows[0].get("state") or {}) if state_rows else {},
    }


async def save_patterns(pattern_id: str, key: str, window_days: int,
                        sample_size: int, aggregates: dict, patterns: list) -> None:
    await _write(
        "save_patterns",
        lambda: create_admin_client().table("prep_flow_patterns").insert({
            "id": pattern_id,
            "scope_key": key,
            "window_days": window_days,
            "sample_size": sample_size,
            "aggregates": _jsonable(aggregates),
            "patterns": _jsonable(patterns),
            "created_at": now_iso(),
        }).execute(),
    )


# ── Adaptive generation state ─────────────────────────────────────────────────

async def fetch_active_adaptive_state(key: str) -> dict:
    """What the next chapter generated for this cohort will be told.

    Prefers the row flagged active, and falls back to the highest version when
    none is — which closes the only gap in `activate_adaptive_state` below.
    PostgREST has no multi-statement transaction, so activating a new version is
    three calls with a brief moment where nothing is flagged active; without this
    fallback a generation run landing in that moment would silently start cold.

    An empty dict is the correct cold-start answer: no feedback yet means no
    adjustments, not a failure.
    """
    def query(active_only: bool):
        q = (create_admin_client().table("prep_flow_adaptive_state")
             .select("version, state, sample_size, active")
             .eq("scope_key", key))
        if active_only:
            q = q.eq("active", True)
        return q.order("version", desc=True).limit(1).execute()

    result = await _write("fetch_adaptive_state", lambda: query(True))
    rows = (result.data if result else None) or []
    if not rows:
        result = await _write("fetch_adaptive_state(fallback)", lambda: query(False))
        rows = (result.data if result else None) or []
        if rows:
            print(f"[prep_flow:db] no active adaptive state for {key}; using "
                  f"v{rows[0]['version']} (highest version)")
    if not rows:
        return {}

    row = rows[0]
    state = row.get("state")
    if not isinstance(state, dict):
        return {}
    return {**state, "_version": row["version"], "_sampleSize": row.get("sample_size", 0)}


async def activate_adaptive_state(state_id: str, key: str, state: dict,
                                  derived_from: dict = None, changelog: list = None,
                                  sample_size: int = 0) -> Optional[int]:
    """Append a new version and make it the active one.

    Append-only and never updated in place: when a state makes the material
    worse, the fix is to reactivate the previous version, and that is only
    possible if it still exists.

    Ordered insert-inactive -> deactivate-others -> activate-new, because the
    partial unique index allows exactly one active row per cohort and there is no
    transaction to hide the intermediate state in. Doing it the other way round
    (deactivate first) would leave the cohort with no active row if the insert
    then failed — a silent regression to cold-start generation, which is worse
    than the brief window this ordering leaves and which
    fetch_active_adaptive_state's fallback already covers.
    """
    if not configured():
        return None

    def insert_inactive() -> Optional[int]:
        client = create_admin_client()
        existing = (client.table("prep_flow_adaptive_state").select("version")
                    .eq("scope_key", key).order("version", desc=True)
                    .limit(1).execute().data or [])
        version = (existing[0]["version"] if existing else 0) + 1
        client.table("prep_flow_adaptive_state").insert({
            "id": state_id,
            "scope_key": key,
            "version": version,
            "state": _jsonable(state),
            "derived_from": _jsonable(derived_from or {}),
            "changelog": _jsonable(changelog or []),
            "sample_size": sample_size,
            "active": False,
            "created_at": now_iso(),
        }).execute()
        return version

    version = await _write("activate_adaptive_state(insert)", insert_inactive)
    if version is None:
        return None

    def flip() -> bool:
        client = create_admin_client()
        (client.table("prep_flow_adaptive_state").update({"active": False})
         .eq("scope_key", key).eq("active", True).execute())
        (client.table("prep_flow_adaptive_state").update({"active": True})
         .eq("id", state_id).execute())
        return True  # see _write's convention: None would read as a failure

    if await _write("activate_adaptive_state(flip)", flip) is None:
        # The version exists but is not flagged active. Not a failure worth
        # discarding it over — fetch_active_adaptive_state falls back to the
        # highest version, so it still takes effect.
        print(f"[prep_flow:db] adaptive state v{version} for {key} was stored but "
              f"could not be flagged active; the version fallback will pick it up")
    return version


async def rollback_adaptive_state(key: str, version: int) -> Optional[dict]:
    """Make an earlier version active again — the answer to a bad optimization.

    Deliberately not an LLM decision and not automatic: an adaptive state that
    made the material worse is something a human notices in the sheets, and the
    reversal should be as blunt as the observation.
    """
    def flip():
        client = create_admin_client()
        target = (client.table("prep_flow_adaptive_state")
                  .select("id, version, state, sample_size")
                  .eq("scope_key", key).eq("version", version).limit(1).execute().data or [])
        if not target:
            return None
        (client.table("prep_flow_adaptive_state").update({"active": False})
         .eq("scope_key", key).eq("active", True).execute())
        (client.table("prep_flow_adaptive_state").update({"active": True})
         .eq("id", target[0]["id"]).execute())
        return target[0]

    return await _write("rollback_adaptive_state", flip)


async def list_adaptive_states(key: str, limit: int = 20) -> list[dict]:
    result = await _write(
        "list_adaptive_states",
        lambda: create_admin_client().table("prep_flow_adaptive_state")
        .select("version, active, sample_size, changelog, created_at")
        .eq("scope_key", key).order("version", desc=True).limit(limit).execute(),
    )
    return (result.data if result else None) or []


# ── Curriculum reasoning: a cache, not a record ───────────────────────────────
#
# Deliberately NOT in TABLES, so `verify_schema()` does not report a deployment
# without migration 031 as broken. Everything above is a record — losing it loses
# the run. This is a cache of something derivable: a miss costs one LLM call and
# changes nothing else, so a chapter must generate identically whether or not the
# table exists. Reported by health() as a capability, the way resumability is.

REASONING_TABLE = "prep_flow_reasoning"
REASONING_MIGRATION = "migrations/031_prep_flow_reasoning.sql"

_reasoning_unavailable = False


async def _reasoning_call(label: str, fn) -> Optional[Any]:
    """Like `_write`, but a missing table is a one-line notice and then silence.

    `_write` points at migration 030 and says the pipeline's tables are missing,
    which would be actively misleading here — nothing is missing, an optional
    cache is simply not installed. After the first notice the flag suppresses the
    rest, because a 40-chapter batch printing the same advisory forty times
    trains people to ignore the log.
    """
    global _reasoning_unavailable
    if not configured() or _reasoning_unavailable:
        return None
    try:
        return await _run(fn)
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "PGRST205" in message or "42P01" in message:
            _reasoning_unavailable = True
            print(f"[prep_flow:db] {REASONING_TABLE} is not installed — curriculum "
                  f"reasoning will be regenerated on every run. Apply "
                  f"{REASONING_MIGRATION} to cache it.")
        else:
            print(f"[prep_flow:db] {label} failed: {message[:300]}")
        return None


async def reasoning_available() -> bool:
    """Whether the reasoning cache table is installed. For health reporting."""
    if not configured():
        return False
    result = await _reasoning_call(
        "reasoning_probe",
        lambda: create_admin_client().table(REASONING_TABLE)
        .select("cache_key").limit(1).execute())
    return result is not None


async def fetch_reasoning(cache_key: str) -> dict:
    """The stored reasoning for this chapter, or {} — which means "generate it"."""
    result = await _reasoning_call(
        "fetch_reasoning",
        lambda: create_admin_client().table(REASONING_TABLE)
        .select("reasoning").eq("cache_key", cache_key).limit(1).execute())
    rows = (result.data if result else None) or []
    reasoning = rows[0].get("reasoning") if rows else None
    return reasoning if isinstance(reasoning, dict) else {}


async def save_reasoning(cache_key: str, *, grade: str, subject: str,
                         chapter_title: str, chapter_number: Optional[int],
                         reasoning: dict) -> None:
    """Store it under its cache key, replacing any previous answer for that key.

    Upsert rather than insert: two runs of the same chapter started together both
    generate and both write, and a unique-violation traceback on the second is a
    failure mode invented by the cache rather than found by it. The key already
    fingerprints the topic spine, so the two answers describe the same chapter and
    either is correct.
    """
    await _reasoning_call(
        "save_reasoning",
        lambda: create_admin_client().table(REASONING_TABLE).upsert({
            "cache_key": cache_key,
            "grade": str(grade),
            "subject": subject,
            "chapter_title": chapter_title,
            "chapter_number": chapter_number,
            "reasoning": _jsonable(reasoning),
            "created_at": now_iso(),
        }, on_conflict="cache_key").execute())


# ── Learner diagnoses ─────────────────────────────────────────────────────────
#
# What the simulated learner could not do, kept after the run that found it.
#
# OPTIONAL, on the same terms as the reasoning cache: a chapter generates,
# repairs and persists identically whether or not this table exists, and the
# full verdict history is written to prep_flow_materials.validation either way.
# What the table buys is a question the jsonb column cannot answer — "how often
# has this dimension failed for this cohort, for the same stated reason" — and
# that is a question nothing asks yet.
#
# It is EVIDENCE. Nothing here feeds a generation prompt, and the step that
# would turn accumulated rows into a durable constraint is deliberately absent:
# a root cause is one model's account of one sheet, and promoting an account
# into a permanent rule is a decision that needs thresholds, a provenance tag
# and a way to retire what stops being true.

LEARNER_DIAGNOSES_TABLE = "prep_flow_learner_diagnoses"
LEARNER_DIAGNOSES_MIGRATION = "migrations/033_prep_flow_learner_diagnoses.sql"

_diagnoses_unavailable = False


async def _diagnoses_call(label: str, fn) -> Optional[Any]:
    """Like `_write`, but a missing table is one notice and then silence.

    `_write` would point at migration 030 and say the pipeline's tables are
    missing, which is wrong twice over here: nothing is missing, and nothing is
    lost — the same verdicts are already in `validation`. After the first notice
    the flag suppresses the rest, because a forty-chapter batch printing the same
    advisory forty times teaches people to skip the log.
    """
    global _diagnoses_unavailable
    if not configured() or _diagnoses_unavailable:
        return None
    try:
        return await _run(fn)
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "PGRST205" in message or "42P01" in message:
            _diagnoses_unavailable = True
            print(f"[prep_flow:db] {LEARNER_DIAGNOSES_TABLE} is not installed — "
                  f"learner failures stay inside each run's material rows and "
                  f"cannot be counted across runs. Apply "
                  f"{LEARNER_DIAGNOSES_MIGRATION} to keep them queryable.")
        else:
            print(f"[prep_flow:db] {label} failed: {message[:300]}")
        return None


async def learner_diagnoses_available() -> bool:
    """Whether the evidence table is installed. For health reporting."""
    if not configured():
        return False
    result = await _diagnoses_call(
        "learner_diagnoses_probe",
        lambda: create_admin_client().table(LEARNER_DIAGNOSES_TABLE)
        .select("id").limit(1).execute())
    return result is not None


async def save_learner_diagnoses(run_id: str, rows: list[dict]) -> None:
    """Upsert one run's failed dimensions in a single round trip.

    Upsert on (run_id, idx, sim_round, dimension) rather than insert: a resumed
    run re-judges the sheets it re-generated, and a second row claiming the same
    failure happened twice would corrupt the only number this table is for.
    """
    if not rows:
        return
    await _diagnoses_call(
        "save_learner_diagnoses",
        lambda: create_admin_client().table(LEARNER_DIAGNOSES_TABLE)
        .upsert([{**row, "run_id": run_id} for row in rows],
                on_conflict="run_id,idx,sim_round,dimension").execute())


async def fetch_learner_diagnoses_window(key: str, window_days: int) -> list[dict]:
    """Every recorded failure for a cohort inside the window.

    The cutoff is computed here rather than in SQL, for the reason
    `fetch_feedback_window` gives: PostgREST has no `make_interval`, and one
    Python-side cutoff behaves the same down both persistence paths.

    Returns `[]` when the table is not installed, which reads the same as a quiet
    window on purpose — a caller counting evidence should find none, not fail.
    """
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, window_days))).isoformat()
    result = await _diagnoses_call(
        "fetch_learner_diagnoses_window",
        lambda: create_admin_client().table(LEARNER_DIAGNOSES_TABLE)
        .select("idx, topic, subtopic, dimension, score, weighted, gated, failure, "
                "root_cause, affected_sections, question, sim_round, revision, "
                "resolved_by_repair, run_id, chapter_title, created_at")
        .eq("scope_key", key).gte("created_at", cutoff)
        .order("created_at", desc=True).limit(5000).execute())
    return (result.data if result else None) or []


# ── Review ────────────────────────────────────────────────────────────────────
#
# The attempts a chapter made, and the decisions a teacher took about them.
#
# OPTIONAL for generation, on the same terms as the reasoning cache and the
# learner-diagnoses table: a chapter runs, repairs, selects and persists
# identically without these. What is unavailable without them is the review
# packet, and the endpoint says which migration is missing rather than serving
# something half-empty.

REVIEW_TABLES = ("prep_flow_material_attempts", "prep_flow_reviews")
REVIEW_MIGRATION = "migrations/034_prep_flow_review.sql"

_review_unavailable = False


async def _review_call(label: str, fn) -> Optional[Any]:
    """Like `_write`, but a missing table is one notice and then silence."""
    global _review_unavailable
    if not configured() or _review_unavailable:
        return None
    try:
        return await _run(fn)
    except Exception as exc:
        message = str(exc)
        if "does not exist" in message or "PGRST205" in message or "42P01" in message:
            _review_unavailable = True
            print(f"[prep_flow:db] the review tables are not installed — chapters "
                  f"still generate and persist, but teacher review is unavailable. "
                  f"Apply {REVIEW_MIGRATION}.")
        else:
            print(f"[prep_flow:db] {label} failed: {message[:300]}")
        return None


async def review_available() -> bool:
    """Whether the review tables are installed. For health reporting."""
    if not configured():
        return False
    result = await _review_call(
        "review_probe",
        lambda: create_admin_client().table("prep_flow_reviews")
        .select("id").limit(1).execute())
    return result is not None


async def save_material_attempts(run_id: str, rows: list[dict]) -> None:
    """Upsert every revision of every repaired sheet, in one round trip.

    Upsert on (run_id, idx, revision) rather than insert: a resumed run rewrites
    the revisions it regenerated, and a second row for the same attempt would
    make the review packet show a chapter trying four times when it tried three.
    """
    if not rows:
        return
    await _review_call(
        "save_material_attempts",
        lambda: create_admin_client().table("prep_flow_material_attempts")
        .upsert([{**row, "run_id": run_id} for row in rows],
                on_conflict="run_id,idx,revision").execute())


async def fetch_material_attempts(run_id: str) -> list[dict]:
    """Every stored attempt for a run, oldest revision first per topic.

    Returns `[]` when the tables are absent, which reads the same as "no topic
    needed a second attempt" — the caller distinguishes the two through
    `review_available()` rather than by guessing from an empty list.
    """
    result = await _review_call(
        "fetch_material_attempts",
        lambda: create_admin_client().table("prep_flow_material_attempts")
        .select("idx, revision, material, markdown, blocking, advisory, "
                "learner_weighted, learner_passed, readiness_weighted, "
                "readiness_passed, shipped, created_at")
        .eq("run_id", run_id).order("idx").order("revision").limit(2000).execute())
    return (result.data if result else None) or []


async def save_review(review_id: str, run_id: str, *, action: str,
                      idx: int = None, teacher_id: str = None,
                      note: str = None) -> Optional[str]:
    """Record one decision. Returns the id, or None if it could not be stored.

    None matters here in a way it does not for the pipeline's other writes: a
    review the teacher believes they submitted and which was silently dropped is
    worse than an error, so the route turns this into a 503 rather than a 201.
    """
    result = await _review_call(
        "save_review",
        lambda: create_admin_client().table("prep_flow_reviews").insert({
            "id": review_id, "run_id": run_id, "idx": idx,
            "teacher_id": teacher_id, "action": action,
            "note": (note or "").strip()[:4000] or None,
            "created_at": now_iso(),
        }).execute())
    return review_id if result else None


async def fetch_reviews(run_id: str) -> list[dict]:
    """Every decision taken about a run, newest first."""
    result = await _review_call(
        "fetch_reviews",
        lambda: create_admin_client().table("prep_flow_reviews")
        .select("id, idx, teacher_id, action, note, created_at")
        .eq("run_id", run_id).order("created_at", desc=True).limit(500).execute())
    return (result.data if result else None) or []


async def set_run_status(run_id: str, status: str) -> None:
    """Move a run to a review outcome.

    Deliberately narrow: it writes `status` and `updated_at` and nothing else.
    `finish_run` is the pipeline's own writer and also stamps completion, metrics
    and provenance — reusing it here would let a review overwrite the record of
    what the run actually did.
    """
    await _write(
        "set_run_status",
        lambda: create_admin_client().table("prep_flow_runs")
        .update({"status": status, "updated_at": now_iso()})
        .eq("id", run_id).execute())
