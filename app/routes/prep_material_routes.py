"""The Phase A/B/C prep-material pipeline, over HTTP.

Runs the same six stages scripts/prep_material_cli.py does, in the same order,
against the same real canonical + Pedagogy libraries:

    1. Curriculum extraction from pasted text  (prep_material_generator)
    2. Canonical resolution                    (canonical_mapping, Phase B)
    3. Pedagogy lookup                         (pedagogy_library, Phase C)
    4/5. Activity + context selection          (prep_material_generator)
    6. Prompt assembly -> LLM -> prep material (prep_material_generator)

Deliberately PARALLEL to /api/smart-lesson rather than a replacement, which is
the split prep_material_generator's own module docstring insists on: iterating
on these prompts must not be able to regress the feature teachers use today.
The two routes share no code beyond lib/ai.call_ai.

SIDE EFFECT worth knowing: Stage 2 grows the canonical library. A concept or
competency this content mentions that isn't in the library yet gets CREATED
(that is resolve_canonical's designed "grows organically" behaviour, not an
accident) — so calling this endpoint writes rows to concepts/competencies/
vocabulary/contexts. dryRun stops before the LLM call, not before this.
"""
import time
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from ..deps import require_user
from ..lib.canonical_mapping import resolve_canonical
from ..lib.logger import get_client_ip
from ..lib.pedagogy_library import get_recommended_activities
from ..lib.prep_material_generator import (
    build_prep_material_prompt,
    extract_knowledge_from_text,
    generate_prep_material,
    grade_band_for,
    render_prep_material_markdown,
    select_activity_and_context,
)
from ..lib.rate_limit import check_vision_rate_limit
from ..lib.schemas import PrepMaterialSchema
from ..lib.supabase_clients import create_admin_client

router = APIRouter()


def _activity_summary(activities: list) -> list:
    """The per-activity detail the CLI prints at Stage 3 — enough to see why a
    particular template won without shipping every field of every candidate."""
    return [
        {
            "name": a.get("name"),
            "category": a.get("category"),
            "matchedCompetencies": len(a.get("matchedCompetencyIds") or []),
            "contextOptions": len(a.get("contexts") or []),
        }
        for a in activities
    ]


@router.post("/prep-material")
async def prep_material(
    body: PrepMaterialSchema, request: Request, user: dict = Depends(require_user)
):
    """Generate one prep material end to end. Returns the material, a rendered
    Markdown version, and a per-stage trace — the trace is what makes a thin
    result diagnosable (an empty `activities` means the Pedagogy Library has no
    template for these competencies, which is a very different problem from a
    bad prompt, and the CLI exists precisely because that distinction matters).
    """
    # Vision tier, not the standard one: a full run is two LLM calls (Stage 1
    # extraction + Stage 6 generation), so it belongs with the expensive routes.
    ip = get_client_ip(request)
    allowed, _ = check_vision_rate_limit(ip)
    if not allowed:
        raise HTTPException(status_code=429, detail="Rate limit exceeded.")

    started = time.time()
    ac = create_admin_client()
    band = grade_band_for(body.grade)

    # ── Stage 1 — curriculum extraction from the pasted content ──────────────
    try:
        knowledge = await extract_knowledge_from_text(
            body.topic, body.subtopic or "", body.content, body.grade, body.subject,
        )
    except ValueError as exc:
        # _parse_json_response raises this with the offending window attached.
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    # ── Stage 2 — canonical resolution (writes; see the module docstring) ────
    canonical = {
        kind: resolve_canonical(ac, kind, knowledge.get(key) or [])
        for kind, key in (
            ("concepts", "concepts"),
            ("competencies", "competencies"),
            ("vocabulary", "vocabulary"),
            ("contexts", "contexts"),
        )
    }

    # ── Stage 3 — pedagogy lookup ────────────────────────────────────────────
    competency_ids = list(canonical["competencies"].values())
    activities = (
        get_recommended_activities(
            ac, competency_ids, resource_level=body.resourceLevel, grade_band=band,
        )
        if competency_ids
        else []
    )

    # ── Stage 4/5 — activity + context selection ─────────────────────────────
    activity, context = select_activity_and_context(
        activities, preferred_context=body.preferredContext,
    )

    teacher_settings = {
        "duration": body.duration,
        "classSize": body.classSize,
        "resourceLevel": body.resourceLevel,
        "language": body.language,
        "learningObjective": body.learningObjective,
        "teachingStyle": body.teachingStyle,
    }

    stages = {
        "gradeBand": band,
        "knowledge": knowledge,
        "canonical": {k: sorted(v) for k, v in canonical.items()},
        "activityCandidates": _activity_summary(activities),
        "selected": {
            "activity": (activity or {}).get("name"),
            "category": (activity or {}).get("category"),
            "context": (context or {}).get("name"),
        },
    }

    # ── Stage 6 — prompt assembly, then generation unless dryRun ─────────────
    args = (
        body.topic, body.subtopic or "", body.grade, body.subject,
        knowledge, activity, context, teacher_settings, body.previousTopic,
    )

    if body.dryRun:
        return {
            "dryRun": True,
            "prompt": build_prep_material_prompt(*args),
            "stages": stages,
            "elapsedMs": int((time.time() - started) * 1000),
        }

    try:
        material = await generate_prep_material(*args)
    except ValueError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    return {
        "material": material,
        "markdown": render_prep_material_markdown(material, body.topic, body.subtopic or ""),
        "stages": stages,
        "elapsedMs": int((time.time() - started) * 1000),
    }
