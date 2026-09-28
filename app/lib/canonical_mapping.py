"""Canonical mapping for Phase B semantic entities (concepts, competencies,
vocabulary, contexts) — resolves a batch of raw names extracted from one
chapter onto the shared, cross-book library, growing it as needed.

The library starts empty. The first textbook ever ingested creates every
concept/competency/vocabulary/context it mentions; every extraction after that
tries to match new names onto what already exists before creating anything new
— "Recognize Numerals" should resolve to the same row as an earlier "Number
Recognition", not create a duplicate.

Matching is LLM-based (exact string/alias equality is caught first, for free)
and DELIBERATELY biased toward creating a new entry over merging two entries
that might not really be the same thing: a duplicate concept is a minor,
correctable annoyance (dedupe it later); wrongly merging two distinct ideas
into one canonical row pollutes every future book that reuses it and is much
harder to notice.

Deliberately does NOT import vision_extraction at module scope — that module
pulls in PyMuPDF/Pillow, and this one should stay importable (and unit-
testable with a fake `ac`) without either.
"""

import uuid
from datetime import datetime, timezone

# table name -> name column
_TABLES = {
    "concepts": "name",
    "competencies": "name",
    "vocabulary": "term",
    "contexts": "name",
}

# table name -> (junction table, foreign key column on that junction table)
_JUNCTION_TABLES = {
    "concepts": ("topic_concepts", "concept_id"),
    "competencies": ("topic_competencies", "competency_id"),
    "vocabulary": ("topic_vocabulary", "vocabulary_id"),
    "contexts": ("topic_contexts", "context_id"),
}

_KIND_SINGULAR = {
    "concepts": "concept",
    "competencies": "competency",
    "vocabulary": "vocabulary word",
    "contexts": "context",
}

_MATCH_PROMPT = """
You are maintaining a canonical curriculum taxonomy library for {kind}.

EXISTING LIBRARY ENTRIES:
{existing}

NEWLY EXTRACTED NAMES (from a textbook chapter) — for each one, decide whether
it refers to the SAME underlying {kind_singular} as an existing entry, or is
genuinely new:
{new_names}

Rules:
- Only match when you are CONFIDENT it's the same thing under a different name
  or phrasing (e.g. "Recognize Numerals" and "Number Recognition" are the same
  competency). Two things that are merely related or often taught together are
  NOT the same — do not merge them.
- When unsure, mark it new. A duplicate entry is a minor, fixable cost; wrongly
  merging two different {kind} is not.
- "matched_existing" must be copied EXACTLY (same casing) from the library list
  above, or null if genuinely new.

Return ONLY valid JSON (no markdown):
{{
  "resolutions": [
    {{"new_name": "Recognize Numerals", "matched_existing": "Number Recognition"}},
    {{"new_name": "Photosynthesis", "matched_existing": null}}
  ]
}}
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _retrying(fn, *, attempts: int = 2):
    """Run `fn()`, retrying once on a transient connection error before
    giving up.

    `create_admin_client()` (supabase_clients.py) is a process-wide singleton
    — one httpx/HTTP2 connection pool, reused by every concurrent
    `asyncio.to_thread()` caller. `resolve_knowledge()` fans this out across
    several topics at once (gather_bounded), so a connection Supabase's edge
    quietly closed shows up here as `ConnectionTerminated` on whichever
    request happened to be mid-flight — a transient pool hiccup, not a real
    data problem. Retried once because that is exactly what re-clicking
    "Generate sheet" would get anyway, and because the alternative — this
    exception propagating out of resolve_canonical() uncaught — previously
    took down canonical resolution for ALL FOUR kinds on that topic, not just
    the one call that happened to hit it (see resolve_knowledge()'s per-kind
    try/except in tools.py, the other half of this fix).
    """
    last_exc = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except Exception as exc:
            last_exc = exc
            if attempt >= attempts:
                raise
            print(f"[CANONICAL] transient error ({exc}), retrying "
                  f"({attempt}/{attempts - 1} retries left)")
    raise last_exc  # pragma: no cover — loop always returns or raises above


def _fetch_all(ac, table: str, name_col: str) -> list:
    res = _retrying(lambda: ac.table(table).select(f"id, {name_col}, aliases").execute())
    return res.data or []


def resolve_canonical(ac, kind: str, raw_names: list, trace: dict = None) -> dict:
    """Resolve a batch of raw, as-extracted names onto canonical row ids in
    `kind` (one of "concepts", "competencies", "vocabulary", "contexts").

    Returns {raw_name: canonical_id}, keyed by the name exactly as passed in
    (after trim + duplicate-collapse), so callers can look up every name they
    started with. Creates new rows for names that don't match anything
    existing; a unique-constraint race with a concurrent extraction falls back
    to a lookup rather than failing the whole batch.

    `trace`, if given a dict, is filled in place with the breakdown of HOW each
    name resolved — {"matchedExisting": [...], "matchedViaLLM": [...],
    "created": [...]} — without changing what this function returns, so every
    existing caller is unaffected. "created" is the one list that matters for
    provenance: those are new rows this call actually wrote into the shared
    library (resolve_canonical's designed "grows organically" behaviour), as
    opposed to the other two, which only ever read what was already there.
    """
    if kind not in _TABLES:
        raise ValueError(f"unknown canonical kind: {kind}")
    name_col = _TABLES[kind]

    if trace is not None:
        trace.update({"kind": kind, "matchedExisting": [], "matchedViaLLM": [], "created": []})

    cleaned = [n.strip() for n in (raw_names or []) if n and n.strip()]
    if not cleaned:
        return {}
    seen_input: dict = {}
    for n in cleaned:
        seen_input.setdefault(n.lower(), n)
    cleaned = list(seen_input.values())

    existing = _fetch_all(ac, kind, name_col)
    by_lower = {e[name_col].strip().lower(): e for e in existing}
    for e in existing:
        for alias in (e.get("aliases") or []):
            by_lower.setdefault(alias.strip().lower(), e)

    resolved: dict = {}
    unmatched: list = []
    matched_direct: list = []
    for name in cleaned:
        hit = by_lower.get(name.lower())
        if hit:
            resolved[name] = hit["id"]
            matched_direct.append(name)
        else:
            unmatched.append(name)

    matched_llm: dict = {}
    if unmatched and existing:
        matched_llm = _llm_match(ac, kind, name_col, unmatched, existing)
        for name, canonical_id in matched_llm.items():
            resolved[name] = canonical_id
        unmatched = [n for n in unmatched if n not in resolved]

    created: list = []
    for name in unmatched:
        new_id = str(uuid.uuid4())
        try:
            _retrying(lambda: ac.table(kind).insert(
                {"id": new_id, name_col: name, "aliases": [], "created_at": _now()}
            ).execute())
            resolved[name] = new_id
            created.append(name)
        except Exception:
            # Reached either because the row genuinely already exists (a real
            # unique-constraint race with a concurrent extraction) or because
            # even the retry above hit the same transient connection problem.
            # Either way, one more read is worth it before giving up on this
            # single name — it is a lot cheaper than losing it outright.
            try:
                row = _retrying(lambda: ac.table(kind).select("id").ilike(name_col, name)
                                .limit(1).execute()).data or []
            except Exception as exc:
                print(f"[CANONICAL] Could not insert or find '{name}' in {kind} "
                      f"(connection issue: {exc}); dropping.")
                continue
            if row:
                resolved[name] = row[0]["id"]
                matched_direct.append(name)  # lost a create race — this is a read, not a write
            else:
                print(f"[CANONICAL] Could not insert or find '{name}' in {kind}; dropping.")

    if trace is not None:
        trace["matchedExisting"] = sorted(matched_direct)
        trace["matchedViaLLM"] = sorted(matched_llm)
        trace["created"] = sorted(created)
    return resolved


def _llm_match(ac, kind: str, name_col: str, unmatched: list, existing: list) -> dict:
    """Ask the model which unmatched names are really existing entries under a
    different phrasing. Returns {raw_name: canonical_id} for confident matches
    only — anything else is left for the caller to create as new.
    """
    existing_list = "\n".join(f"- {e[name_col]}" for e in existing)
    new_list = "\n".join(f"- {n}" for n in unmatched)
    prompt = _MATCH_PROMPT.format(
        kind=kind,
        kind_singular=_KIND_SINGULAR[kind],
        existing=existing_list,
        new_names=new_list,
    )

    try:
        # Lazy AND inside the try: a flat checkout (no app/lib/vision_extraction,
        # no PyMuPDF/Pillow) must degrade to "treat everything as new" exactly
        # like a real LLM failure would — not crash resolve_canonical outright,
        # which previously discarded concepts/vocabulary/contexts resolution for
        # the whole topic rather than just skipping the fuzzy-match step.
        from .vision_extraction import call_gemini, robust_json_parse
        # "standard", not "simple": a wrong merge here permanently pollutes a
        # library every future book's extraction reads from — this is the one
        # canonical-mapping call where the stakes justify the better model.
        raw = call_gemini([prompt], tier="standard")
        data = robust_json_parse(raw)
        resolutions = data.get("resolutions", []) or []
    except (ImportError, ModuleNotFoundError):
        # Expected and permanent in a flat checkout — vision_extraction lives in
        # the real backend monorepo, not here (see this module's own docstring).
        # Every unmatched name becomes new, silently, same as the log line below
        # would say every single call in this environment — printing it once per
        # call just trains the console to be ignored. Genuine failures (the API
        # call itself, a malformed response) still print, below.
        return {}
    except Exception as exc:
        print(f"[CANONICAL] LLM match failed for {kind}, treating all {len(unmatched)} as new: {exc}")
        return {}

    row_by_lower = {e[name_col].strip().lower(): e for e in existing}
    unmatched_set = set(unmatched)
    resolved: dict = {}

    for r in resolutions:
        new_name = (r.get("new_name") or "").strip()
        matched = r.get("matched_existing")
        if not new_name or new_name not in unmatched_set or not matched:
            continue
        target = row_by_lower.get(str(matched).strip().lower())
        if not target:
            print(f"[CANONICAL] Model matched '{new_name}' to '{matched}', "
                  f"which isn't in the library — ignoring, will create new.")
            continue

        resolved[new_name] = target["id"]

        # Record the alias so this exact phrasing resolves for free next time,
        # without another LLM call.
        if new_name.lower() != str(matched).strip().lower():
            aliases = set(target.get("aliases") or [])
            if new_name not in aliases:
                aliases.add(new_name)
                try:
                    ac.table(kind).update({"aliases": sorted(aliases)}).eq("id", target["id"]).execute()
                    target["aliases"] = sorted(aliases)
                except Exception as exc:
                    print(f"[CANONICAL] Could not record alias '{new_name}' on {kind} "
                          f"'{matched}': {exc}")

    return resolved


# ── Admin review/management ───────────────────────────────────────────────────
# The other half of resolve_canonical()'s "grow organically" design: a library
# that only ever grows and never gets reviewed will accumulate the model's
# mistakes forever. These give an admin (via app/routes/admin_canonical.py) the
# tools to see what's in the library, and to fix it when resolve_canonical()
# either merged two things that weren't the same (split_canonical) or missed a
# merge it should have made (merge_canonical).

def _require_kind(kind: str) -> str:
    if kind not in _TABLES:
        raise ValueError(
            f"unknown canonical kind '{kind}' — must be one of {sorted(_TABLES)}"
        )
    return _TABLES[kind]


def junction_for(kind: str) -> tuple:
    """(junction table, foreign-key column) for `kind`. Exposed so callers that
    write links themselves — syllabus_persist.py, during extraction — don't
    hard-code the same mapping a second time and drift from it."""
    _require_kind(kind)
    return _JUNCTION_TABLES[kind]


def list_canonical(ac, kind: str, search: str = None) -> list:
    """Every canonical entry for `kind`, with how many topics currently link
    to it — a `usageCount` of 0 or 1 is worth a second look; it's either a
    genuinely rare idea or a near-duplicate resolve_canonical() should have
    matched onto something else."""
    name_col = _require_kind(kind)
    junction_table, fk_col = _JUNCTION_TABLES[kind]

    rows = ac.table(kind).select(f"id, {name_col}, aliases, created_at").execute().data or []
    if search:
        needle = search.strip().lower()
        rows = [
            r for r in rows
            if needle in (r.get(name_col) or "").lower()
            or any(needle in (a or "").lower() for a in (r.get("aliases") or []))
        ]

    ids = [r["id"] for r in rows]
    counts: dict = {}
    if ids:
        links = ac.table(junction_table).select(fk_col).in_(fk_col, ids).execute().data or []
        for link in links:
            fid = link[fk_col]
            counts[fid] = counts.get(fid, 0) + 1

    return [
        {
            "id": r["id"],
            "name": r[name_col],
            "aliases": r.get("aliases") or [],
            "usageCount": counts.get(r["id"], 0),
            "createdAt": r.get("created_at"),
        }
        for r in sorted(rows, key=lambda r: (r[name_col] or "").lower())
    ]


def get_canonical_topics(ac, kind: str, entity_id: str) -> list:
    """Every topic linked to this canonical entry, with the raw name
    (source_name) that resolved to it — enough for an admin to judge whether
    each link actually belongs here."""
    _require_kind(kind)
    junction_table, fk_col = _JUNCTION_TABLES[kind]

    links = (
        ac.table(junction_table).select("topic_definition_id, source_name")
        .eq(fk_col, entity_id).execute().data or []
    )
    if not links:
        return []

    def_ids = [link["topic_definition_id"] for link in links]
    source_by_def = {link["topic_definition_id"]: link.get("source_name") for link in links}

    topic_rows = (
        ac.table("syllabus_topics")
        .select("definition_id, topic, grade, subject, class_id")
        .in_("definition_id", def_ids)
        .execute().data or []
    )

    # School is resolved through the class rather than read off the topic row.
    # syllabus_topics.school_id is not present on every deployment, and the
    # class is the real ownership link regardless — the same reasoning
    # prep_batch_jobs.py's module docstring gives for scoping through classes.
    class_ids = {t["class_id"] for t in topic_rows if t.get("class_id")}
    school_by_class: dict = {}
    if class_ids:
        class_rows = (
            ac.table("classes").select("id, school_id")
            .in_("id", list(class_ids)).execute().data or []
        )
        school_by_class = {c["id"]: c.get("school_id") for c in class_rows}

    seen = set()
    out = []
    for t in topic_rows:
        did = t["definition_id"]
        if did in seen:  # one row per class section — collapse to one per topic
            continue
        seen.add(did)
        out.append({
            "topicDefinitionId": did,
            "topic": t.get("topic"),
            "grade": t.get("grade"),
            "subject": t.get("subject"),
            "schoolId": school_by_class.get(t.get("class_id")),
            "sourceName": source_by_def.get(did),
        })
    return out


def rename_canonical(ac, kind: str, entity_id: str, new_name: str) -> dict:
    """Rename a canonical entry directly. Existing aliases and links are
    unaffected — this only changes the name resolve_canonical() and the review
    UI show for it going forward."""
    name_col = _require_kind(kind)
    new_name = (new_name or "").strip()
    if not new_name:
        raise ValueError("new name cannot be empty")

    ac.table(kind).update({name_col: new_name}).eq("id", entity_id).execute()
    row = ac.table(kind).select(f"id, {name_col}, aliases").eq("id", entity_id).execute().data
    if not row:
        raise ValueError(f"no such {kind} entry: {entity_id}")
    return row[0]


def merge_canonical(ac, kind: str, keep_id: str, merge_ids: list) -> dict:
    """Fold one or more canonical entries into `keep_id`: every topic link
    that pointed at a merged entry now points at keep_id instead (a link that
    would collide with one the kept entry already has — the same topic linked
    to both — is dropped rather than duplicated), the merged entries' names
    become aliases of the kept one, and the merged rows are deleted.

    This is the tool for the merges resolve_canonical() was too conservative
    to make on its own — two entries an admin can see are the same thing.
    """
    name_col = _require_kind(kind)
    junction_table, fk_col = _JUNCTION_TABLES[kind]
    merge_ids = [m for m in (merge_ids or []) if m and m != keep_id]
    if not merge_ids:
        raise ValueError("nothing to merge")

    rows = (
        ac.table(kind).select(f"id, {name_col}, aliases")
        .in_("id", [keep_id] + merge_ids).execute().data or []
    )
    by_id = {r["id"]: r for r in rows}
    if keep_id not in by_id:
        raise ValueError(f"no such {kind} entry: {keep_id}")

    keeper = by_id[keep_id]
    new_aliases = set(keeper.get("aliases") or [])
    existing_links = (
        ac.table(junction_table).select("topic_definition_id").eq(fk_col, keep_id).execute().data or []
    )
    keeper_topic_ids = {link["topic_definition_id"] for link in existing_links}

    merged_count = 0
    for merge_id in merge_ids:
        merged = by_id.get(merge_id)
        if not merged:
            continue
        merged_count += 1
        new_aliases.add(merged[name_col])
        new_aliases.update(merged.get("aliases") or [])

        links = (
            ac.table(junction_table).select("id, topic_definition_id")
            .eq(fk_col, merge_id).execute().data or []
        )
        for link in links:
            if link["topic_definition_id"] in keeper_topic_ids:
                ac.table(junction_table).delete().eq("id", link["id"]).execute()
                continue
            ac.table(junction_table).update({fk_col: keep_id}).eq("id", link["id"]).execute()
            keeper_topic_ids.add(link["topic_definition_id"])

        ac.table(kind).delete().eq("id", merge_id).execute()

    new_aliases.discard(keeper[name_col])
    ac.table(kind).update({"aliases": sorted(new_aliases)}).eq("id", keep_id).execute()

    return {
        "id": keep_id,
        "name": keeper[name_col],
        "aliases": sorted(new_aliases),
        "mergedCount": merged_count,
    }


def delete_canonical(ac, kind: str, entity_id: str, force: bool = False) -> dict:
    """Delete a canonical entry outright. Refuses if it's still linked to any
    topic unless force=True — the junction table's FK cascade-deletes those
    links on the DB side, so dropping real data should be a deliberate choice,
    not a side effect of an admin not realizing it was in use."""
    _require_kind(kind)
    junction_table, fk_col = _JUNCTION_TABLES[kind]

    links = ac.table(junction_table).select("id").eq(fk_col, entity_id).execute().data or []
    if links and not force:
        raise ValueError(
            f"{kind} entry {entity_id} is linked to {len(links)} topic(s) — "
            f"pass force=true to delete anyway, or merge it into another entry first"
        )

    ac.table(kind).delete().eq("id", entity_id).execute()
    return {"id": entity_id, "deletedLinks": len(links)}


def split_canonical(ac, kind: str, entity_id: str, source_name: str) -> dict:
    """Undo a bad merge: every link on `entity_id` whose recorded source_name
    matches (case-insensitively) is detached and re-pointed at a brand-new
    canonical entry named `source_name`. Links with no recorded source_name
    (written before this provenance tracking existed) can't be split this way
    — there's nothing to key the split on."""
    name_col = _require_kind(kind)
    junction_table, fk_col = _JUNCTION_TABLES[kind]
    source_name = (source_name or "").strip()
    if not source_name:
        raise ValueError("source_name is required")

    links = (
        ac.table(junction_table).select("id, source_name").eq(fk_col, entity_id).execute().data or []
    )
    matching = [
        link for link in links
        if (link.get("source_name") or "").strip().lower() == source_name.lower()
    ]
    if not matching:
        raise ValueError(f"no links on this {kind} entry have source_name '{source_name}'")

    new_id = str(uuid.uuid4())
    ac.table(kind).insert(
        {"id": new_id, name_col: source_name, "aliases": [], "created_at": _now()}
    ).execute()

    for link in matching:
        ac.table(junction_table).update({fk_col: new_id}).eq("id", link["id"]).execute()

    row = ac.table(kind).select(f"id, {name_col}, aliases").eq("id", entity_id).execute().data
    if row:
        remaining = [
            a for a in (row[0].get("aliases") or [])
            if a.strip().lower() != source_name.lower()
        ]
        ac.table(kind).update({"aliases": remaining}).eq("id", entity_id).execute()

    return {"newId": new_id, "name": source_name, "movedLinks": len(matching)}
