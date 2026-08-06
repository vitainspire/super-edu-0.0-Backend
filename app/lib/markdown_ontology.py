"""Markdown <-> ontology conversion for textbook extraction.

The vision pipeline currently asks the model to emit the full ontology as strict
JSON (see vision_extraction._chapter_prompt). That makes the model responsible
for three separate things at once: reading the pages, inventing a consistent id
scheme, and producing syntactically valid JSON. The last two are where it fails,
which is why vision_extraction carries robust_json_parse, _sanitize_escapes,
call_gemini(max_retries=6) and a three-tier prompt-degradation ladder.

This module moves those two responsibilities out of the model and into
deterministic, testable code:

  - The model emits Markdown, where structure is heading depth. There is no
    syntax to get wrong: a malformed heading is still readable content, and a
    truncated document still parses into everything above the cut.
  - parse_markdown_ontology() assigns every id (C_n / T_n_t / ST_n_t_s /
    E_n_t_e / S_n_t_s) from position, so ids are consistent by construction.
    The prompt no longer needs to explain an id schema, and batched chapters no
    longer need topic_start / exercise_start / subtopic_start bookkeeping
    threaded through the prompt to stay unique.
  - The `graphs` section is *derived* from the hierarchy rather than emitted.
    Every edge in it was always redundant with the chapter_id/topic_id fields.

Output shape is byte-compatible with what vision_extraction.validate_and_fix()
expects: entities.chapters / topics / subtopics / exercises / sidebars, with
subtopics FLAT and carrying topic_id (matching the flatten at
vision_extraction.py's merge step), plus graphs.*.

parse_markdown_ontology() never raises. Anything it can't understand becomes a
warning in the result, because half a chapter is worth more than an exception.

The Markdown dialect
--------------------
    ---
    chapter: 3
    title: Numbers Around Us
    pages: 24-36
    ---

    ## Counting to Twenty
    ---
    pages: 24-27
    prerequisites: [Counting to Ten]
    ---

    English summary of the topic, as prose.

    ### Writing the numeral 1 `writing_skill` p24
    Summary of the subtopic.

    #### Exercises
    - `writing_practice` p25 — Trace the numeral, then count the mangoes.

    #### Sidebars
    - p26 — Did you know? Finger counting is very old.

Everything except the `##` topic headings is optional. Prerequisites are given
by topic NAME (the model has no way to know the ids this module will assign) and
resolved to ids here; an unresolvable name is dropped with a warning, which
enforces the "prerequisites must be valid topic ids" invariant in code rather
than asking the prompt to promise it.
"""

import re
from dataclasses import dataclass, field
from typing import Optional

# Mirrored from vision_extraction's prompt rules 7 and 8.
VALID_SKILL_TYPES = frozenset({
    "reading_skill", "writing_skill", "recognition_skill", "comprehension_skill",
    "vocabulary_skill", "listening_skill", "counting_skill", "art_skill",
    "general_skill",
})
VALID_EXERCISE_TYPES = frozenset({
    "writing_practice", "art_activity", "matching_exercise", "reading_exercise",
    "comprehension", "listening_activity", "counting_activity", "general_activity",
})

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z]*\s*$")
_HEADING_RE = re.compile(r"^(#{1,6})\s+(.*)$")
# Placeholder name for the topic synthesised around an orphan '###' subtopic.
# _build() replaces it with the chapter title whenever one is known, so this
# literal only ever reaches the caller for a fragment with no chapter context.
_SYNTHETIC_TOPIC_NAME = "(untitled topic)"
_YAML_DELIM_RE = re.compile(r"^\s*---\s*$")
_BULLET_RE = re.compile(r"^\s*[-*+]\s+(.*)$")
# `skill_type` or `exercise_type` in backticks anywhere on a line
_TYPE_TOKEN_RE = re.compile(r"`([a-z_]+)`")
# p24, p. 24, pp24-27, pages 24-27, page 24
_PAGE_RE = re.compile(
    r"\b(?:pages?|pp?)\s*\.?\s*(\d{1,4})(?:\s*[-–—to]+\s*(\d{1,4}))?",
    re.IGNORECASE,
)
_ID_LIKE_RE = re.compile(r"^[CTES]{1,2}_\d+(?:_\d+)*$")
# Separators the model may put between an exercise's metadata and its text
_TEXT_SPLIT_RE = re.compile(r"\s+(?:—|–|--)\s+|\s+-\s+|:\s+")


def _classify_skill(text: str) -> tuple:
    """Fall back to vision_extraction's keyword classifier for a missing skill_type.

    Imported lazily: vision_extraction pulls in PyMuPDF and Pillow at module
    scope, and this module is useful (and unit-testable) without them.
    """
    try:
        from .vision_extraction import classify_skill
        return classify_skill(text), None
    except Exception:
        return "general_skill", "vision_extraction.classify_skill unavailable — defaulted skill_type"


def _classify_exercise(text: str) -> tuple:
    try:
        from .vision_extraction import classify_exercise
        return classify_exercise(text), None
    except Exception:
        return "general_activity", "vision_extraction.classify_exercise unavailable — defaulted exercise_type"


# ── Small parsing helpers ─────────────────────────────────────────────────────

def _parse_pages(text: str) -> tuple:
    """Extract (page_start, page_end) from any of `p24`, `pages 24-27`, `24-27`.

    Returns (0, 0) when nothing page-like is present — validate_and_fix() treats
    page_start == 0 as a ghost entity and filters it, which is the behaviour we
    want for content whose page we genuinely couldn't read.
    """
    if not text:
        return 0, 0
    m = _PAGE_RE.search(text)
    if m:
        start = int(m.group(1))
        end = int(m.group(2)) if m.group(2) else start
        return (start, end) if end >= start else (start, start)
    # A bare "24-27" or "24" with no keyword, e.g. a lone `pages:` value
    m = re.search(r"\b(\d{1,4})\s*[-–—]\s*(\d{1,4})\b", text)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return (a, max(a, b))
    m = re.fullmatch(r"\s*(\d{1,4})\s*", text)
    if m:
        p = int(m.group(1))
        return p, p
    return 0, 0


def _parse_yaml_block(lines: list) -> dict:
    """Parse the flat `key: value` subset used here. Not a YAML implementation.

    Only flat scalars and inline `[a, b]` lists appear in this dialect, so a real
    YAML parser would be a dependency bought for nothing — and pyyaml raising on
    the model's malformed indentation is exactly the failure mode this whole
    module exists to avoid.
    """
    out: dict = {}
    for raw in lines:
        line = raw.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip().lower()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            items = [v.strip().strip("'\"") for v in value[1:-1].split(",")]
            out[key] = [v for v in items if v]
        else:
            out[key] = value.strip().strip("'\"")
    return out


def _strip_type_tokens(text: str) -> str:
    return _TYPE_TOKEN_RE.sub("", text).strip()


def _extract_type(text: str, valid: frozenset) -> Optional[str]:
    """Return the type token the model wrote, valid or not.

    Deliberately returns an INVALID token rather than None so the caller can tell
    "the model gave us a bad type" (warn, then reclassify) from "the model gave
    us no type" (silently infer). Collapsing the two would hide the model
    emitting garbage — the exact signal an admin needs when reviewing an
    extraction. A valid token always wins over an earlier invalid one, since a
    heading may legitimately contain other backticked text.
    """
    tokens = _TYPE_TOKEN_RE.findall(text or "")
    for token in tokens:
        if token in valid:
            return token
    return tokens[0] if tokens else None


def _clean_heading(text: str, valid_types: frozenset) -> str:
    """Strip the metadata decorations off a heading, leaving the name."""
    out = _strip_type_tokens(text)
    out = _PAGE_RE.sub("", out)
    out = re.sub(r"\s{2,}", " ", out)
    return out.strip(" \t-–—:·|").strip()


# ── Intermediate representation ───────────────────────────────────────────────

@dataclass
class _Subtopic:
    name: str
    summary_lines: list = field(default_factory=list)
    skill_type: Optional[str] = None
    page_start: int = 0
    page_end: int = 0


@dataclass
class _Exercise:
    text: str
    exercise_type: Optional[str] = None
    page: int = 0


@dataclass
class _Sidebar:
    text: str
    page: int = 0


@dataclass
class _Topic:
    name: str
    summary_lines: list = field(default_factory=list)
    page_start: int = 0
    page_end: int = 0
    prerequisite_names: list = field(default_factory=list)
    subtopics: list = field(default_factory=list)
    exercises: list = field(default_factory=list)
    sidebars: list = field(default_factory=list)


@dataclass
class ParseResult:
    ontology: dict
    warnings: list = field(default_factory=list)
    stats: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when at least one topic was recovered — the pipeline's own bar
        (syllabus_pdf_jobs errors out on an empty topic list)."""
        return bool(self.ontology.get("entities", {}).get("topics"))


# ── Parser ────────────────────────────────────────────────────────────────────

def parse_markdown_ontology(
    markdown: str,
    chapter_number: int = 1,
    chapter_title: str = "",
    topic_start: int = 1,
    subtopic_start: int = 1,
    exercise_start: int = 1,
) -> ParseResult:
    """Parse the Markdown dialect above into the canonical ontology shape.

    topic_start / subtopic_start / exercise_start let a caller number a later
    page batch of the same chapter without colliding with an earlier batch,
    matching how vision_extraction batches large chapters.

    Never raises. Unparseable input yields an empty-but-valid ontology and a
    warning explaining what happened.
    """
    warnings: list = []
    try:
        chapter_meta, topics = _scan(markdown or "", warnings)
    except Exception as exc:  # pragma: no cover - belt and braces
        warnings.append(f"parser crashed, recovered nothing: {type(exc).__name__}: {exc}")
        chapter_meta, topics = {}, []

    return _build(
        chapter_meta, topics, warnings,
        chapter_number=chapter_number,
        chapter_title=chapter_title,
        topic_start=topic_start,
        subtopic_start=subtopic_start,
        exercise_start=exercise_start,
    )


def _scan(markdown: str, warnings: list) -> tuple:
    """Line-oriented scan. Each branch is total: no input reaches a raise."""
    lines = markdown.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    # Drop a fence wrapping the whole document (models add ```markdown despite
    # being told not to). Inner fences are left alone — they may be content.
    if lines and _FENCE_RE.match(lines[0]):
        lines = lines[1:]
        while lines and not _FENCE_RE.match(lines[-1]):
            if lines[-1].strip() == "":
                lines.pop()
            else:
                break
        if lines and _FENCE_RE.match(lines[-1]):
            lines.pop()

    chapter_meta: dict = {}
    topics: list = []
    current_topic: Optional[_Topic] = None
    current_subtopic: Optional[_Subtopic] = None
    bullet_mode: Optional[str] = None  # 'exercises' | 'sidebars' | None

    i = 0
    seen_any_heading = False

    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # ── YAML block ────────────────────────────────────────────────────────
        if _YAML_DELIM_RE.match(line):
            block, i = _consume_yaml(lines, i)
            meta = _parse_yaml_block(block)
            if not meta:
                i += 1 if i < len(lines) else 0
                continue
            if current_topic is None and not seen_any_heading:
                chapter_meta.update(meta)
            elif current_topic is not None:
                _apply_topic_meta(current_topic, meta)
            else:
                chapter_meta.update(meta)
            continue

        heading = _HEADING_RE.match(line)
        if heading:
            level = len(heading.group(1))
            text = heading.group(2).strip()
            seen_any_heading = True
            bullet_mode = None

            if level == 1:
                # Chapter title line
                chapter_meta.setdefault("title", _clean_heading(text, frozenset()))
                ps, pe = _parse_pages(text)
                if ps:
                    chapter_meta.setdefault("pages", f"{ps}-{pe}")
                current_subtopic = None
                i += 1
                continue

            if level == 2:
                current_topic = _Topic(name=_clean_heading(text, frozenset()))
                ps, pe = _parse_pages(text)
                if ps:
                    current_topic.page_start, current_topic.page_end = ps, pe
                topics.append(current_topic)
                current_subtopic = None
                i += 1
                continue

            if level == 3:
                if current_topic is None:
                    # A subtopic before any topic — keep the content rather than
                    # discard it, and say so.
                    current_topic = _Topic(name=_SYNTHETIC_TOPIC_NAME)
                    topics.append(current_topic)
                    warnings.append(
                        "found a '###' subtopic before any '##' topic — "
                        "attached it to a synthesised '(untitled topic)'"
                    )
                st = _Subtopic(name=_clean_heading(text, VALID_SKILL_TYPES))
                st.skill_type = _extract_type(text, VALID_SKILL_TYPES)
                ps, pe = _parse_pages(text)
                st.page_start, st.page_end = ps, pe
                current_topic.subtopics.append(st)
                current_subtopic = st
                i += 1
                continue

            # level >= 4: a section label such as "#### Exercises"
            label = _clean_heading(text, frozenset()).lower()
            if label.startswith("exercise") or label.startswith("activit"):
                bullet_mode = "exercises"
            elif label.startswith("sidebar") or label.startswith("note"):
                bullet_mode = "sidebars"
            else:
                # An unrecognised deep heading: treat as a subtopic so its prose
                # isn't silently dropped.
                if current_topic is not None:
                    st = _Subtopic(name=_clean_heading(text, VALID_SKILL_TYPES))
                    st.skill_type = _extract_type(text, VALID_SKILL_TYPES)
                    st.page_start, st.page_end = _parse_pages(text)
                    current_topic.subtopics.append(st)
                    current_subtopic = st
            current_subtopic = current_subtopic if bullet_mode is None else None
            i += 1
            continue

        # ── Bullets ───────────────────────────────────────────────────────────
        bullet = _BULLET_RE.match(line)
        if bullet and bullet_mode and current_topic is not None:
            body = bullet.group(1).strip()
            page_start, _ = _parse_pages(body)
            if bullet_mode == "exercises":
                etype = _extract_type(body, VALID_EXERCISE_TYPES)
                text_only = _clean_bullet_text(body)
                current_topic.exercises.append(
                    _Exercise(text=text_only, exercise_type=etype, page=page_start)
                )
            else:
                current_topic.sidebars.append(
                    _Sidebar(text=_clean_bullet_text(body), page=page_start)
                )
            i += 1
            continue

        # ── Prose ─────────────────────────────────────────────────────────────
        if stripped:
            if current_subtopic is not None:
                current_subtopic.summary_lines.append(stripped)
            elif current_topic is not None:
                current_topic.summary_lines.append(stripped)
            # Prose before any topic is chapter preamble; not part of the schema.
        i += 1

    return chapter_meta, topics


def _consume_yaml(lines: list, start: int) -> tuple:
    """Read a `---` delimited block starting at `start`. Returns (body, next_index).

    An unterminated block (truncated output) yields whatever followed the opening
    delimiter, stopping at the first heading so a missing closing `---` can't
    swallow the rest of the document.
    """
    body: list = []
    i = start + 1
    while i < len(lines):
        if _YAML_DELIM_RE.match(lines[i]):
            return body, i + 1
        if _HEADING_RE.match(lines[i]):
            return body, i  # unterminated — stop before the heading
        body.append(lines[i])
        i += 1
    return body, i


def _apply_topic_meta(topic: _Topic, meta: dict) -> None:
    if "pages" in meta:
        ps, pe = _parse_pages(str(meta["pages"]))
        if ps:
            topic.page_start, topic.page_end = ps, pe
    for key in ("page_start", "page"):
        if key in meta and not topic.page_start:
            ps, pe = _parse_pages(str(meta[key]))
            topic.page_start, topic.page_end = ps, pe or ps
    if "page_end" in meta:
        _, pe = _parse_pages(str(meta["page_end"]))
        if pe:
            topic.page_end = max(topic.page_start, pe)
    pre = meta.get("prerequisites") or meta.get("prerequisite")
    if pre:
        if isinstance(pre, str):
            pre = [p.strip() for p in pre.split(",")]
        topic.prerequisite_names = [p for p in (x.strip() for x in pre) if p]


def _clean_bullet_text(body: str) -> str:
    """Strip leading type/page metadata off a bullet, keeping the human text."""
    out = _strip_type_tokens(body)
    # Remove a leading page marker, then any separator introducing the text
    out = _PAGE_RE.sub("", out, count=1).strip()
    out = re.sub(r"^\s*(?:—|–|--|-|:)\s*", "", out).strip()
    return out or body.strip()


# ── Build the canonical ontology ──────────────────────────────────────────────

def _build(
    chapter_meta: dict,
    topics: list,
    warnings: list,
    chapter_number: int,
    chapter_title: str,
    topic_start: int,
    subtopic_start: int,
    exercise_start: int,
) -> ParseResult:
    n = _coerce_int(chapter_meta.get("chapter"), chapter_number)
    chap_id = f"C_{n}"
    title = (chapter_title or str(chapter_meta.get("title") or "")).strip() or f"Chapter {n}"
    c_start, c_end = _parse_pages(str(chapter_meta.get("pages") or ""))

    # Drop topics with no name AND no content — usually an artefact of a stray
    # heading, and validate_and_fix would discard them anyway.
    kept: list = []
    for t in topics:
        has_content = bool(
            t.summary_lines or t.subtopics or t.exercises or t.sidebars
        )
        if not t.name and not has_content:
            continue
        kept.append(t)
    dropped = len(topics) - len(kept)
    if dropped:
        warnings.append(f"dropped {dropped} empty topic heading(s)")

    # A chunk that opens with '###' (common on continuation batches, where the
    # model resumes mid-chapter) synthesises a parent topic in _scan(), which has
    # no chapter context to name it with. Here we do — and "Air Around Us" beats
    # "(untitled topic)" in the syllabus UI. Only a *real* title qualifies: the
    # "Chapter <n>" fallback above is no more informative than the placeholder.
    real_title = (chapter_title or str(chapter_meta.get("title") or "")).strip()
    if real_title:
        for t in kept:
            if t.name == _SYNTHETIC_TOPIC_NAME:
                t.name = real_title

    out_topics: list = []
    out_subtopics: list = []
    out_exercises: list = []
    out_sidebars: list = []
    chapter_structure: list = []
    exercise_mapping: list = []

    name_to_id: dict = {}
    st_counter = subtopic_start
    ex_counter = exercise_start

    for offset, t in enumerate(kept):
        t_index = topic_start + offset
        tid = f"T_{n}_{t_index}"
        name = t.name or f"Topic {t_index}"
        name_to_id.setdefault(name.strip().lower(), tid)

        # A topic with no page of its own inherits its first subtopic's, then the
        # chapter's — better than being filtered as a ghost for lack of a number.
        page_start = t.page_start
        page_end = t.page_end or t.page_start
        if not page_start:
            for st in t.subtopics:
                if st.page_start:
                    page_start, page_end = st.page_start, st.page_end or st.page_start
                    break
        if not page_start and c_start:
            page_start, page_end = c_start, c_end or c_start

        out_topics.append({
            "id": tid,
            "name": name,
            "summary": " ".join(t.summary_lines).strip(),
            "chapter_id": chap_id,
            "page_start": page_start,
            "page_end": max(page_start, page_end or page_start),
            "prerequisites": [],  # resolved in the second pass below
        })
        chapter_structure.append({"from": chap_id, "to": tid, "type": "contains"})

        for st in t.subtopics:
            summary = " ".join(st.summary_lines).strip()
            skill = st.skill_type
            if skill is None:
                skill, note = _classify_skill(f"{st.name} {summary}")
                if note and note not in warnings:
                    warnings.append(note)
            elif skill not in VALID_SKILL_TYPES:
                warnings.append(f"unknown skill_type '{skill}' on '{st.name}' — reclassified")
                skill, _ = _classify_skill(f"{st.name} {summary}")
            sp = st.page_start or page_start
            out_subtopics.append({
                "id": f"ST_{n}_{t_index}_{st_counter}",
                "name": st.name or f"Subtopic {st_counter}",
                "summary": summary,
                "skill_type": skill,
                "page_start": sp,
                "page_end": max(sp, st.page_end or sp),
                "topic_id": tid,
            })
            st_counter += 1

        for ex in t.exercises:
            etype = ex.exercise_type
            if etype is None:
                etype, note = _classify_exercise(ex.text)
                if note and note not in warnings:
                    warnings.append(note)
            elif etype not in VALID_EXERCISE_TYPES:
                warnings.append(f"unknown exercise_type '{etype}' — reclassified")
                etype, _ = _classify_exercise(ex.text)
            eid = f"E_{n}_{t_index}_{ex_counter}"
            out_exercises.append({
                "id": eid,
                "text": ex.text,
                "topic_id": tid,
                "page": ex.page or page_start,
                "exercise_type": etype,
            })
            exercise_mapping.append({"from": eid, "to": tid, "type": "tests"})
            ex_counter += 1

        for idx, sb in enumerate(t.sidebars, start=1):
            out_sidebars.append({
                "id": f"S_{n}_{t_index}_{idx}",
                "text": sb.text,
                "topic_id": tid,
                "page": sb.page or page_start,
            })

    # ── Second pass: resolve prerequisite NAMES to ids ────────────────────────
    # Enforcing "prerequisites are valid topic ids" here, in code, is the point:
    # the JSON prompt has to ask the model for this (rule 6) and cannot check it.
    concept_dependencies: list = []
    valid_ids = {t["id"] for t in out_topics}
    for offset, t in enumerate(kept):
        tid = f"T_{n}_{topic_start + offset}"
        resolved: list = []
        for raw in t.prerequisite_names:
            token = raw.strip()
            if _ID_LIKE_RE.match(token):
                if token in valid_ids or token.startswith("T_"):
                    resolved.append(token)
                else:
                    warnings.append(f"prerequisite '{token}' on {tid} is not a known id — dropped")
                continue
            hit = name_to_id.get(token.lower())
            if hit and hit != tid:
                resolved.append(hit)
            else:
                warnings.append(
                    f"prerequisite '{token}' on {tid} matched no topic in this chapter — dropped"
                )
        deduped = list(dict.fromkeys(resolved))
        for topic in out_topics:
            if topic["id"] == tid:
                topic["prerequisites"] = deduped
                break
        for pre in deduped:
            concept_dependencies.append({"from": pre, "to": tid, "type": "prerequisite_of"})

    ontology = {
        "entities": {
            "chapters": [{
                "id": chap_id,
                "number": n,
                "title": title,
                "page_start": c_start or (out_topics[0]["page_start"] if out_topics else 0),
                "page_end": c_end or max(
                    (t["page_end"] for t in out_topics), default=c_start or 0
                ),
            }],
            "topics": out_topics,
            "subtopics": out_subtopics,
            "exercises": out_exercises,
            "sidebars": out_sidebars,
        },
        "graphs": {
            "chapter_structure": chapter_structure,
            "exercise_mapping": exercise_mapping,
            "concept_dependencies": concept_dependencies,
        },
    }

    stats = {
        "topics": len(out_topics),
        "subtopics": len(out_subtopics),
        "exercises": len(out_exercises),
        "sidebars": len(out_sidebars),
        "prerequisite_edges": len(concept_dependencies),
    }
    return ParseResult(ontology=ontology, warnings=warnings, stats=stats)


def _coerce_int(value, default: int) -> int:
    try:
        m = re.search(r"\d+", str(value))
        return int(m.group(0)) if m else default
    except Exception:
        return default


# ── Inverse: ontology -> Markdown ─────────────────────────────────────────────

def ontology_to_markdown(ontology: dict, chapter_id: Optional[str] = None) -> str:
    """Render an ontology back into the Markdown dialect.

    Used for round-trip tests, and to build the re-enrichment prompt: a schema
    change becomes a cheap text-in/text-out pass over stored Markdown instead of
    re-rendering and re-reading the page images.
    """
    entities = (ontology or {}).get("entities", {}) or {}
    chapters = entities.get("chapters", []) or []
    if chapter_id:
        chapters = [c for c in chapters if c.get("id") == chapter_id]
    if not chapters:
        return ""

    topics = entities.get("topics", []) or []
    subtopics = entities.get("subtopics", []) or []
    exercises = entities.get("exercises", []) or []
    sidebars = entities.get("sidebars", []) or []

    subs_by_topic: dict = {}
    for s in subtopics:
        subs_by_topic.setdefault(s.get("topic_id"), []).append(s)
    ex_by_topic: dict = {}
    for e in exercises:
        ex_by_topic.setdefault(e.get("topic_id"), []).append(e)
    sb_by_topic: dict = {}
    for sb in sidebars:
        sb_by_topic.setdefault(sb.get("topic_id"), []).append(sb)
    name_by_id = {t.get("id"): (t.get("name") or "") for t in topics}

    out: list = []
    for chap in chapters:
        out += [
            "---",
            f"chapter: {chap.get('number', 1)}",
            f"title: {chap.get('title', '')}",
            f"pages: {chap.get('page_start', 0)}-{chap.get('page_end', 0)}",
            "---",
            "",
        ]
        chap_topics = [t for t in topics if t.get("chapter_id") == chap.get("id")]
        chap_topics.sort(key=lambda t: (t.get("page_start") or 0, t.get("id") or ""))

        for t in chap_topics:
            out.append(f"## {t.get('name', '')}")
            out.append("---")
            out.append(f"pages: {t.get('page_start', 0)}-{t.get('page_end', 0)}")
            pre_names = [name_by_id.get(p) for p in (t.get("prerequisites") or [])]
            pre_names = [p for p in pre_names if p]
            if pre_names:
                out.append(f"prerequisites: [{', '.join(pre_names)}]")
            out.append("---")
            out.append("")
            if t.get("summary"):
                out.append(t["summary"])
                out.append("")

            for s in subs_by_topic.get(t.get("id"), []):
                out.append(
                    f"### {s.get('name', '')} `{s.get('skill_type', 'general_skill')}` "
                    f"p{s.get('page_start', 0)}"
                )
                if s.get("summary"):
                    out.append(s["summary"])
                out.append("")

            topic_ex = ex_by_topic.get(t.get("id"), [])
            if topic_ex:
                out.append("#### Exercises")
                for e in topic_ex:
                    out.append(
                        f"- `{e.get('exercise_type', 'general_activity')}` "
                        f"p{e.get('page', 0)} — {e.get('text', '')}"
                    )
                out.append("")

            topic_sb = sb_by_topic.get(t.get("id"), [])
            if topic_sb:
                out.append("#### Sidebars")
                for sb in topic_sb:
                    out.append(f"- p{sb.get('page', 0)} — {sb.get('text', '')}")
                out.append("")

    return "\n".join(out).rstrip() + "\n"


# ── Extraction prompt ─────────────────────────────────────────────────────────

def markdown_chapter_prompt(
    chap_num: int,
    language: str,
    context: str,
    global_chapter_list: str,
    prior_topics_summary: str = "",
) -> str:
    """Markdown counterpart to vision_extraction._chapter_prompt.

    Deliberately shorter: no id schema to explain (this module assigns ids), no
    JSON template to embed, and no id-continuation offsets to thread through
    batches. The extraction RULES are unchanged so output is comparable.
    """
    prior_section = ""
    if prior_topics_summary:
        prior_section = f"""
ALREADY EXTRACTED from earlier pages of this same chapter (do NOT repeat these):
{prior_topics_summary}

Extract ONLY NEW content visible in the current page batch.
"""

    return f"""
You are an expert educational architect analyzing textbook pages.
Language: {language}. Read ALL text accurately in its original script.

CONTEXT: {context}
{prior_section}
FULL BOOK CHAPTER LIST (use this to avoid cross-chapter content leakage):
{global_chapter_list}

Analyze EVERY visible page image and extract the COMPLETE content shown.
Do NOT include content that belongs to other chapters listed above.

EXTRACTION RULES:
1. titles/names: transcribe exactly in original script (Hindi/Telugu/English as printed)
2. summaries: always write in clear English
3. page numbers: use the numbers PRINTED in the book images, not PDF indices
4. Exercises: capture EVERY activity — fill-in-the-blank, writing practice, colouring,
   tracing, matching, drawing, circling, answering questions, reading aloud, singing.
   For image-only activities describe what the student must do from the visual cue.
5. Sidebars: tips, "Did you know?", learning objective boxes, QR codes, margin notes
6. prerequisites: give the NAME of the earlier topic, exactly as you titled it
7. skill_type per subtopic — one of:
   reading_skill | writing_skill | recognition_skill | comprehension_skill |
   vocabulary_skill | listening_skill | counting_skill | art_skill | general_skill
8. exercise_type per exercise — one of:
   writing_practice | art_activity | matching_exercise | reading_exercise |
   comprehension | listening_activity | counting_activity | general_activity

HEADING LEVELS — this is structural, get it right:
- Every `###` subtopic MUST sit under a `##` topic. NEVER emit a `###` before the
  first `##`.
- If these pages continue a topic whose `##` heading appeared on an earlier page,
  re-state that `##` heading verbatim before its `###` subtopics. Repeating the
  heading is correct here; it is not "repeating content".
- If a section has no obvious parent topic, make it a `##` topic in its own right
  rather than a bare `###`.

Return ONLY Markdown in exactly this shape. Repeat the `##` block per topic:

---
chapter: {chap_num}
title: <chapter title as printed>
pages: <first>-<last>
---

## <topic name>
---
pages: <first>-<last>
prerequisites: [<name of an earlier topic, omit this line if none>]
---

<English summary of the topic>

### <subtopic name> `<skill_type>` p<page>
<English summary of the subtopic>

#### Exercises
- `<exercise_type>` p<page> — <describe the activity or question>

#### Sidebars
- p<page> — <sidebar content>
"""
