# Prep Material Pipeline — Phases A–C + Stage 6

What this is: a pipeline that turns raw textbook content into a structured,
ready-to-use lesson prep material, built in four pieces that stay
architecturally separate on purpose:

```
World 1 — Curriculum knowledge (Phase A/B, from ingestion)
Book -> Chapter -> Topic -> Concept -> Competency -> Vocabulary -> Context 

World 2 — Pedagogy knowledge (Phase C, curated, independent of any textbook)
Competency -> Recommended Activities -> Resource Levels -> Assessment Methods

They meet only at generation time (Stage 6):
Chunk -> Competency -> Activity Library lookup -> Prompt Assembly -> LLM -> Prep Material
```

Ingestion facts are immutable and derived from a real textbook. Pedagogy
knowledge is curated and reusable across every textbook. Keeping them in
separate tables, joined only through the shared `competencies`/`contexts`
canonical library, is what lets the Pedagogy Library grow (or get corrected)
without touching a single ingested fact, and vice versa.

This pipeline is **new and parallel** — it does not touch or replace the
production `/api/smart-lesson` route real teachers use today. That route's
Python port is a thin stand-in for a much richer ~900-line original
(`app/api/smart-lesson/route.ts` in the frontend repo); this pilot's Stage 6
output deliberately matches that route's real output shape (see below) so the
two are directly comparable, but iterating on these prompts can't regress
what's in production.

---

## Phase A — Ingestion accuracy hardening

The vision-based textbook extraction pipeline (`app/lib/vision_extraction.py`)
previously hardcoded "Grade 1" into every prompt regardless of what was
actually being ingested. Fixed by threading a real `grade` (and `subject`)
parameter from the upload form all the way through:

- Frontend upload form → `POST /api/admin/schools/{schoolId}/syllabus/extract-pdf/upload`
  (`grade`, `subject` form fields) → `syllabus_pdf_jobs.start_extraction()` →
  `vision_extraction.generate_ontology_vision(grade=..., subject=...)` → every
  prompt function (TOC detection, content-start detection, per-chapter
  extraction — both Markdown and JSON paths — and cross-chapter dependency
  inference).

Also added `_detect_coverage_gaps()`: after extraction, checks every chapter's
final page range against every extracted topic/subtopic/exercise/sidebar's
page references, and surfaces any uncovered page range as an admin-visible
warning (`ontology["extraction_warnings"]`) — catching the case where the
model silently skipped a page while still returning well-formed output.

## Phase B — Curriculum knowledge extraction + canonical mapping

**Schema** (`db/migrations/020_phase_b_semantic_entities.sql`,
`021_canonical_link_provenance.sql`):

- `concepts`, `competencies`, `vocabulary`, `contexts` — **global**, not
  school-scoped. Shared curriculum taxonomy, not per-tenant data. Each has
  `aliases text[]` for names that resolved onto it.
- `topic_concepts`, `topic_competencies`, `topic_vocabulary`, `topic_contexts`
  — junction tables keyed on the topic's `definition_id` (stable across the
  per-class-section fan-out `syllabus_topics` already uses), each carrying a
  `source_name` — the *raw* extracted name that resolved here, distinct from
  whatever the canonical entry is named today. This is what makes a bad merge
  reversible.
- `topic_learning_outcomes` — **not** canonicalized (a learning outcome is a
  specific statement about one topic, not a reusable taxonomy entry).
- `syllabus_topics.bloom_level`, `syllabus_topics.difficulty` — new columns.

**Extraction**: both the Markdown (default) and JSON (legacy) extraction
prompts in `vision_extraction.py` / `markdown_ontology.py` now ask for, per
topic: `concepts`, `competencies`, `vocabulary`, `learning_outcomes`,
`contexts`, `bloom_level`, `difficulty` — see markdown_ontology.py's
`markdown_chapter_prompt()` rule 9.

**Canonical mapping** — `app/lib/canonical_mapping.py`:

- `resolve_canonical(ac, kind, raw_names)` — the library starts **empty** and
  grows organically. Exact/alias match is free (no LLM call); an unmatched
  name triggers one LLM call per kind per extraction batch, asking whether
  each new name is the same thing as an existing entry under different
  phrasing. Deliberately biased toward creating a duplicate over merging two
  things that might not be the same — a duplicate is a minor, fixable cost;
  a wrong merge pollutes every future book that reuses the entry.
- Admin review/fix surface (`list_canonical`, `get_canonical_topics`,
  `rename_canonical`, `merge_canonical`, `delete_canonical`,
  `split_canonical`) — exposed at `/api/admin/canonical/{kind}` in
  `app/routes/admin_canonical.py`. `split_canonical` is the one that only
  works because of the `source_name` provenance column: it detaches every
  link whose raw name matches back into a brand-new entry, genuinely undoing
  a merge rather than just renaming.

Persistence: `app/lib/syllabus_persist.py`'s `persist_extraction()` calls
`resolve_canonical()` once per kind, batched across every topic in the
extraction (not per-topic), then writes the junction rows with `source_name`.

### Competency library — current live state

Snapshot as of this doc — the library is not static, it grows with every
extraction (real ingestion or CLI test runs). Re-pull with
`SELECT * FROM competencies` for the current truth; this is here so the shape
is visible without a DB connection.

**11 competencies total, but only 7 are linked to a Pedagogy Library activity
today** — the other 4 exist in the canonical library (created by CLI test
extractions) but `get_recommended_activities()` will return nothing for them
until either a matching activity template is authored, or they're merged
onto one of the 7 linked ones via `merge_canonical`:

| Competency | Aliases | Linked to an activity? |
|---|---|---|
| Count Objects | Count objects one-to-one | ✅ Count Real Objects |
| Compare Quantities | Match Groups | ✅ Compare and Estimate Quantities |
| Estimate Numbers | — | ✅ Compare and Estimate Quantities |
| Observe Objects | — | ✅ Observe Real Objects |
| Compare Objects | — | ✅ Compare Real Objects |
| Identify Similarities | — | ✅ Find Similar or Different Items |
| Identify Patterns | — | ✅ Find Similar or Different Items, Observe Visual Patterns |
| Identify total quantity | Identify Quantity | ❌ not yet |
| Match groups of objects | — | ❌ not yet |
| Identify quantity represented by a number | Match Quantity to Number, Match Quantity to Numeral | ❌ not yet |
| Identify quantity up to 5 | — | ❌ not yet |

**A real inconsistency worth flagging, not hiding**: "Identify total
quantity" and "Identify quantity represented by a number" are arguably the
same underlying competency, extracted in two different CLI sessions and
*not* merged by `resolve_canonical()`'s LLM call — canonical matching isn't
perfectly consistent run-to-run on borderline cases. This is exactly what the
`merge_canonical` admin tool exists for; it just hasn't been applied here yet
because this is pilot/test data, not a curated decision.

Concepts (7) and vocabulary (7) have grown the same way — `Counting`,
`Quantity`, `Number representation`, `One-to-one Correspondence` (alias:
`Matching`), `Numbers 1-5`, `Number Recognition`, `Quantity Comparison` for
concepts; `more`, `total`, `count`, `quantity`, `match`, `group` (alias:
`groups`), `numbers` (aliases: `five`, `four`, `numeral`, `one`, `three`,
`two`) for vocabulary. Neither is canonicalized against an activity template
(only competencies and contexts are, since those are what Phase C's junction
tables key on) — they ride along in the knowledge bundle purely as context
for Stage 6's prompt.

## Phase C — the Pedagogy Library

**Schema** (`db/migrations/022_pedagogy_library.sql`):

- `activity_templates` — `grade_band` (`'1-3'` or `'4-5'`), `category` (e.g.
  "Count", "Observe"), `name`, plus `resource_level` (0/1/2),
  `duration_min`/`max`, `grouping`, `classroom_type`, `bloom_level`,
  `fln_compatible`, `assessment_method`.
- `activity_template_competencies`, `activity_template_contexts` — junction
  tables onto the **same** `competencies`/`contexts` tables Phase B uses.
  This shared table is the entire "two worlds meet at generation time"
  mechanism — nothing is duplicated or re-declared.

Deliberately **no** separate `activity_categories` table — category is a
plain text field on each template, not an entity with its own metadata worth
a join. Follows the design doc's own recommended pattern: *Activity Category
→ Activity Template → Context*, e.g. one "Count Real Objects" template
carries ten interchangeable contexts (Mangoes, Tamarind Seeds, Pebbles, ...)
rather than ten separately-authored activities.

**Pilot content** (`scripts/seed_pedagogy_library.py`, idempotent — run again
any time, it only creates what's missing): 6 activity templates, Grade 1–3,
Indian-contextualized, across 2 of the 9 categories from the original design
doc (Count, Observe).

### The 6 activity templates, in full

| Category | Name | Resource level | Duration | Grouping | Classroom | Bloom | Assessment | Competencies | Contexts |
|---|---|---|---|---|---|---|---|---|---|
| Count | **Count Real Objects** | 0 | 5–10 min | individual | both | remember | observation | Count Objects | Tamarind Seeds, Pebbles, Bottle Caps, Mangoes, Flowers, School Bags, Benches, Windows, Trees, Water Bottles |
| Count | **Compare and Estimate Quantities** | 0 | 5–10 min | pair | both | understand | oral | Compare Quantities, Estimate Numbers | Mangoes, Flowers, Pebbles |
| Observe | **Observe Real Objects** | 0 | 5–10 min | individual | both | remember | oral | Observe Objects | Classroom Objects, School Garden, Market Pictures, Birds Around School |
| Observe | **Find Similar or Different Items** | 0 | 5–10 min | individual | both | understand | worksheet | Identify Similarities, Identify Patterns | Leaves, Fruits |
| Observe | **Compare Real Objects** | 0 | 5–10 min | pair | both | understand | oral | Compare Objects | Vegetables |
| Observe | **Observe Visual Patterns** | 0 | 5–10 min | individual | indoor | understand | worksheet | Identify Patterns | Rangoli |

All six are `fln_compatible: true` and resource level 0 (no materials needed
beyond what's already in the context) — deliberate for a Grade 1–3, low-
resource-classroom pilot; Phase C's schema supports levels 1/2 for when
higher grades or richer classrooms are authored.

### The Context Library, in full (22 entries)

The **Everyday Indian Context Library** categories from the design doc, as
actually seeded/accumulated:

| Category | Contexts |
|---|---|
| NATURE | Mangoes, Tamarind Seeds, Flowers, Leaves, Trees, Pebbles, Fruits (alias: Fruit), Birds Around School |
| SCHOOL | School Bags, Benches, Windows, Water Bottles, Classroom Objects, School Garden |
| MARKET | Market Pictures, Vegetables |
| HOME | Bottle Caps |
| FESTIVALS | Rangoli |
| *(uncategorized)* | beads, buttons, Everyday objects (alias: Real-life objects), School supplies |

The four uncategorized ones are **not** part of the curated pilot — they're
artifacts of `resolve_canonical()` creating a context on the fly during CLI
test extraction (`get_or_create_context()`'s category param is only set by
the seed script; ad-hoc extraction doesn't supply one). "Everyday objects"
in particular is exactly the abstraction the Stage 1 prompt fix (see above)
was meant to stop happening — it predates that fix. Worth a cleanup pass
(`rename_canonical`/`delete_canonical` via `/api/admin/canonical/contexts`)
before this library is treated as curated rather than pilot/test data.

**Lookup** — `app/lib/pedagogy_library.py`:

- `get_recommended_activities(ac, competency_ids, resource_level=None, grade_band=None)`
  — Stage 2 of the generation pipeline: given a topic's competencies, returns
  every matching activity template with its full context list.
- Admin authoring (`create_activity_template`, `update_activity_template`,
  `delete_activity_template`, `link_competency`/`unlink_competency`,
  `link_context`/`unlink_context`, `get_or_create_context`,
  `get_or_create_competency`) — exposed at `/api/admin/pedagogy` in
  `app/routes/admin_pedagogy.py`.

## Stage 6 — the Prompt Assembly Engine + generation

`app/lib/prep_material_generator.py`. Three responsibilities:

1. **`extract_knowledge_from_text()`** — a text-only counterpart to
   `vision_extraction`'s per-topic extraction, for when the source is pasted
   topic content rather than a PDF page image. Same output shape, so
   `resolve_canonical()` and `get_recommended_activities()` don't care which
   path produced the knowledge.
2. **`select_activity_and_context()`** — Stage 4 (Activity Selection) + Stage
   5 (Context Engine), run **before** the LLM call (per the design doc).
   Picks the activity with the most matched competencies; within its
   contexts, matches a teacher's stated preference by name/category, else
   picks **randomly** among the available contexts. (Originally fell back to
   `contexts[0]` — deterministic, so every unpreferenced call landed on
   whichever context happened to be seeded first, e.g. "Tamarind Seeds" for
   "Count Real Objects" every single time. Caught from real repeated CLI
   runs, not a test; fixed to `random.choice`, verified to vary both in a
   unit check and against the live DB.)
3. **`build_prep_material_prompt()`** / **`generate_prep_material()`** — Stage
   6 itself: assembles curriculum knowledge (Phase B) + the matched activity
   and context (Phase C) + teacher settings into one prompt, calls the LLM,
   returns the parsed material. `build_prep_material_prompt()` is exposed
   separately so a caller can inspect the exact prompt before spending a call
   on it.

**Output shape matches the real production contract** (`ParsedSmartLesson` in
`app/api/smart-lesson/route.ts`): `objective`, `previousTopicRefresher`,
`concept`, `explore` (`points` + `imageFocus`), `challenge` (`activity` +
`points`), `sectionWatch`, `materialsUsed`, `levelSet`, `timings`. Every bullet
is `{text, detail}` — `text` a 6–12 word headline, `detail` the script/wait
time/worked numbers. Not replicated from the real route: teacher-profile and
class-interests personalization, previous-topic DB history, activity
avoid-list rotation, and image generation — none of that infrastructure is
part of this pilot, so `challenge.activity` is grounded in this module's own
Pedagogy Library match instead, and `previousTopicRefresher` only appears
when the caller passes a `previous_topic` explicitly.

`render_prep_material_markdown()` renders the same result as a readable
Markdown prep sheet instead of raw JSON — the CLI's default view.

---

## The prompts, verbatim

### Stage 1 — text-based knowledge extraction (`extract_knowledge_from_text`)

```
You are an expert educational architect analyzing a single topic's teaching content.

TOPIC: {topic}
SUBTOPIC: {subtopic}
GRADE: {grade}
SUBJECT: {subject}

CONTENT:
{content}

Extract the underlying educational knowledge in this content — not activities,
just what a curriculum expert would call the ideas/skills/words involved:
- concepts: the underlying idea(s) being taught (e.g. "Addition", "Counting")
- competencies: the specific skill(s) a student practices, phrased as an action
  a student does (e.g. "Count Objects", not "Counting")
- vocabulary: new/key words this topic introduces or relies on
- learning_outcomes: what a student can DO after this topic — short action
  phrases, not sentences
- contexts: the SPECIFIC, CONCRETE real-world things the content actually names
  or clearly implies — e.g. "Mangoes", "Tamarind Seeds", "Kirana Shop",
  "Rangoli". NEVER invent a generic category like "Real-life objects",
  "Everyday objects", or "Fruit" as a stand-in for a specific noun — if the
  content only says something generic like "objects" or "things" with no
  actual named example, return an empty list rather than abstracting one
- bloom_level: the single Bloom's-taxonomy level this topic mainly targets —
  one of remember | understand | apply | analyze | evaluate | create
- difficulty: easy | medium | hard, relative to this grade level

Leave any field as an empty list (or bloom_level/difficulty omitted) rather
than guessing if the content doesn't support it.

Return ONLY valid JSON, no markdown fences:
{
  "concepts": [], "competencies": [], "vocabulary": [], "learning_outcomes": [],
  "contexts": [], "bloom_level": null, "difficulty": null
}
```

*(The original context-extraction wording said "e.g. 'Fruit', 'Market'" —
itself an abstraction — which taught the model to invent categories instead of
naming things. Rewritten after three real runs all showed the same failure
mode; verified fixed on the same input, which now correctly returns an empty
`contexts` list instead of inventing "Real-life objects".)*

### Canonical matching (`canonical_mapping._llm_match`)

```
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
{
  "resolutions": [
    {"new_name": "Recognize Numerals", "matched_existing": "Number Recognition"},
    {"new_name": "Photosynthesis", "matched_existing": null}
  ]
}
```

Model tier: `"standard"`, not `"simple"` — a wrong merge here permanently
pollutes a library every future book's extraction reads from, so this is the
one canonical-mapping call where the stakes justify the better model.

### Stage 6 — generation (`build_prep_material_prompt` / `generate_prep_material`)

```
You are an expert teaching assistant creating a lesson prep material for a
teacher in an Indian classroom. This is a TEACHER'S prep sheet, not a student
handout: the visible "text" says WHAT happens; "detail" tells the teacher HOW
to run it — what to actually say, how long to wait, whether students answer
aloud/in pairs, and what to listen for. A first-time teacher should not have
to improvise anything.

TOPIC: {topic} — {subtopic}
GRADE: {grade} | SUBJECT: {subject}
{previous topic line — either "PREVIOUS TOPIC (for the refresher): X" or
 "PREVIOUS TOPIC: none on record — set previousTopicRefresher to null."}

CURRICULUM KNOWLEDGE (extracted from the textbook — Phase B):
- Concepts: {concepts}
- Vocabulary: {vocabulary}
- Learning outcomes: {learning_outcomes}
- Bloom level: {bloom_level} | Difficulty: {difficulty}

MATCHED ACTIVITY (from the Pedagogy Library — Phase C; this is "challenge.activity"
below, copied exactly by name — adapt the details freely, but do not swap in an
unrelated activity or invent a different one):
- Name: {activity_name} ({activity_category})
- Description: {activity_description}
- Suggested context: {context_name}
- Grouping: {grouping} | Classroom: {classroom_type} | Assessment method: {assessment_method}

TEACHER SETTINGS:
- Total lesson duration: {duration} minutes | Class size: {class_size} students
- Resource level: {resource_level} (0 = no materials, 1 = notebook/pencil, 2 = local materials)
- Language: {language} | Learning objective: {learning_objective} | Teaching style: {teaching_style}

Return ONLY valid JSON (no markdown fences), matching this exact shape:
{
  "planningNote": "1-2 sentences of your own reasoning, written first: how the matched activity and context fit this topic, and what the refresher (if any) bridges from.",
  "objective": "ONE short verb-first headline, 4-8 words — not a full sentence. Good: 'Count and compare small groups of objects'. Bad: 'Students will be able to count objects.'",
  "previousTopicRefresher": null OR {"previousTopic": "...", "recap": [bullet, "... up to 3"]},
  "concept": [bullet, "... 1-3 total — DISCOVERED, not lectured. Students find the idea in pairs BEFORE it's named; only the LAST bullet has the teacher confirm it aloud. Never open with 'Explain that...'."],
  "explore": {
    "points": [bullet, "... EXACTLY 3, in order: (1) CURIOSITY HOOK using the suggested context, ending in one question; (2) PAIR-FIRST — partners guess before any explanation; (3) the activity, carrying ONE real either/or choice."],
    "imageFocus": "one short phrase describing the single most useful thing to sketch on the board"
  },
  "challenge": {
    "activity": "<copied exactly from Matched Activity>",
    "points": [bullet, "... EXACTLY 3, as PLAY -> REFLECT -> ACT, with the worked answer stated in 'detail'"]
  },
  "sectionWatch": {"refresher": bullet-or-null, "concept": bullet, "explore": bullet, "challenge": bullet, "levelSet": bullet},
  "materialsUsed": ["every item actually referenced in explore/challenge — nothing invented, nothing unused"],
  "levelSet": {"points": [bullet, "... EXACTLY 3: self-check, reflect-on-easiest, final either/or choice framed as invitation not homework"]},
  "timings": {"refresher": 3-or-omitted, "concept": 5, "explore": 10, "challenge": 10, "levelSet": 5}
}
(bullet shape: {"text": "6-12 word headline", "detail": "1-2 sentences: the script, the wait, worked numbers"})

Rules:
- EVERYTHING is bullets, at most 3 per list. "text" is a 6-12 word headline; "detail" carries the substance (1-2 sentences, never a paragraph).
- STUDENT ACTION FIRST, every section: opens with something students DO — never the teacher explaining. Rewrite any "text" starting with "Explain...", "Tell them...", "Say that..." to start with the student action.
- ANSWERS ARE FOR THE TEACHER, NOT A VERDICT: "detail" states the worked answer for the teacher's confidence; the script spoken to students stays invitational.
- Set sectionWatch.refresher to null when previousTopicRefresher is null.
- "challenge.activity" must be copied character-for-character from the Matched Activity above.
- Use one consistent currency (₹) if the lesson involves money.
- Never use the words "quiz", "test", "evaluate", "assess", "review", "recall", "prerequisite".
- "timings" should sum to roughly {duration} minutes (omit "refresher" if previousTopicRefresher is null).
```

---

