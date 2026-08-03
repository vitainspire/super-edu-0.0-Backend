"""
Cost comparison for textbook extraction: Markdown vs JSON ontology.

Answers one question with real numbers: for the SAME extracted information, what
does each output format cost to produce through the vision pipeline in
app/lib/vision_extraction.py?

Three formats are compared:

  json_ontology   The current production format — the full `entities` + `graphs`
                  schema that _chapter_prompt() asks for.
  markdown        Plain hierarchical Markdown. Headings carry the hierarchy that
                  JSON carries with ids and *_id foreign keys.
  markdown_meta   Hierarchical Markdown + YAML front-matter per topic, holding
                  only the fields prose can't express (page ranges, skill_type,
                  exercise_type, prerequisites).

Why the input side barely differs
---------------------------------
Both formats send the same rendered page images, and image tokens dominate the
input. At PAGE_DPI=150 an A4 page is ~1240x1754px, which Gemini tiles into 6
tiles of 258 tokens = ~1548 tokens per page — roughly 10.8k input tokens for a
PAGE_BATCH_SIZE=7 batch, against which even a 1.1k-token JSON schema prompt is
a rounding error. So the format choice is almost entirely an OUTPUT-token
decision, plus a reliability tax (below).

The reliability tax
-------------------
robust_json_parse(), _sanitize_escapes(), call_gemini(max_retries=6) and the
_chapter_prompt -> _simplified_prompt -> _minimal_prompt degradation ladder all
exist because JSON output fails to parse. Markdown has no parse-failure mode: a
malformed heading is still readable content. This script prices that in via
--json-fail-rate, which `--mode live` measures instead of assuming.

Usage
-----
Estimate mode (default — no API key, no spend):
    python extraction_cost_compare.py

Model a specific book:
    python extraction_cost_compare.py --chapters 14 --pages-per-chapter 12 \
        --topics-per-chapter 6 --exercises-per-topic 4

Scale to a deployment:
    python extraction_cost_compare.py --books 40 --re-extractions 2

Live mode — real calls against real pages, reports the API's own token usage:
    python extraction_cost_compare.py --mode live --pdf ../samples/grade1_math.pdf --pages 8-14

Pricing is fetched from OpenRouter's public model list; override with
--price-in / --price-out (USD per million tokens) to model a different model.

Reports print to the console and are written as .json + .md under --out-dir.
"""

import argparse
import io
import json
import os
import re
import sys
import textwrap
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Optional

# Windows terminals default to cp1252 and choke on em-dashes / non-ASCII output
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

_HERE = Path(__file__).resolve().parent
_BACKEND = _HERE.parent
_REPO = _BACKEND.parent
sys.path.insert(0, str(_BACKEND))

# ── Production code under measurement ─────────────────────────────────────────
# The comparison is only worth anything if it prices the REAL prompts and the
# REAL serialisers. Imported rather than reimplemented so this script can't
# quietly drift from what the pipeline actually sends.
PROD: dict = {"json_prompt": None, "md_prompt": None, "md_render": None, "md_parse": None}
PROD_NOTES: list = []

try:
    from app.lib.markdown_ontology import (
        markdown_chapter_prompt, ontology_to_markdown, parse_markdown_ontology,
    )
    PROD["md_prompt"] = markdown_chapter_prompt
    PROD["md_render"] = ontology_to_markdown
    PROD["md_parse"] = parse_markdown_ontology
except Exception as exc:
    PROD_NOTES.append(f"app.lib.markdown_ontology unavailable ({exc}) — using the local fallback")

try:
    from app.lib.vision_extraction import _chapter_prompt as _prod_json_prompt
    PROD["json_prompt"] = _prod_json_prompt
except Exception as exc:
    PROD_NOTES.append(f"app.lib.vision_extraction unavailable ({exc}) — using the local fallback")

# ── Defaults mirrored from app/lib/vision_extraction.py ───────────────────────
# Kept as module constants (not imported) so this script runs standalone without
# PyMuPDF/Pillow installed in estimate mode.
PAGE_DPI = 150
PAGE_BATCH_SIZE = 7
DEFAULT_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
OPENROUTER_MODELS_URL = "https://openrouter.ai/api/v1/models"

# Gemini image tokenisation: tiles of up to 768x768, 258 tokens each.
# Override with --tile-px / --tokens-per-tile if modelling a different model.
TILE_PX = 768
TOKENS_PER_TILE = 258

# Last-known OpenRouter pricing for google/gemini-2.5-flash, USD per 1M tokens.
# Only used when the live model list can't be reached — always printed with its
# provenance so a stale number is never mistaken for a fetched one.
FALLBACK_PRICE_IN = 0.30
FALLBACK_PRICE_OUT = 2.50

# A4 at 150 DPI, matching render_page()
PAGE_W_PX = int(8.27 * PAGE_DPI)
PAGE_H_PX = int(11.69 * PAGE_DPI)


# ── Token counting ────────────────────────────────────────────────────────────

class TokenCounter:
    """Counts tokens with tiktoken when available, else a structure-aware heuristic.

    The heuristic splits alphanumeric runs from punctuation because real BPE
    tokenisers behave very differently on the two: prose packs ~4 chars into a
    token, while dense punctuation (`{"id": "T_1_1",`) packs closer to 1.5. A
    naive chars/4 rule would understate JSON's overhead — exactly the bias this
    comparison must not have.
    """

    def __init__(self) -> None:
        self.encoder = None
        self.method = "heuristic (install tiktoken for closer estimates)"
        try:
            import tiktoken  # type: ignore
            self.encoder = tiktoken.get_encoding("cl100k_base")
            self.method = "tiktoken cl100k_base"
        except Exception:
            pass

    def count(self, text: str) -> int:
        if self.encoder is not None:
            return len(self.encoder.encode(text))
        alnum = len(re.findall(r"[A-Za-z0-9]", text))
        punct = len(re.findall(r"[^A-Za-z0-9\s]", text))
        spaces = len(re.findall(r"\s", text))
        return int(alnum / 4 + punct / 1.5 + spaces / 8) + 1


def image_tokens(width_px: int, height_px: int, tile_px: int, per_tile: int) -> int:
    """Tokens for one page image under a tiling vision model."""
    cols = max(1, -(-width_px // tile_px))   # ceil division
    rows = max(1, -(-height_px // tile_px))
    return cols * rows * per_tile


# ── Representative chapter content ────────────────────────────────────────────
# One synthetic chapter, serialised into all three formats. Synthetic is the
# point: it guarantees the formats carry IDENTICAL information, so the delta is
# pure serialisation overhead rather than one extraction being more thorough.

@dataclass
class Subtopic:
    name: str
    summary: str
    skill_type: str
    page_start: int
    page_end: int


@dataclass
class Exercise:
    text: str
    exercise_type: str
    page: int


@dataclass
class Sidebar:
    text: str
    page: int


@dataclass
class Topic:
    name: str
    summary: str
    page_start: int
    page_end: int
    prerequisites: list
    subtopics: list
    exercises: list
    sidebars: list


@dataclass
class Chapter:
    number: int
    title: str
    page_start: int
    page_end: int
    topics: list


SKILL_TYPES = [
    "reading_skill", "writing_skill", "recognition_skill", "comprehension_skill",
    "vocabulary_skill", "counting_skill", "art_skill",
]
EXERCISE_TYPES = [
    "writing_practice", "art_activity", "matching_exercise", "reading_exercise",
    "comprehension", "counting_activity", "general_activity",
]

# Text lengths tuned to the prompt's own instructions: topic summaries are full
# English sentences, exercise text "describe the activity or question".
TOPIC_SUMMARY = (
    "Students learn to recognise and write numbers from one to twenty, connecting "
    "each numeral to a matching count of familiar objects such as fruits and toys."
)
SUBTOPIC_SUMMARY = (
    "Trace and copy the numeral while saying its name aloud, then count objects to match."
)
EXERCISE_TEXT = (
    "Look at the picture of the basket and count the mangoes, then write the correct "
    "numeral in the box provided beside it."
)
SIDEBAR_TEXT = (
    "Did you know? Counting on your fingers is one of the oldest ways people learned numbers."
)


def build_chapter(
    number: int,
    topics_per_chapter: int,
    subtopics_per_topic: int,
    exercises_per_topic: int,
    sidebars_per_topic: int,
    pages_per_chapter: int,
) -> Chapter:
    page_start = (number - 1) * pages_per_chapter + 1
    topics = []
    for t in range(topics_per_chapter):
        t_page = page_start + int(t * pages_per_chapter / max(1, topics_per_chapter))
        topics.append(Topic(
            name=f"Numbers and Counting Part {t + 1}",
            summary=TOPIC_SUMMARY,
            page_start=t_page,
            page_end=t_page + 1,
            # First topic has no prerequisite; the rest depend on the previous one
            prerequisites=[] if t == 0 else [f"T_{number}_{t}"],
            subtopics=[
                Subtopic(
                    name=f"Writing numeral {t * subtopics_per_topic + s + 1}",
                    summary=SUBTOPIC_SUMMARY,
                    skill_type=SKILL_TYPES[(t + s) % len(SKILL_TYPES)],
                    page_start=t_page,
                    page_end=t_page,
                )
                for s in range(subtopics_per_topic)
            ],
            exercises=[
                Exercise(
                    text=EXERCISE_TEXT,
                    exercise_type=EXERCISE_TYPES[(t + e) % len(EXERCISE_TYPES)],
                    page=t_page,
                )
                for e in range(exercises_per_topic)
            ],
            sidebars=[
                Sidebar(text=SIDEBAR_TEXT, page=t_page)
                for _ in range(sidebars_per_topic)
            ],
        ))
    return Chapter(
        number=number,
        title=f"Chapter {number}: Numbers Around Us",
        page_start=page_start,
        page_end=page_start + pages_per_chapter - 1,
        topics=topics,
    )


# ── Serialisers — same chapter, three output formats ──────────────────────────

def to_json_ontology(ch: Chapter) -> str:
    """The current production shape, exactly as _chapter_prompt() specifies:
    flat entity lists with string ids, *_id foreign keys, and a `graphs` section.
    """
    n = ch.number
    topics, exercises, sidebars = [], [], []
    chapter_structure, exercise_mapping, concept_deps = [], [], []

    for ti, t in enumerate(ch.topics, start=1):
        tid = f"T_{n}_{ti}"
        topics.append({
            "id": tid,
            "name": t.name,
            "summary": t.summary,
            "chapter_id": f"C_{n}",
            "page_start": t.page_start,
            "page_end": t.page_end,
            "prerequisites": t.prerequisites,
            "subtopics": [
                {
                    "id": f"ST_{n}_{ti}_{si}",
                    "name": s.name,
                    "summary": s.summary,
                    "skill_type": s.skill_type,
                    "page_start": s.page_start,
                    "page_end": s.page_end,
                }
                for si, s in enumerate(t.subtopics, start=1)
            ],
        })
        chapter_structure.append({"from": f"C_{n}", "to": tid, "type": "contains"})
        for pre in t.prerequisites:
            concept_deps.append({"from": pre, "to": tid, "type": "prerequisite_of"})
        for ei, e in enumerate(t.exercises, start=1):
            eid = f"E_{n}_{ti}_{ei}"
            exercises.append({
                "id": eid, "text": e.text, "topic_id": tid,
                "page": e.page, "exercise_type": e.exercise_type,
            })
            exercise_mapping.append({"from": eid, "to": tid, "type": "tests"})
        for si, s in enumerate(t.sidebars, start=1):
            sidebars.append({
                "id": f"S_{n}_{ti}_{si}", "text": s.text, "topic_id": tid, "page": s.page,
            })

    payload = {
        "entities": {
            "chapters": [{
                "id": f"C_{n}", "number": n, "title": ch.title,
                "page_start": ch.page_start, "page_end": ch.page_end,
            }],
            "topics": topics,
            "exercises": exercises,
            "sidebars": sidebars,
        },
        "graphs": {
            "chapter_structure": chapter_structure,
            "exercise_mapping": exercise_mapping,
            "concept_dependencies": concept_deps,
        },
    }
    # separators=(",", ":") — the model is told "strict JSON, no fences", and a
    # compact emit is the most favourable case for JSON. Pretty-printed output
    # (which models often produce anyway) costs materially more.
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def to_markdown(ch: Chapter) -> str:
    """Plain hierarchical Markdown. Heading depth replaces ids and foreign keys."""
    out = [f"# {ch.title}", f"pages {ch.page_start}-{ch.page_end}", ""]
    for t in ch.topics:
        out.append(f"## {t.name}")
        out.append(f"pages {t.page_start}-{t.page_end}")
        out.append("")
        out.append(t.summary)
        out.append("")
        for s in t.subtopics:
            out.append(f"### {s.name}")
            out.append(f"{s.summary} ({s.skill_type}, p{s.page_start})")
            out.append("")
        if t.exercises:
            out.append("#### Exercises")
            for e in t.exercises:
                out.append(f"- [{e.exercise_type}] {e.text} (p{e.page})")
            out.append("")
        if t.sidebars:
            out.append("#### Sidebars")
            for sb in t.sidebars:
                out.append(f"- {sb.text} (p{sb.page})")
            out.append("")
    return "\n".join(out)


def to_markdown_meta(ch: Chapter) -> str:
    """Hierarchical Markdown + minimal YAML front matter per topic.

    Carries the machine-readable fields prose can't express — page ranges,
    skill_type, exercise_type, prerequisites — while leaving the hierarchy to
    heading depth and the prose to prose.
    """
    out = [
        "---",
        f"chapter: {ch.number}",
        f"title: {ch.title}",
        f"pages: {ch.page_start}-{ch.page_end}",
        "---",
        "",
    ]
    for ti, t in enumerate(ch.topics, start=1):
        out.append(f"## {t.name}")
        out.append("---")
        out.append(f"id: T_{ch.number}_{ti}")
        out.append(f"pages: {t.page_start}-{t.page_end}")
        if t.prerequisites:
            out.append(f"prerequisites: [{', '.join(t.prerequisites)}]")
        out.append("---")
        out.append("")
        out.append(t.summary)
        out.append("")
        for s in t.subtopics:
            out.append(f"### {s.name} `{s.skill_type}` p{s.page_start}")
            out.append(s.summary)
            out.append("")
        if t.exercises:
            out.append("#### Exercises")
            for e in t.exercises:
                out.append(f"- `{e.exercise_type}` p{e.page} — {e.text}")
            out.append("")
        if t.sidebars:
            out.append("#### Sidebars")
            for sb in t.sidebars:
                out.append(f"- p{sb.page} — {sb.text}")
            out.append("")
    return "\n".join(out)


# ── Prompts — one per format, same extraction ask ─────────────────────────────
# The JSON prompt is the real one from _chapter_prompt(), trimmed of the
# job-specific context interpolation. The Markdown prompts ask for the same
# information in the same order, so prompt length differences are structural
# (schema template vs none), not an artefact of one being written tersely.

_SHARED_RULES = """
EXTRACTION RULES:
1. titles/names: transcribe exactly in original script (Hindi/Telugu/English as printed)
2. "summary" fields: always write in clear English
3. page numbers: use the numbers PRINTED in the book images, not PDF indices
4. Exercises: capture EVERY activity — fill-in-the-blank, writing practice, colouring,
   tracing, matching, drawing, circling, answering questions, reading aloud, singing.
   For image-only activities describe what the student must do from the visual cue.
5. Sidebars: tips, "Did you know?", learning objective boxes, QR codes, margin notes
6. prerequisites: reference topics by their id (e.g. "T_2_1") — never plain text
7. skill_type per subtopic — one of:
   reading_skill | writing_skill | recognition_skill | comprehension_skill |
   vocabulary_skill | listening_skill | counting_skill | art_skill | general_skill
8. exercise_type per exercise — one of:
   writing_practice | art_activity | matching_exercise | reading_exercise |
   comprehension | listening_activity | counting_activity | general_activity
"""

JSON_PROMPT = """
You are an expert educational architect analyzing Grade 1 textbook pages.
Language: {language}. Read ALL text accurately in its original script.

Analyze EVERY visible page image and extract a COMPLETE ontology for the content shown.
""" + _SHARED_RULES + """
ID SCHEMA (embed chapter number {n}):
  chapters  -> C_{n}
  topics    -> T_{n}_1, T_{n}_2 ...
  subtopics -> ST_{n}_1_1, ST_{n}_1_2 ...
  exercises -> E_{n}_1_1, E_{n}_1_2 ...
  sidebars  -> S_{n}_1_1 ...

Return ONLY strict JSON (no markdown fences, no explanation):
{{
  "entities": {{
    "chapters": [
      {{"id": "C_{n}", "number": {n}, "title": "...", "page_start": 0, "page_end": 0}}
    ],
    "topics": [
      {{
        "id": "T_{n}_1",
        "name": "...",
        "summary": "English description...",
        "chapter_id": "C_{n}",
        "page_start": 0,
        "page_end": 0,
        "prerequisites": [],
        "subtopics": [
          {{"id": "ST_{n}_1_1", "name": "...", "summary": "...", "skill_type": "reading_skill", "page_start": 0, "page_end": 0}}
        ]
      }}
    ],
    "exercises": [
      {{"id": "E_{n}_1_1", "text": "describe the activity or question...", "topic_id": "T_{n}_1", "page": 0, "exercise_type": "general_activity"}}
    ],
    "sidebars": [
      {{"id": "S_{n}_1_1", "text": "sidebar content...", "topic_id": "T_{n}_1", "page": 0}}
    ]
  }},
  "graphs": {{
    "chapter_structure": [{{"from": "C_{n}", "to": "T_{n}_1", "type": "contains"}}],
    "exercise_mapping": [{{"from": "E_{n}_1_1", "to": "T_{n}_1", "type": "tests"}}],
    "concept_dependencies": []
  }}
}}
"""

MARKDOWN_PROMPT = """
You are an expert educational architect analyzing Grade 1 textbook pages.
Language: {language}. Read ALL text accurately in its original script.

Analyze EVERY visible page image and extract the COMPLETE content shown.
""" + _SHARED_RULES + """
Return ONLY Markdown, using heading depth for structure:

# Chapter {n}: <title>
pages <start>-<end>

## <topic name>
pages <start>-<end>

<English summary of the topic>

### <subtopic name>
<summary> (<skill_type>, p<page>)

#### Exercises
- [<exercise_type>] <describe the activity or question> (p<page>)

#### Sidebars
- <sidebar content> (p<page>)
"""

MARKDOWN_META_PROMPT = """
You are an expert educational architect analyzing Grade 1 textbook pages.
Language: {language}. Read ALL text accurately in its original script.

Analyze EVERY visible page image and extract the COMPLETE content shown.
""" + _SHARED_RULES + """
Return ONLY Markdown. Use heading depth for structure, and a short YAML block
for the machine-readable fields:

---
chapter: {n}
title: <title>
pages: <start>-<end>
---

## <topic name>
---
id: T_{n}_1
pages: <start>-<end>
prerequisites: [T_{n}_0]
---

<English summary of the topic>

### <subtopic name> `<skill_type>` p<page>
<summary>

#### Exercises
- `<exercise_type>` p<page> — <describe the activity or question>

#### Sidebars
- p<page> — <sidebar content>
"""

def flatten_subtopics(ontology: dict) -> dict:
    """Lift nested subtopics into a flat entities["subtopics"] with topic_id.

    Mirrors the flatten in vision_extraction's chunk-merge step, which is what
    turns the prompt's nested shape into the shape validate_and_fix() consumes.
    """
    out = json.loads(json.dumps(ontology))
    entities = out["entities"]
    flat = entities.setdefault("subtopics", [])
    for topic in entities.get("topics", []):
        for st in topic.pop("subtopics", []) or []:
            st["topic_id"] = topic["id"]
            flat.append(st)
    return out


def prod_json_prompt(n: int) -> str:
    if PROD["json_prompt"] is not None:
        return PROD["json_prompt"](n, "Telugu", "chapter context", "1. Numbers\n2. Shapes")
    return JSON_PROMPT.replace("{language}", "Telugu").replace("{n}", str(n))


def prod_md_prompt(n: int) -> str:
    if PROD["md_prompt"] is not None:
        return PROD["md_prompt"](n, "Telugu", "chapter context", "1. Numbers\n2. Shapes")
    return MARKDOWN_META_PROMPT.replace("{language}", "Telugu").replace("{n}", str(n))


def prod_md_render(ch: Chapter) -> str:
    """Serialise via the production renderer, so the measured Markdown is exactly
    what the parser round-trips — not an approximation written for this script."""
    if PROD["md_render"] is not None:
        return PROD["md_render"](flatten_subtopics(json.loads(to_json_ontology(ch))))
    return to_markdown_meta(ch)


FORMATS = {
    "json_ontology": (prod_json_prompt, to_json_ontology, "Current production format"),
    "markdown": (lambda n: MARKDOWN_PROMPT.replace("{language}", "Telugu").replace("{n}", str(n)),
                 to_markdown, "Plain hierarchical Markdown"),
    "markdown_meta": (prod_md_prompt, prod_md_render,
                      "Markdown + YAML metadata (production parser path)"),
}


# ── Pricing ───────────────────────────────────────────────────────────────────

@dataclass
class Pricing:
    model: str
    usd_per_m_in: float
    usd_per_m_out: float
    source: str

    def cost(self, tokens_in: int, tokens_out: int) -> float:
        return tokens_in / 1e6 * self.usd_per_m_in + tokens_out / 1e6 * self.usd_per_m_out


def fetch_pricing(model: str, override_in: Optional[float], override_out: Optional[float]) -> Pricing:
    if override_in is not None and override_out is not None:
        return Pricing(model, override_in, override_out, "--price-in/--price-out override")
    try:
        import requests
        resp = requests.get(OPENROUTER_MODELS_URL, timeout=15)
        resp.raise_for_status()
        for entry in resp.json().get("data", []):
            if entry.get("id") == model:
                p = entry.get("pricing", {})
                # OpenRouter quotes USD per token; convert to per-million.
                return Pricing(
                    model,
                    float(p["prompt"]) * 1e6,
                    float(p["completion"]) * 1e6,
                    "fetched live from OpenRouter",
                )
        raise LookupError(f"model {model} not in OpenRouter's list")
    except Exception as exc:
        return Pricing(
            model,
            override_in if override_in is not None else FALLBACK_PRICE_IN,
            override_out if override_out is not None else FALLBACK_PRICE_OUT,
            f"FALLBACK CONSTANT — live fetch failed ({type(exc).__name__}: {exc})",
        )


# ── Estimate mode ─────────────────────────────────────────────────────────────

@dataclass
class FormatResult:
    fmt: str
    note: str
    prompt_tokens: int
    image_tokens: int
    output_tokens_per_chapter: int
    output_chars_per_chapter: int
    calls_per_chapter: int
    retry_calls_per_chapter: float
    tokens_in_per_chapter: int
    tokens_out_per_chapter: float
    cost_per_chapter: float
    cost_per_book: float
    cost_total: float
    sample: str = field(default="", repr=False)


def estimate(args, pricing: Pricing, tc: TokenCounter) -> list:
    tokens_per_page = image_tokens(PAGE_W_PX, PAGE_H_PX, args.tile_px, args.tokens_per_tile)
    calls_per_chapter = max(1, -(-args.pages_per_chapter // args.page_batch_size))

    results = []
    for fmt, (build_prompt, serialise, note) in FORMATS.items():
        prompt_text = build_prompt(1)
        prompt_tokens = tc.count(prompt_text)

        chapter = build_chapter(
            1, args.topics_per_chapter, args.subtopics_per_topic,
            args.exercises_per_topic, args.sidebars_per_topic, args.pages_per_chapter,
        )
        body = serialise(chapter)
        out_tokens = tc.count(body)

        # Input: every call re-sends the prompt plus its share of page images.
        img_tokens_total = args.pages_per_chapter * tokens_per_page
        tokens_in = calls_per_chapter * prompt_tokens + img_tokens_total

        # Reliability tax: a failed JSON parse re-runs the whole call — prompt,
        # images and all — because the pipeline retries the batch, not the tail.
        fail_rate = args.json_fail_rate if fmt == "json_ontology" else args.markdown_fail_rate
        retry_calls = calls_per_chapter * fail_rate
        retry_tokens_in = retry_calls * (prompt_tokens + img_tokens_total / calls_per_chapter)
        retry_tokens_out = out_tokens * fail_rate

        total_in = tokens_in + retry_tokens_in
        total_out = out_tokens + retry_tokens_out

        cost_chapter = pricing.cost(int(total_in), int(total_out))
        cost_book = cost_chapter * args.chapters
        cost_total = cost_book * args.books * (1 + args.re_extractions)

        results.append(FormatResult(
            fmt=fmt, note=note,
            prompt_tokens=prompt_tokens,
            image_tokens=img_tokens_total,
            output_tokens_per_chapter=out_tokens,
            output_chars_per_chapter=len(body),
            calls_per_chapter=calls_per_chapter,
            retry_calls_per_chapter=round(retry_calls, 3),
            tokens_in_per_chapter=int(total_in),
            tokens_out_per_chapter=round(total_out, 1),
            cost_per_chapter=cost_chapter,
            cost_per_book=cost_book,
            cost_total=cost_total,
            sample=body,
        ))
    return results


# ── Live mode ─────────────────────────────────────────────────────────────────

def load_env():
    """Mirrors test_prep_material.py: frontend/.env.local first, then backend/.env."""
    for candidate in (_REPO / "frontend" / ".env.local", _BACKEND / ".env"):
        if not candidate.exists():
            continue
        for line in candidate.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


def parse_page_range(spec: str) -> tuple:
    if "-" in spec:
        a, b = spec.split("-", 1)
        return int(a), int(b)
    p = int(spec)
    return p, p


def live(args, pricing: Pricing, tc: TokenCounter) -> list:
    import fitz  # PyMuPDF
    import PIL.Image
    import base64
    import requests

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key:
        raise SystemExit("OPENROUTER_API_KEY not set (checked frontend/.env.local and backend/.env)")

    pdf_path = Path(args.pdf).expanduser().resolve()
    if not pdf_path.exists():
        raise SystemExit(f"PDF not found: {pdf_path}")

    first, last = parse_page_range(args.pages)
    doc = fitz.open(str(pdf_path))
    if last > doc.page_count:
        raise SystemExit(f"--pages {args.pages} exceeds the PDF's {doc.page_count} pages")

    print(f"Rendering pages {first}-{last} of {pdf_path.name} at {PAGE_DPI} DPI ...")
    parts = []
    for page_num in range(first - 1, last):
        page = doc[page_num]
        pix = page.get_pixmap(dpi=PAGE_DPI)
        img = PIL.Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=85)
        parts.append({
            "type": "image_url",
            "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()},
        })
    doc.close()
    print(f"  {len(parts)} page image(s) prepared\n")

    results = []
    for fmt, (build_prompt, _serialise, note) in FORMATS.items():
        prompt_text = build_prompt(1)
        content = [{"type": "text", "text": prompt_text}] + parts

        # json_ontology keeps the production system prompt; the Markdown formats
        # must not be told to emit JSON.
        system = (
            "You must respond with valid JSON only. No markdown fences, no preamble."
            if fmt == "json_ontology"
            else "Respond with Markdown only. No preamble, no code fences around the whole document."
        )
        payload = {
            "model": args.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": content},
            ],
            "max_tokens": args.max_output_tokens,
            "temperature": 0.15,
            "usage": {"include": True},
        }

        print(f"[{fmt}] calling {args.model} ...", flush=True)
        started = time.time()
        resp = requests.post(
            OPENROUTER_URL,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "HTTP-Referer": "https://eduteach.app",
                "X-Title": "EduTeach cost comparison",
            },
            json=payload,
            timeout=300,
        )
        elapsed = time.time() - started
        if resp.status_code >= 400:
            print(f"  ERROR {resp.status_code}: {resp.text[:400]}\n")
            continue

        data = resp.json()
        body = data["choices"][0]["message"]["content"]
        usage = data.get("usage", {}) or {}
        tin = int(usage.get("prompt_tokens", 0)) or tc.count(prompt_text)
        tout = int(usage.get("completion_tokens", 0)) or tc.count(body)

        # OpenRouter returns its own computed cost when usage.include is set;
        # prefer it over our arithmetic, and say which we used.
        api_cost = usage.get("cost")
        cost = float(api_cost) if api_cost is not None else pricing.cost(tin, tout)
        cost_source = "OpenRouter usage.cost" if api_cost is not None else "computed from pricing"

        # Did it actually parse? This is the reliability tax, measured.
        parsed_ok = True
        parse_note = "n/a (Markdown has no parse step)"
        if fmt == "json_ontology":
            stripped = re.sub(r"^```(?:json)?\s*|\s*```$", "", body.strip())
            try:
                json.loads(stripped)
                parse_note = "parsed on first attempt"
            except Exception as exc:
                parsed_ok = False
                parse_note = f"PARSE FAILED — {type(exc).__name__}: {exc}"

        print(f"  {elapsed:.1f}s · in {tin:,} · out {tout:,} · ${cost:.4f} ({cost_source})")
        print(f"  {parse_note}\n")

        results.append({
            "format": fmt,
            "note": note,
            "elapsed_s": round(elapsed, 1),
            "tokens_in": tin,
            "tokens_out": tout,
            "output_chars": len(body),
            "cost_usd": cost,
            "cost_source": cost_source,
            "parsed_ok": parsed_ok,
            "parse_note": parse_note,
            "sample": body,
        })
        if args.delay:
            time.sleep(args.delay)

    return results


# ── Reporting ─────────────────────────────────────────────────────────────────

def fmt_usd(v: float) -> str:
    if v >= 100:
        return f"${v:,.0f}"
    if v >= 1:
        return f"${v:,.2f}"
    return f"${v:.4f}"


def check_fidelity(args) -> dict:
    """Verify the Markdown and JSON payloads really do carry the same information.

    Without this the cost table is meaningless: a cheaper format that quietly
    drops half the entities isn't cheaper, it's lossy. Runs the synthetic chapter
    through JSON -> Markdown -> parser and compares entity counts.
    """
    if PROD["md_parse"] is None or PROD["md_render"] is None:
        return {"checked": False, "reason": "app.lib.markdown_ontology unavailable"}

    ch = build_chapter(
        1, args.topics_per_chapter, args.subtopics_per_topic,
        args.exercises_per_topic, args.sidebars_per_topic, args.pages_per_chapter,
    )
    original = flatten_subtopics(json.loads(to_json_ontology(ch)))
    markdown = PROD["md_render"](original)
    reparsed = PROD["md_parse"](markdown)

    def counts(ont: dict) -> dict:
        e = ont["entities"]
        return {k: len(e.get(k, [])) for k in
                ("chapters", "topics", "subtopics", "exercises", "sidebars")}

    before, after = counts(original), counts(reparsed.ontology)
    pre_before = sum(len(t.get("prerequisites") or []) for t in original["entities"]["topics"])
    pre_after = sum(len(t.get("prerequisites") or [])
                    for t in reparsed.ontology["entities"]["topics"])

    return {
        "checked": True,
        "entity_counts_before": before,
        "entity_counts_after": after,
        "entities_lossless": before == after,
        "prerequisite_edges_before": pre_before,
        "prerequisite_edges_after": pre_after,
        "prerequisites_lossless": pre_before == pre_after,
        "parser_warnings": reparsed.warnings,
    }


def price_reenrichment(args, pricing: Pricing, tc: TokenCounter) -> dict:
    """Cost of adding a schema field LATER, two ways.

    full re-extract: re-render the PDF pages and re-read them. Pays the image
                     tokens again — and re-transcribes, so text an admin already
                     reviewed can change underneath them.
    from markdown:   a text-in/text-out pass over the stored Markdown. No images,
                     and the transcription is fixed because nothing re-reads the
                     page.
    """
    tokens_per_page = image_tokens(PAGE_W_PX, PAGE_H_PX, args.tile_px, args.tokens_per_tile)
    calls = max(1, -(-args.pages_per_chapter // args.page_batch_size))
    img_total = args.pages_per_chapter * tokens_per_page

    ch = build_chapter(
        1, args.topics_per_chapter, args.subtopics_per_topic,
        args.exercises_per_topic, args.sidebars_per_topic, args.pages_per_chapter,
    )
    json_body = to_json_ontology(ch)
    md_body = prod_md_render(ch)

    json_prompt_tokens = tc.count(prod_json_prompt(1))
    json_out = tc.count(json_body)

    # Full re-extract: exactly the production JSON path again, retries included.
    re_in = calls * json_prompt_tokens + img_total
    re_out = json_out
    re_in += calls * args.json_fail_rate * (json_prompt_tokens + img_total / calls)
    re_out += json_out * args.json_fail_rate
    full = pricing.cost(int(re_in), int(re_out))

    # From stored Markdown: one text call, no page images.
    enrich_prompt = (
        "Add a `difficulty` field to every topic below. Return the same Markdown, unchanged "
        "except for the new field. Do not re-transcribe or re-word any existing content.\n\n"
    )
    md_in = tc.count(enrich_prompt) + tc.count(md_body)
    md_out = tc.count(md_body)  # returns the document with one field added
    from_md = pricing.cost(md_in, md_out)

    return {
        "full_reextract_usd_per_chapter": full,
        "from_markdown_usd_per_chapter": from_md,
        "saving_pct": (1 - from_md / full) * 100 if full else 0,
        "full_tokens_in": int(re_in),
        "full_tokens_out": int(re_out),
        "from_markdown_tokens_in": md_in,
        "from_markdown_tokens_out": md_out,
        "image_tokens_avoided": img_total,
    }


def render_estimate_report(args, pricing: Pricing, tc: TokenCounter, results: list) -> str:
    baseline = next(r for r in results if r.fmt == "json_ontology")
    lines = []
    w = lines.append

    w("# Extraction cost: Markdown vs JSON")
    w("")
    w(f"- Model: `{pricing.model}`")
    w(f"- Pricing: **${pricing.usd_per_m_in:.4f}/M in**, **${pricing.usd_per_m_out:.4f}/M out** — {pricing.source}")
    w(f"- Token counting: {tc.method}")
    w(f"- Page images: {PAGE_W_PX}x{PAGE_H_PX}px at {PAGE_DPI} DPI → "
      f"{image_tokens(PAGE_W_PX, PAGE_H_PX, args.tile_px, args.tokens_per_tile):,} tokens/page "
      f"({args.tile_px}px tiles @ {args.tokens_per_tile} tok)")
    w(f"- Batching: {args.page_batch_size} pages/call → {baseline.calls_per_chapter} call(s) per chapter")
    w(f"- JSON parse-failure rate: {args.json_fail_rate:.0%} · Markdown: {args.markdown_fail_rate:.0%}")
    w("")
    w(f"Chapter shape: {args.pages_per_chapter} pages, {args.topics_per_chapter} topics, "
      f"{args.subtopics_per_topic} subtopics/topic, {args.exercises_per_topic} exercises/topic, "
      f"{args.sidebars_per_topic} sidebar(s)/topic.")
    w(f"Scale: {args.chapters} chapters/book × {args.books} book(s), "
      f"{args.re_extractions} re-extraction(s).")
    w("")

    w("## Per-chapter token breakdown")
    w("")
    w("| Format | Prompt | Images | Output | Total in | Total out | vs JSON out |")
    w("|---|---:|---:|---:|---:|---:|---:|")
    for r in results:
        ratio = r.output_tokens_per_chapter / baseline.output_tokens_per_chapter
        w(f"| `{r.fmt}` | {r.prompt_tokens:,} | {r.image_tokens:,} | "
          f"{r.output_tokens_per_chapter:,} | {r.tokens_in_per_chapter:,} | "
          f"{r.tokens_out_per_chapter:,.0f} | {ratio:.2f}× |")
    w("")

    w("## Cost")
    w("")
    w("| Format | Per chapter | Per book | Total | Saving vs JSON |")
    w("|---|---:|---:|---:|---:|")
    for r in results:
        saving = baseline.cost_total - r.cost_total
        pct = (saving / baseline.cost_total * 100) if baseline.cost_total else 0
        label = "— (baseline)" if r.fmt == "json_ontology" else f"{fmt_usd(saving)} ({pct:.1f}%)"
        w(f"| `{r.fmt}` | {fmt_usd(r.cost_per_chapter)} | {fmt_usd(r.cost_per_book)} | "
          f"{fmt_usd(r.cost_total)} | {label} |")
    w("")

    w("## Where the money actually goes")
    w("")
    img_share = baseline.image_tokens / max(1, baseline.tokens_in_per_chapter)
    in_cost = baseline.tokens_in_per_chapter / 1e6 * pricing.usd_per_m_in
    out_cost = baseline.tokens_out_per_chapter / 1e6 * pricing.usd_per_m_out
    total = in_cost + out_cost
    w(f"- Page images are {img_share:.0%} of input tokens, and input is "
      f"{in_cost / total:.0%} of the per-chapter cost. That part is **identical for every format** — "
      "the same pixels get sent either way.")
    w(f"- Output is {out_cost / total:.0%} of the cost, and it is the only part the format changes.")
    w(f"- So the ceiling on any format change is roughly {out_cost / total:.0%} of extraction spend. "
      "A 2× smaller output does not halve the bill.")
    w("")
    w("Output-token overhead of the JSON ontology, by cause:")
    w("")
    ch = build_chapter(1, args.topics_per_chapter, args.subtopics_per_topic,
                       args.exercises_per_topic, args.sidebars_per_topic, args.pages_per_chapter)
    full = json.loads(to_json_ontology(ch))
    graphs_only = json.dumps({"graphs": full["graphs"]}, separators=(",", ":"))
    ids = re.findall(r'"(?:id|chapter_id|topic_id|from|to)":"[^"]*"', to_json_ontology(ch))
    w(f"- `graphs` section: {tc.count(graphs_only):,} tokens — "
      "fully derivable from the `chapter_id`/`topic_id` fields already present in `entities`.")
    w(f"- id and foreign-key fields: {tc.count(''.join(ids)):,} tokens — "
      "replaced by heading depth in Markdown.")
    w("- Quoted keys repeated per item, plus braces, brackets and commas.")
    w("")

    # ── Is the comparison actually apples-to-apples? ──────────────────────────
    fid = check_fidelity(args)
    w("## Fidelity check — do both formats carry the same information?")
    w("")
    if not fid.get("checked"):
        w(f"Not checked: {fid.get('reason')}")
    else:
        verdict = "LOSSLESS" if fid["entities_lossless"] and fid["prerequisites_lossless"] else "LOSSY"
        w(f"JSON ontology → Markdown → parser round trip: **{verdict}**")
        w("")
        w("| Entity | In JSON | After Markdown round trip |")
        w("|---|---:|---:|")
        for key, before in fid["entity_counts_before"].items():
            w(f"| {key} | {before} | {fid['entity_counts_after'][key]} |")
        w(f"| prerequisite edges | {fid['prerequisite_edges_before']} | "
          f"{fid['prerequisite_edges_after']} |")
        w("")
        if fid["parser_warnings"]:
            w("Parser warnings on the round trip:")
            for warning in fid["parser_warnings"]:
                w(f"- {warning}")
        else:
            w("Parser reported no warnings. The cost figures below compare equal payloads.")
    w("")

    # ── Cost of a later schema change ─────────────────────────────────────────
    enrich = price_reenrichment(args, pricing, tc)
    w("## Adding a field later")
    w("")
    w("| Approach | Tokens in | Tokens out | Per chapter | Per book |")
    w("|---|---:|---:|---:|---:|")
    w(f"| Full re-extract from the PDF | {enrich['full_tokens_in']:,} | "
      f"{enrich['full_tokens_out']:,} | "
      f"{fmt_usd(enrich['full_reextract_usd_per_chapter'])} | "
      f"{fmt_usd(enrich['full_reextract_usd_per_chapter'] * args.chapters)} |")
    w(f"| Re-enrich stored Markdown | {enrich['from_markdown_tokens_in']:,} | "
      f"{enrich['from_markdown_tokens_out']:,} | "
      f"{fmt_usd(enrich['from_markdown_usd_per_chapter'])} | "
      f"{fmt_usd(enrich['from_markdown_usd_per_chapter'] * args.chapters)} |")
    w("")
    w(f"Re-enriching from Markdown is **{enrich['saving_pct']:.0f}% cheaper** and avoids "
      f"{enrich['image_tokens_avoided']:,} image tokens per chapter.")
    w("")
    w("The stronger argument isn't the money. A full re-extract re-reads the pixels, so "
      "transcription can come back different — syllabus text an admin already reviewed and "
      "approved can change underneath them. Re-enriching stored Markdown cannot do that, "
      "because nothing re-reads the page.")
    w("")

    w("## Reading this")
    w("")
    md = next(r for r in results if r.fmt == "markdown")
    mm = next(r for r in results if r.fmt == "markdown_meta")
    w(f"- `markdown` is the cheapest at {fmt_usd(md.cost_total)}, but drops "
      "`skill_type`/`exercise_type`/`prerequisites` into prose, so anything downstream that "
      "needs them must re-derive them — a second AI pass whose cost is not counted here.")
    w(f"- `markdown_meta` costs {fmt_usd(mm.cost_total)} "
      f"({(1 - mm.cost_total / baseline.cost_total) * 100:.0f}% under JSON) and keeps every "
      "machine-readable field, which is why it's the sensible canonical format.")
    w("- The JSON figure above is generous to JSON: it assumes compact separators. Models "
      "asked for JSON routinely pretty-print, which inflates it further.")
    w("")
    w("Not modelled: the `_chapter_prompt` → `_simplified_prompt` → `_minimal_prompt` "
      "degradation ladder. Each fallback is another full call with the same images, and it only "
      "triggers for JSON. Raising `--json-fail-rate` is the crude way to account for it; "
      "`--mode live` measures the real rate.")

    return "\n".join(lines)


def render_live_report(args, pricing: Pricing, results: list) -> str:
    lines = []
    w = lines.append
    w("# Extraction cost: Markdown vs JSON (live)")
    w("")
    w(f"- Model: `{args.model}`")
    w(f"- PDF: `{Path(args.pdf).name}`, pages {args.pages}, rendered at {PAGE_DPI} DPI")
    w(f"- Pricing: {pricing.source}")
    w("")
    if not results:
        w("No successful calls — nothing to compare.")
        return "\n".join(lines)

    baseline = next((r for r in results if r["format"] == "json_ontology"), results[0])
    w("| Format | Tokens in | Tokens out | Output chars | Cost | vs JSON out | Parsed |")
    w("|---|---:|---:|---:|---:|---:|---|")
    for r in results:
        ratio = r["tokens_out"] / max(1, baseline["tokens_out"])
        w(f"| `{r['format']}` | {r['tokens_in']:,} | {r['tokens_out']:,} | "
          f"{r['output_chars']:,} | {fmt_usd(r['cost_usd'])} | {ratio:.2f}× | "
          f"{'yes' if r['parsed_ok'] else '**NO**'} |")
    w("")
    w("Per-batch costs above. Multiply by batches per chapter × chapters × books for a "
      "deployment figure, or use `--mode estimate` which does that arithmetic.")
    w("")
    for r in results:
        w(f"## {r['format']} — first 1200 chars")
        w("")
        w("```")
        w(r["sample"][:1200])
        w("```")
        w("")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(
        description="Compare extraction cost: Markdown vs JSON ontology.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__ or ""),
    )
    p.add_argument("--mode", choices=["estimate", "live"], default="estimate")
    p.add_argument("--model", default=DEFAULT_MODEL)

    # Chapter / book shape
    p.add_argument("--pages-per-chapter", type=int, default=12)
    p.add_argument("--topics-per-chapter", type=int, default=5)
    p.add_argument("--subtopics-per-topic", type=int, default=3)
    p.add_argument("--exercises-per-topic", type=int, default=4)
    p.add_argument("--sidebars-per-topic", type=int, default=1)
    p.add_argument("--chapters", type=int, default=14, help="chapters per book")
    p.add_argument("--books", type=int, default=1)
    p.add_argument("--re-extractions", type=int, default=0,
                   help="extra full re-runs to budget for (schema changes, prompt fixes)")

    # Pipeline knobs
    p.add_argument("--page-batch-size", type=int, default=PAGE_BATCH_SIZE)
    p.add_argument("--tile-px", type=int, default=TILE_PX)
    p.add_argument("--tokens-per-tile", type=int, default=TOKENS_PER_TILE)
    p.add_argument("--json-fail-rate", type=float, default=0.12,
                   help="share of JSON calls needing a retry/repair (default 0.12)")
    p.add_argument("--markdown-fail-rate", type=float, default=0.0)

    # Pricing
    p.add_argument("--price-in", type=float, default=None, help="USD per 1M input tokens")
    p.add_argument("--price-out", type=float, default=None, help="USD per 1M output tokens")

    # Live mode
    p.add_argument("--pdf", help="PDF to extract from (live mode)")
    p.add_argument("--pages", default="1-7", help="page range for live mode, e.g. 8-14")
    p.add_argument("--language", default="English")
    p.add_argument("--max-output-tokens", type=int, default=16384)
    p.add_argument("--delay", type=float, default=4.0, help="seconds between live calls")

    p.add_argument("--out-dir", default=str(_HERE / "extraction_cost_output"))
    p.add_argument("--no-write", action="store_true", help="print only, write no files")
    args = p.parse_args()

    if args.mode == "live" and not args.pdf:
        raise SystemExit("--mode live requires --pdf")

    load_env()
    tc = TokenCounter()
    pricing = fetch_pricing(args.model, args.price_in, args.price_out)
    if pricing.source.startswith("FALLBACK"):
        print(f"! Pricing: {pricing.source}\n", file=sys.stderr)

    if args.mode == "estimate":
        results = estimate(args, pricing, tc)
        report = render_estimate_report(args, pricing, tc, results)
        payload = {
            "mode": "estimate",
            "model": pricing.model,
            "pricing": asdict(pricing),
            "token_counting": tc.method,
            "params": vars(args),
            "results": [
                {k: v for k, v in asdict(r).items() if k != "sample"} for r in results
            ],
            "samples": {r.fmt: r.sample for r in results},
        }
    else:
        results = live(args, pricing, tc)
        report = render_live_report(args, pricing, results)
        payload = {
            "mode": "live",
            "model": args.model,
            "pricing": asdict(pricing),
            "params": vars(args),
            "results": results,
        }

    print(report)

    if not args.no_write:
        out_dir = Path(args.out_dir)
        out_dir.mkdir(parents=True, exist_ok=True)
        stem = f"cost_{args.mode}_{args.model.replace('/', '_').replace(':', '_')}"
        (out_dir / f"{stem}.md").write_text(report, encoding="utf-8")
        (out_dir / f"{stem}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
        )
        print(f"\nWritten: {out_dir / (stem + '.md')}")
        print(f"         {out_dir / (stem + '.json')}")


if __name__ == "__main__":
    main()
