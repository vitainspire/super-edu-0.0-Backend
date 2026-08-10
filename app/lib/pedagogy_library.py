"""Phase C: the Pedagogy Library — curated teaching knowledge (activity
templates, resource/assessment metadata) independent of any textbook. This is
"World 2" in the ingestion/pedagogy split:

    World 1 (ingestion, Phase B)      World 2 (this module, Phase C)
    Book -> Chapter -> Topic          Competency -> Recommended Activities
    -> Concept -> Competency          -> Assessment Methods -> Resource Levels

They meet only at generation time, via the shared `competencies` and
`contexts` tables (both already exist — see migration 020 / canonical_mapping.py):

    Chunk -> Competency -> get_recommended_activities() -> Activity Template
    -> (Prompt Assembly Engine picks a Context) -> Generate Lesson

Deliberately does NOT import vision_extraction or canonical_mapping's LLM path
— authoring an activity template is a curated, admin-driven action, not an
extraction, so there's no fuzzy matching here. get_or_create_context() is
idempotent on exact name only, which is enough for an admin typing a context
that already exists.
"""

import uuid
from datetime import datetime, timezone

VALID_GRADE_BANDS = frozenset({"1-3", "4-5"})
VALID_RESOURCE_LEVELS = frozenset({0, 1, 2})
VALID_GROUPINGS = frozenset({"individual", "pair", "group"})
VALID_CLASSROOM_TYPES = frozenset({"indoor", "outdoor", "both"})
VALID_BLOOM_LEVELS = frozenset({
    "remember", "understand", "apply", "analyze", "evaluate", "create",
})

_TEMPLATE_FIELDS = {
    "description", "resource_level", "duration_min", "duration_max", "grouping",
    "classroom_type", "bloom_level", "fln_compatible", "assessment_method",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── Stage 2: the pedagogy lookup a lesson generator actually calls ───────────

def get_recommended_activities(
    ac, competency_ids: list, resource_level: int = None, grade_band: str = None,
) -> list:
    """Given the competencies a topic teaches, return every activity template
    that targets at least one of them — each with its full context list, so
    the caller (the Prompt Assembly Engine) picks whichever context fits the
    teacher's setting.

    resource_level, if given, filters to templates a classroom at that level
    can actually run (0 <= resource_level: a level-0 classroom can't run a
    level-2 activity, but a level-2 classroom can still run a level-0 one).
    grade_band, if given, restricts to that band only.
    """
    if not competency_ids:
        return []

    comp_links = (
        ac.table("activity_template_competencies")
        .select("activity_template_id, competency_id")
        .in_("competency_id", competency_ids).execute().data or []
    )
    if not comp_links:
        return []

    matched_by_template: dict = {}
    for link in comp_links:
        matched_by_template.setdefault(link["activity_template_id"], set()).add(link["competency_id"])
    template_ids = list(matched_by_template)

    q = ac.table("activity_templates").select("*").in_("id", template_ids)
    if grade_band:
        q = q.eq("grade_band", grade_band)
    templates = q.execute().data or []
    if resource_level is not None:
        templates = [t for t in templates if (t.get("resource_level") or 0) <= resource_level]
    if not templates:
        return []

    matched_ids = [t["id"] for t in templates]
    context_links = (
        ac.table("activity_template_contexts").select("activity_template_id, context_id")
        .in_("activity_template_id", matched_ids).execute().data or []
    )
    context_ids = list({link["context_id"] for link in context_links})
    contexts_by_id = {}
    if context_ids:
        rows = ac.table("contexts").select("id, name, category").in_("id", context_ids).execute().data or []
        contexts_by_id = {r["id"]: r for r in rows}

    contexts_by_template: dict = {}
    for link in context_links:
        contexts_by_template.setdefault(link["activity_template_id"], []).append(
            contexts_by_id.get(link["context_id"])
        )

    return [
        {
            "id": t["id"],
            "gradeBand": t.get("grade_band"),
            "category": t.get("category"),
            "name": t.get("name"),
            "description": t.get("description"),
            "resourceLevel": t.get("resource_level"),
            "durationMin": t.get("duration_min"),
            "durationMax": t.get("duration_max"),
            "grouping": t.get("grouping"),
            "classroomType": t.get("classroom_type"),
            "bloomLevel": t.get("bloom_level"),
            "flnCompatible": t.get("fln_compatible"),
            "assessmentMethod": t.get("assessment_method"),
            "matchedCompetencyIds": sorted(matched_by_template.get(t["id"], [])),
            "contexts": [c for c in contexts_by_template.get(t["id"], []) if c],
        }
        for t in sorted(templates, key=lambda t: (t.get("category") or "", t.get("name") or ""))
    ]


# ── Admin authoring ───────────────────────────────────────────────────────────

def list_activity_templates(ac, grade_band: str = None, category: str = None) -> list:
    q = ac.table("activity_templates").select("*")
    if grade_band:
        q = q.eq("grade_band", grade_band)
    if category:
        q = q.eq("category", category)
    rows = q.execute().data or []
    return sorted(rows, key=lambda r: (r.get("grade_band") or "", r.get("category") or "", r.get("name") or ""))


def get_activity_template(ac, template_id: str) -> dict:
    row = ac.table("activity_templates").select("*").eq("id", template_id).execute().data
    if not row:
        raise ValueError(f"no such activity template: {template_id}")
    return row[0]


def create_activity_template(ac, grade_band: str, category: str, name: str, **fields) -> dict:
    if grade_band not in VALID_GRADE_BANDS:
        raise ValueError(f"grade_band must be one of {sorted(VALID_GRADE_BANDS)}")
    category = (category or "").strip()
    name = (name or "").strip()
    if not category or not name:
        raise ValueError("category and name are required")

    row = {
        "id": str(uuid.uuid4()),
        "grade_band": grade_band,
        "category": category,
        "name": name,
        "resource_level": 0,
        "fln_compatible": True,
        "created_at": _now(),
    }
    row.update({k: v for k, v in fields.items() if k in _TEMPLATE_FIELDS and v is not None})
    ac.table("activity_templates").insert(row).execute()
    return row


def update_activity_template(ac, template_id: str, **fields) -> dict:
    patch = {k: v for k, v in fields.items() if k in _TEMPLATE_FIELDS and v is not None}
    if not patch:
        raise ValueError("nothing to update")
    ac.table("activity_templates").update(patch).eq("id", template_id).execute()
    return get_activity_template(ac, template_id)


def delete_activity_template(ac, template_id: str) -> dict:
    ac.table("activity_templates").delete().eq("id", template_id).execute()
    return {"id": template_id}


def link_competency(ac, template_id: str, competency_id: str) -> None:
    try:
        ac.table("activity_template_competencies").insert({
            "id": str(uuid.uuid4()), "activity_template_id": template_id,
            "competency_id": competency_id, "created_at": _now(),
        }).execute()
    except Exception:
        pass  # already linked — unique constraint, not an error worth surfacing


def unlink_competency(ac, template_id: str, competency_id: str) -> None:
    (
        ac.table("activity_template_competencies")
        .delete().eq("activity_template_id", template_id).eq("competency_id", competency_id).execute()
    )


def link_context(ac, template_id: str, context_id: str) -> None:
    try:
        ac.table("activity_template_contexts").insert({
            "id": str(uuid.uuid4()), "activity_template_id": template_id,
            "context_id": context_id, "created_at": _now(),
        }).execute()
    except Exception:
        pass


def unlink_context(ac, template_id: str, context_id: str) -> None:
    (
        ac.table("activity_template_contexts")
        .delete().eq("activity_template_id", template_id).eq("context_id", context_id).execute()
    )


def get_or_create_competency(ac, name: str) -> str:
    """Exact-match get-or-create for a competency, used by authoring/seeding —
    NOT resolve_canonical()'s LLM fuzzy matching, which is for ingestion
    extraction reconciling against whatever an admin already typed here, not
    the other way around. An admin authoring a template who types a name
    close-but-not-identical to an existing competency should see two rows and
    fix it themselves (via canonical_mapping.merge_canonical), not have this
    silently guess."""
    name = (name or "").strip()
    if not name:
        raise ValueError("competency name is required")
    existing = ac.table("competencies").select("id").ilike("name", name).limit(1).execute().data
    if existing:
        return existing[0]["id"]
    new_id = str(uuid.uuid4())
    ac.table("competencies").insert({
        "id": new_id, "name": name, "aliases": [], "created_at": _now(),
    }).execute()
    return new_id


def get_or_create_context(ac, name: str, category: str = None) -> str:
    """Look up a context by exact name (case-insensitive), or create it.

    Deliberately NOT routed through canonical_mapping's LLM matching — an
    admin (or the seed script) typing an exact context name needs idempotency,
    not fuzzy dedup. Ingestion-discovered contexts still go through
    resolve_canonical(), which will pick up whatever this creates via the
    normal alias/exact-match fast path.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("context name is required")
    existing = ac.table("contexts").select("id").ilike("name", name).limit(1).execute().data
    if existing:
        return existing[0]["id"]
    new_id = str(uuid.uuid4())
    ac.table("contexts").insert({
        "id": new_id, "name": name, "category": category, "aliases": [], "created_at": _now(),
    }).execute()
    return new_id
