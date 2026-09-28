"""Node 2's persistence — the half of the shadow record that outlives one run.

Until now this package wrote nothing. `shadow.record()` builds a complete account
of a run — every proposal, its factor, type, section, evidence, and the gate's
verdict on it — and that account reached a JSON file or nowhere. For reading one
run that was enough. The reinforcement loop asks a different question:

    "across the last three batches, did adaptations of THIS kind help?"

which is a join across runs, and a JSON file per run is not something you can
join. So the same record is written to rows here, and `shadow.record()` is left
exactly as it was — it still serves the human reader, and this serves the loop.

WHAT IS DELIBERATELY NOT HERE. No reading beyond `latest_version`. The attribution
join belongs to the reinforcement package that consumes it, not to the node that
produces the rows: a producer that also owned the queries would end up shaped by
them, and Node 2's job is to plan a lesson, not to serve an analytics layer.

BEST-EFFORT, ALWAYS. Every write goes through `prep_flow.db._write` rather than a
local reimplementation — its convention (None means failed, never raises, names a
missing table specially) is the single policy this codebase handles Supabase
failures under, and a second copy would drift on the day someone fixes one of
them. A failed insert here must never cost a chapter its plan.
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from . import factors as factors_module


def _db():
    """`prep_flow.db`, imported at call time rather than at module import.

    DELIBERATELY LAZY, for two reasons that both showed up immediately.

    `prep_flow.db` pulls in `prep_flow.deps`, which resolves `ai.py`, which needs
    httpx — so a module-level import makes THIS file unimportable on a checkout
    without the backend's dependencies, including in a test that never touches
    Supabase. Persistence is best-effort by design; its import should be too.

    And the obvious guard around a module-level import is worse than none. An
    `except ImportError` fallback catches the missing-httpx error as readily as a
    layout problem and re-raises something about relative imports, which is a
    confident answer to the wrong question. Returning None here lets the caller
    say the true thing: nothing was stored.
    """
    try:
        from prep_flow import db
        return db
    except Exception as exc:            # noqa: BLE001 - reported, never raised
        print(f"[context_flow:store] persistence unavailable — prep_flow.db "
              f"could not be imported ({str(exc)[:160]}). The run completes; it "
              f"contributes nothing to the reinforcement loop.")
        return None


MIGRATION = "migrations/036_context_adaptations.sql"

TABLES = ("prep_flow_context_runs", "prep_flow_adaptations")


UNKNOWN_TIER = "unknown"


def _tier(factor_id: str) -> str:
    """The factor's tier, resolved at write time.

    Denormalised into the row on purpose — see the migration.

    AN UNKNOWN FACTOR RECORDS `unknown`, NOT A GUESS. The first version of this
    defaulted to `baseline`, on the reasoning that losing a whole row over a
    label costs more than the label is worth. That reasoning is right about not
    dropping the row and wrong about the value: tier is one of the three buckets
    attribution groups by, so a mislabelled row does not merely lose information,
    it moves evidence into a bucket it does not belong to and makes the wrong
    dial move. `baseline` is the largest bucket, so the error would also be the
    hardest to notice.

    `unknown` groups separately, can never be mistaken for a measurement, and
    says out loud that the plan named a factor the registry does not have — which
    is itself worth someone's attention.
    """
    factor = factors_module.BY_ID.get(factor_id)
    if factor is not None:
        return factor.tier
    print(f"[context_flow:store] adaptation names factor {factor_id!r}, which is "
          f"not in the registry — stored with tier 'unknown' so it is excluded "
          f"from tier attribution rather than counted as baseline.")
    return UNKNOWN_TIER


def _row(adaptation: dict, *, context_run_id: str, run_id: Optional[str],
         key: str, ordinal: int) -> dict:
    confidence = adaptation.get("confidence")
    return {
        "id": str(uuid.uuid4()),
        "context_run_id": context_run_id,
        "run_id": run_id,
        "scope_key": key,
        "topic_index": adaptation.get("topicIndex"),
        # The plan's own ordinal when it has one, which it does on every real
        # run — reason_node stamps it at the moment the final ordering exists.
        # The positional fallback is for a plan assembled by hand or by a test;
        # it agrees with the stamped value whenever the list is whole, and this
        # writer is only ever given the whole list.
        "ordinal": adaptation.get("ordinal", ordinal),
        # "" is what plan.py writes when the model named no section or named one
        # that is not among the six. NULL is the honest storage of that: an
        # adaptation with no section is un-attributable, and a lookup keyed on
        # section should miss it rather than match an empty string.
        "section": adaptation.get("section") or None,
        "factor": adaptation.get("factor") or "",
        "tier": _tier(adaptation.get("factor") or ""),
        "type": adaptation.get("type") or "no_change",
        "purpose": adaptation.get("purpose") or None,
        "change": adaptation.get("change") or None,
        "reason": adaptation.get("reason") or None,
        "supports": adaptation.get("supports") or [],
        "evidence": adaptation.get("evidence") or [],
        "data_source": adaptation.get("dataSource") or None,
        "confidence": float(confidence) if confidence is not None else None,
        # `accepted` is None until the gate has run. Storing None as False is
        # correct here and only here: a plan persisted before the gate ran has
        # nothing accepted yet, and the row will be rewritten when it has.
        "accepted": bool(adaptation.get("accepted")),
        "rejected_because": adaptation.get("rejectedBecause") or [],
    }


async def save_plan(state: dict, *, reinforcement_version: Optional[int] = None
                    ) -> Optional[str]:
    """Persist one Node 2 run and every adaptation it proposed.

    Returns the context_run id, or None if nothing was stored — a caller should
    treat that as "the loop will not learn from this run", never as an error.

    ACCEPTED AND REJECTED ALIKE. A plan is written whole. `plan.accepted()`
    exists and is not used here: filtering to survivors at write time would make
    "this factor never helps" and "this factor never survived the gate"
    indistinguishable later, and they call for opposite responses.
    """
    db = _db()
    if db is None or not db.configured():
        return None

    document = state.get("contract") or {}
    plan = state.get("plan") or {}
    adaptations = plan.get("adaptations") or []

    grade = document.get("grade") or ""
    subject = document.get("subject") or ""
    key = db.scope_key(state.get("school_id"), str(grade), subject)

    context_run_id = str(uuid.uuid4())
    gate = state.get("gate") or {}

    header = {
        "id": context_run_id,
        "run_id": state.get("run_id"),
        "scope_key": key,
        "school_id": state.get("school_id"),
        "class_id": state.get("class_id"),
        "grade": str(grade) or None,
        "subject": subject or None,
        "chapter": (document.get("chapter") or {}).get("title"),
        # The distinction attribution depends on. A shadow run's proposals never
        # reached generation, so no rating a teacher gave can be evidence about
        # them — `mode` is what lets the join exclude them rather than quietly
        # scoring lessons against changes they do not contain.
        "mode": "shadow" if state.get("shadow") else "applied",
        "status": state.get("status"),
        "reinforcement_version": reinforcement_version,
        "profile": state.get("profile_summary") or {},
        "activation": state.get("activation") or {},
        # `findings` can be long and is for human review; the counts are what
        # aggregate. shadow.record() keeps both — this keeps the half that joins.
        "gate": {k: v for k, v in gate.items() if k != "findings"},
        "metrics": state.get("metrics") or {},
    }

    written = await db._write(
        "save_context_run",
        lambda: db.create_admin_client().table("prep_flow_context_runs")
        .insert(header).execute())
    if written is None:
        return None

    if not adaptations:
        # A run that proposed nothing still gets its header row. The header is
        # what says "Node 2 looked at this batch", and without it a run that
        # found nothing to do is indistinguishable from one that never ran —
        # which is the same distinction `no_change` exists to preserve one level
        # down.
        return context_run_id

    rows = [_row(a, context_run_id=context_run_id, run_id=state.get("run_id"),
                 key=key, ordinal=i)
            for i, a in enumerate(adaptations)]

    stored = await db._write(
        "save_adaptations",
        lambda: db.create_admin_client().table("prep_flow_adaptations")
        .insert(rows).execute())
    if stored is None:
        print(f"[context_flow:store] the run header was written but its "
              f"{len(rows)} adaptation(s) were not — this run will contribute "
              f"nothing to attribution. Apply {MIGRATION} if the table is missing.")
    return context_run_id


async def record_adoption(context_run_id: str, verdicts: dict) -> int:
    """Fill in Node 3's answer to "did the sheet show any trace of this?".

    `verdicts` maps (topic_index, ordinal) -> {"adopted": ..., "note": ...}.

    WHY THIS IS A SEPARATE WRITE. Adoption is not knowable when the plan is made
    — it is a fact about the generated material, which does not exist yet. The
    column stays NULL in between, and NULL is load-bearing: attribution drops an
    adaptation it cannot confirm landed rather than crediting or blaming it, so
    a Node 3 that never ran quietly withholds evidence instead of inventing it.
    """
    db = _db()
    if db is None or not db.configured() or not verdicts:
        return 0

    stamp = datetime.now(timezone.utc).isoformat()

    updated = 0
    for (topic_index, ordinal), verdict in verdicts.items():
        adopted = (verdict or {}).get("adopted")
        if adopted not in ("landed", "not_landed", "unknown"):
            continue
        patch = {"adopted": adopted,
                 "adopted_note": (verdict or {}).get("note") or None,
                 "adopted_at": stamp}
        result = await db._write(
            "record_adoption",
            lambda p=patch, t=topic_index, o=ordinal:
                db.create_admin_client().table("prep_flow_adaptations").update(p)
                .eq("context_run_id", context_run_id)
                .eq("topic_index", t).eq("ordinal", o).execute())
        if result is not None:
            updated += 1
    return updated


async def latest_version(key: str) -> Optional[int]:
    """The highest reinforcement policy version any run under this cohort has
    been generated with.

    Read by the feedback window, which is bounded by policy version rather than
    by days: a bulk batch is taught over a term, and a 40-day window would
    discard most of the evidence it produced.
    """
    db = _db()
    if db is None or not db.configured():
        return None
    result = await db._write(
        "latest_reinforcement_version",
        lambda: db.create_admin_client().table("prep_flow_context_runs")
        .select("reinforcement_version").eq("scope_key", key)
        .not_.is_("reinforcement_version", "null")
        .order("reinforcement_version", desc=True).limit(1).execute())
    rows = getattr(result, "data", None) or []
    return rows[0].get("reinforcement_version") if rows else None


async def available() -> bool:
    """Whether migration 036 has been applied.

    Called before a cycle so "the loop found no evidence" and "the tables the
    evidence lives in do not exist" are reported as the different problems they
    are.
    """
    db = _db()
    if db is None or not db.configured():
        return False
    for table in TABLES:
        result = await db._write(
            f"verify_{table}",
            lambda t=table: db.create_admin_client().table(t)
            .select("id").limit(1).execute())
        if result is None:
            return False
    return True
