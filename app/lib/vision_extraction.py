"""Vision-based ontology extraction for textbooks (incl. non-Latin scripts).

Adapted from the standalone BEASTMODE extractor to run inside the FastAPI
backend as a background job:
  - API key/model read from the same env the rest of the backend uses; the
    key is validated lazily (at first call) so importing this module never
    crashes server startup.
  - generate_ontology_vision() accepts an optional progress_cb(pct, message)
    so the job manager can stream progress to the admin UI.
  - Checkpoint/resume, adaptive page batching, auto language detection,
    3-level progressive retry, cross-chapter dependency inference, and strict
    validation are all preserved.

Output shape matches the original: ontology["entities"] holds chapters /
topics / subtopics / exercises / sidebars, plus a legacy ontology["chapters"].
"""

import os
import io
import json
import time
import base64
import threading
import re as _re
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Optional

import requests
import fitz  # PyMuPDF
import PIL.Image

try:
    from dotenv import load_dotenv
    # backend/.env — two levels up from app/lib/
    load_dotenv(Path(__file__).resolve().parent.parent.parent / ".env")
except ImportError:
    pass

OPENROUTER_MODEL = os.environ.get("OPENROUTER_MODEL", "google/gemini-2.5-flash")
_OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"

PAGE_DPI = 150            # render DPI — 150 balances quality vs cost
PAGE_BATCH_SIZE = 7       # max pages per OpenRouter call; larger chapters are batched
MAX_OUTPUT_TOKENS = 65536  # allow full extraction without truncation
INTER_CALL_DELAY = 4      # seconds between successive API calls
CHAPTER_WORKERS = 3       # chapters processed in parallel

ProgressCb = Optional[Callable[[int, str], None]]


def _api_key() -> str:
    key = os.environ.get("OPENROUTER_API_KEY")
    if not key:
        raise EnvironmentError("OPENROUTER_API_KEY is not set on the backend.")
    return key


# ── Rendering ────────────────────────────────────────────────────────────────

def render_page(doc, page_num: int, dpi: int = PAGE_DPI) -> PIL.Image.Image:
    page = doc.load_page(page_num)
    mat = fitz.Matrix(dpi / 72, dpi / 72)
    pix = page.get_pixmap(matrix=mat)
    return PIL.Image.frombytes("RGB", [pix.width, pix.height], pix.samples)


# ── JSON Repair ────────────────────────────────────────────────────────────

def _sanitize_escapes(text: str) -> str:
    return _re.sub(r'\\(?!["\\/bfnrtu])', r'\\\\', text)


def robust_json_parse(text: str):
    text = text.strip()
    for prefix in ("```json", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    if text.endswith("```"):
        text = text[:-3]
    text = text.strip()

    def try_parse(t):
        try:
            return json.loads(t, strict=False)
        except Exception:
            return None

    result = try_parse(text)
    if result is not None:
        return result

    sanitized = _sanitize_escapes(text)
    result = try_parse(sanitized)
    if result is not None:
        return result
    text = sanitized

    if not text.endswith(('}', ']', '"')):
        tmp = text
        if tmp.count('"') % 2 != 0:
            tmp += '"'
        tmp += '}' * max(0, tmp.count('{') - tmp.count('}'))
        tmp += ']' * max(0, tmp.count('[') - tmp.count(']'))
        result = try_parse(tmp)
        if result is not None:
            return result

    for i in range(len(text) - 1, 0, -1):
        if text[i] in ('}', ']'):
            sub = text[:i + 1]
            for suffix in ('', ']', '}', ']}', ']]}', '}}', ']}}'):
                result = try_parse(sub + suffix)
                if result is not None:
                    print(f"  [JSON] Repaired by backtracking to index {i}")
                    return result

    return json.loads(text, strict=False)


# ── OpenRouter Call ──────────────────────────────────────────────────────────

def _pil_to_b64_url(img: PIL.Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=85)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()


def call_gemini(contents: list, max_retries: int = 6, base_delay: int = 5) -> str:
    """Send a list of PIL images and/or strings to OpenRouter and return the text response."""
    parts = []
    for item in contents:
        if isinstance(item, str):
            parts.append({"type": "text", "text": item})
        else:
            parts.append({"type": "image_url", "image_url": {"url": _pil_to_b64_url(item)}})

    messages = [
        {"role": "system", "content": "You must respond with valid JSON only. No markdown fences, no preamble."},
        {"role": "user", "content": parts if len(parts) > 1 or any(p["type"] == "image_url" for p in parts) else parts[0]["text"]},
    ]

    payload = {
        "model": OPENROUTER_MODEL,
        "messages": messages,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "temperature": 0.15,
    }

    for attempt in range(max_retries):
        try:
            resp = requests.post(
                _OPENROUTER_URL,
                headers={"Authorization": f"Bearer {_api_key()}", "Content-Type": "application/json"},
                json=payload,
                timeout=180,
            )
            resp.raise_for_status()
            return resp.json()["choices"][0]["message"]["content"]
        except Exception as e:
            err = str(e)
            if ("429" in err or "quota" in err.lower() or "resource_exhausted" in err.lower()) and attempt < max_retries - 1:
                delay = base_delay * (2 ** attempt)
                print(f"  [RETRY] Rate limited. Waiting {delay}s (attempt {attempt+1}/{max_retries})...")
                time.sleep(delay)
            else:
                raise


# ── Language Detection ─────────────────────────────────────────────────────

_LANGUAGE_DETECT_PROMPT = """
Look at these opening pages of a textbook.
Identify the primary non-English language used for instruction content (headings, body text, exercises).

Examples: "Hindi", "Telugu", "Tamil", "Kannada", "Marathi", "Malayalam", "Gujarati", "Punjabi", "English"

Return ONLY valid JSON with no markdown:
{"language": "Telugu"}
"""


def detect_language_vision(pdf_path: str) -> str:
    """Auto-detect the textbook language from the first few pages."""
    doc = fitz.open(pdf_path)
    images = [render_page(doc, i) for i in range(min(4, len(doc)))]
    try:
        raw = call_gemini([_LANGUAGE_DETECT_PROMPT] + images)
        data = robust_json_parse(raw)
        lang = data.get("language", "Hindi")
        print(f"[VISION] Auto-detected language: {lang}")
        return lang
    except Exception as e:
        print(f"[VISION] Language detection failed ({e}), defaulting to Hindi")
        return "Hindi"


# ── TOC Detection ────────────────────────────────────────────────────────────

TOC_PROMPT = """
You are analyzing the opening pages of a Grade 1 textbook.
The language may be Hindi, Telugu, or another Indian language.

Look carefully at ALL images for a Table of Contents or index page listing
chapter/lesson titles with their page numbers.

CRITICAL RULES:
- Transcribe ALL titles exactly as they appear — keep the original script intact, do NOT translate
- Use ONLY the page numbers printed in the book (Arabic numerals only)
- List chapters in the order they appear in the TOC (ascending page numbers)

SKIP ALL front matter — do NOT list any of these as chapters:
  - Cover page
  - Copyright / publisher page
  - Preface / foreword / introduction
  - National Anthem
  - National Pledge
  - "Dear Teacher" / "Dear Student" / "Dear Parents" messages
  - Table of Contents page itself
  - Blank pages
  - Pages with only Roman numerals (i, ii, iii, iv...)
  - Any page without actual lesson/teaching content for students

Return ONLY valid JSON (no markdown, no explanation):
{
  "chapters": [
    {"title": "exact title in original script", "book_page": 1}
  ]
}

If no explicit TOC is visible, infer chapter boundaries ONLY from visible lesson/unit
headings — never from front-matter headings.
"""


_CONTENT_START_PROMPT = """You are analyzing the opening pages of an Indian Grade 1 school textbook.

Identify the boundary between front matter and actual lessons.

FRONT MATTER includes ALL of the following — do NOT count these as lessons:
  - Cover / title page
  - Publisher / copyright page
  - Preface, foreword, introduction, "About this book"
  - National Anthem, National Song, National Pledge
  - "Dear Teacher", "Dear Student", "Dear Parents" messages
  - Table of Contents / Index page
  - Blank pages
  - Any page printed with Roman numerals only (i, ii, iii, iv, v...)

FIRST LESSON: The first page that has a lesson title in the book language PLUS at least one
of: student vocabulary words, activity instructions, illustrations with labels, exercises,
or fill-in-the-blank questions. A page that only shows the National Anthem text is NOT
a lesson.

The first image here is PDF page index 0 (0-based counting).

Return ONLY valid JSON:
{
  "front_matter_pdf_indices": [0, 1, 2, 3, 4, 5, 6, 7],
  "first_lesson_pdf_index": 8,
  "first_lesson_printed_page": 1
}
"""


def detect_content_start(doc) -> dict:
    """
    Ask the model to identify front matter pages and the first true lesson page.
    Returns offset metadata used to align PDF indices with printed page numbers.
    """
    n = min(20, len(doc))
    images = [render_page(doc, i) for i in range(n)]

    try:
        time.sleep(INTER_CALL_DELAY)
        raw = call_gemini([_CONTENT_START_PROMPT] + images)
        data = robust_json_parse(raw)

        first_idx = data.get("first_lesson_pdf_index", 0)
        printed_page = data.get("first_lesson_printed_page", 1)
        front_pages = set(data.get("front_matter_pdf_indices", []))

        # pdf_start = book_page + offset - 1  ->  offset = first_idx - printed_page + 1
        offset = first_idx - printed_page + 1

        print(f"[VISION] Front matter: PDF indices {sorted(front_pages)}")
        print(f"[VISION] First lesson: PDF index {first_idx}, printed page {printed_page}, offset={offset}")
        return {
            "front_matter_indices": front_pages,
            "first_lesson_index": first_idx,
            "first_printed_page": printed_page,
            "offset": offset,
        }
    except Exception as exc:
        print(f"[VISION] Content-start detection failed: {exc}. Falling back to 0.")
        return {"front_matter_indices": set(), "first_lesson_index": 0,
                "first_printed_page": 1, "offset": 0}


def _calibrate_offset_vision(doc, chapters_raw: list) -> int:
    """
    Return the offset between PDF page index (0-based) and printed book page number.
    Tries text extraction first (fast, works for Latin scripts).
    Falls back to vision for Devanagari / Telugu / other non-Latin textbooks.
    """
    # Text-based pass
    for pdf_i in range(min(len(doc), 30)):
        page_text = doc[pdf_i].get_text().lower()
        for chap in chapters_raw[:5]:
            ascii_words = [
                w for w in chap["title"].lower().split()
                if all(ord(c) < 128 for c in w) and len(w) > 3
            ]
            if ascii_words and any(w in page_text for w in ascii_words):
                offset = (pdf_i + 1) - chap["book_page"]
                print(f"[VISION] TOC offset calibrated (text): {offset}")
                return offset

    # Vision-based fallback: scan pages 4-20 and ask for printed page numbers
    scan_start = min(4, len(doc) - 1)
    scan_end = min(20, len(doc))
    images = [render_page(doc, i) for i in range(scan_start, scan_end)]
    if not images:
        return 0

    prompt = (
        f"Look at these textbook pages carefully.\n"
        f"For each page, find the PRINTED page number (Arabic numeral in header, footer, or corner).\n"
        f"Ignore Roman numerals on front-matter pages.\n"
        f"The first image here corresponds to PDF page index {scan_start} (0-based).\n\n"
        f"Return ONLY valid JSON:\n"
        f'{{"page_numbers": [{{"pdf_index": {scan_start}, "printed": 1}}]}}\n\n'
        f"Include only pages where you can clearly read an Arabic numeral."
    )
    try:
        time.sleep(INTER_CALL_DELAY)
        raw = call_gemini([prompt] + images)
        entries = robust_json_parse(raw).get("page_numbers", [])
        if entries:
            e = entries[0]
            pdf_idx = e.get("pdf_index", scan_start)
            printed = e.get("printed", 1)
            offset = pdf_idx - printed + 1
            print(f"[VISION] TOC offset calibrated (vision): {offset} "
                  f"(printed page {printed} is at PDF index {pdf_idx})")
            return offset
    except Exception as exc:
        print(f"[VISION] Vision offset calibration failed: {exc}")

    print("[VISION] Could not calibrate offset, defaulting to 0.")
    return 0


def detect_chapters_vision(pdf_path: str) -> list:
    doc = fitz.open(pdf_path)

    # Step 1: Detect front matter boundary and page offset
    content_info = detect_content_start(doc)
    offset = content_info["offset"]
    first_lesson = content_info["first_lesson_index"]

    # Step 2: TOC detection from first 18 pages
    n = min(18, len(doc))
    print(f"[VISION] TOC detection: sending first {n} pages as images...")

    images = [render_page(doc, i) for i in range(n)]
    time.sleep(INTER_CALL_DELAY)
    raw = call_gemini([TOC_PROMPT] + images)

    try:
        toc = robust_json_parse(raw)
        chapters_raw = toc.get("chapters", [])
    except Exception as e:
        print(f"[ERROR] TOC parse failed: {e}")
        return []

    if not chapters_raw:
        return []

    # If content-start detection gave no offset, fall back to text/vision calibration
    if offset == 0 and first_lesson == 0:
        offset = _calibrate_offset_vision(doc, chapters_raw)

    final = []
    skipped = 0
    for chap in chapters_raw:
        pdf_start = chap["book_page"] + offset - 1
        if pdf_start < first_lesson:
            print(f"  [SKIP] Front-matter chapter '{chap['title']}' "
                  f"(book_page={chap['book_page']} -> PDF index {pdf_start} < first lesson {first_lesson})")
            skipped += 1
            continue
        if 0 <= pdf_start < len(doc):
            final.append({"title": chap["title"], "start_page": pdf_start})

    if skipped:
        print(f"[VISION] Skipped {skipped} front-matter chapter(s).")

    final.sort(key=lambda x: x["start_page"])
    for i in range(len(final) - 1):
        final[i]["end_page"] = final[i + 1]["start_page"] - 1
    if final:
        final[-1]["end_page"] = len(doc) - 1

    print(f"[VISION] Detected {len(final)} chapters after front-matter filtering.")
    return final


# ── Prompts ────────────────────────────────────────────────────────────────

def _chapter_prompt(
    chap_num: int,
    language: str,
    context: str,
    global_chapter_list: str,
    topic_start: int = 1,
    exercise_start: int = 1,
    subtopic_start: int = 1,
    prior_topics_summary: str = "",
) -> str:
    n = chap_num
    t = topic_start
    e = exercise_start
    st = subtopic_start

    prior_section = ""
    if prior_topics_summary:
        prior_section = f"""
ALREADY EXTRACTED from earlier pages of this same chapter (do NOT repeat these):
{prior_topics_summary}

Extract ONLY NEW content visible in the current page batch.
Start new topic IDs at T_{n}_{t}, exercise IDs at E_{n}_{t}_{e}.
"""

    return f"""
You are an expert educational architect analyzing Grade 1 textbook pages.
Language: {language}. Read ALL text accurately in its original script.

CONTEXT: {context}
{prior_section}
FULL BOOK CHAPTER LIST (use this to avoid cross-chapter content leakage):
{global_chapter_list}

Analyze EVERY visible page image and extract a COMPLETE ontology for the content shown.
Do NOT include content that belongs to other chapters listed above.

EXTRACTION RULES:
1. titles/names: transcribe exactly in original script (Hindi/Telugu/English as printed)
2. "summary" fields: always write in clear English
3. page_start / page_end: use the numbers PRINTED in the book images (book page numbers, not PDF indices)
4. Exercises: capture EVERY activity — fill-in-the-blank, writing practice, colouring,
   tracing, matching, drawing, circling, answering questions, reading aloud, singing.
   For image-only activities describe what the student must do from the visual cue.
5. Sidebars: tips, "Did you know?", learning objective boxes, QR codes, margin notes
6. prerequisites: use ONLY valid topic IDs (e.g. "T_2_1") — NEVER plain text strings
7. skill_type for each subtopic — choose from:
   reading_skill | writing_skill | recognition_skill | comprehension_skill |
   vocabulary_skill | listening_skill | counting_skill | art_skill | general_skill
8. exercise_type for each exercise — choose from:
   writing_practice | art_activity | matching_exercise | reading_exercise |
   comprehension | listening_activity | counting_activity | general_activity

ID SCHEMA (embed chapter number {n}):
  chapters  -> C_{n}
  topics    -> T_{n}_{t}, T_{n}_{t+1} ... (start at {t})
  subtopics -> ST_{n}_{t}_1, ST_{n}_{t}_2 ... (start at {st})
  exercises -> E_{n}_{t}_{e}, E_{n}_{t}_{e+1} ... (start at {e})
  sidebars  -> S_{n}_{t}_1 ...

Return ONLY strict JSON (no markdown fences, no explanation):
{{
  "entities": {{
    "chapters": [
      {{"id": "C_{n}", "number": {n}, "title": "...", "page_start": 0, "page_end": 0}}
    ],
    "topics": [
      {{
        "id": "T_{n}_{t}",
        "name": "...",
        "summary": "English description...",
        "chapter_id": "C_{n}",
        "page_start": 0,
        "page_end": 0,
        "prerequisites": [],
        "subtopics": [
          {{"id": "ST_{n}_{t}_1", "name": "...", "summary": "...", "skill_type": "reading_skill", "page_start": 0, "page_end": 0}}
        ]
      }}
    ],
    "exercises": [
      {{"id": "E_{n}_{t}_{e}", "text": "describe the activity or question...", "topic_id": "T_{n}_{t}", "page": 0, "exercise_type": "general_activity"}}
    ],
    "sidebars": [
      {{"id": "S_{n}_{t}_1", "text": "sidebar content...", "topic_id": "T_{n}_{t}", "page": 0}}
    ]
  }},
  "graphs": {{
    "chapter_structure": [{{"from": "C_{n}", "to": "T_{n}_{t}", "type": "contains"}}],
    "exercise_mapping": [{{"from": "E_{n}_{t}_{e}", "to": "T_{n}_{t}", "type": "tests"}}],
    "concept_dependencies": []
  }}
}}
"""


def _simplified_prompt(chap_num: int, language: str, context: str) -> str:
    n = chap_num
    return f"""
You are analyzing Grade 1 textbook pages. Language: {language}.
CONTEXT: {context}

This chapter may be short, mostly image-based, or have minimal text. That is fine.
Create AT LEAST ONE topic for any visible learning content — even a single activity page.
Describe image-based activities (colouring, tracing, matching) in the exercise "text" field.
Summarize ALL content in English.

IDs: C_{n}, T_{n}_1, ST_{n}_1_1, E_{n}_1_1, S_{n}_1_1

Return ONLY strict JSON:
{{
  "entities": {{
    "chapters": [{{"id": "C_{n}", "number": {n}, "title": "...", "page_start": 0, "page_end": 0}}],
    "topics": [{{
      "id": "T_{n}_1",
      "name": "...",
      "summary": "English summary of learning content...",
      "chapter_id": "C_{n}",
      "page_start": 0,
      "page_end": 0,
      "prerequisites": [],
      "subtopics": [
        {{"id": "ST_{n}_1_1", "name": "...", "summary": "...", "skill_type": "general_skill", "page_start": 0, "page_end": 0}}
      ]
    }}],
    "exercises": [{{"id": "E_{n}_1_1", "text": "describe the activity...", "topic_id": "T_{n}_1", "page": 0, "exercise_type": "general_activity"}}],
    "sidebars": []
  }},
  "graphs": {{
    "chapter_structure": [{{"from": "C_{n}", "to": "T_{n}_1", "type": "contains"}}],
    "exercise_mapping": [{{"from": "E_{n}_1_1", "to": "T_{n}_1", "type": "tests"}}],
    "concept_dependencies": []
  }}
}}
"""


def _minimal_prompt(chap_num: int, language: str, context: str) -> str:
    n = chap_num
    return f"""
Grade 1 textbook, language: {language}. Context: {context}
Create a MINIMAL valid ontology for whatever you see. One topic is enough.
Return JSON only:
{{"entities":{{"chapters":[{{"id":"C_{n}","number":{n},"title":"Chapter {n}","page_start":1,"page_end":1}}],"topics":[{{"id":"T_{n}_1","name":"Content","summary":"Chapter {n} content","chapter_id":"C_{n}","page_start":1,"page_end":1,"prerequisites":[],"subtopics":[]}}],"exercises":[],"sidebars":[]}},"graphs":{{"chapter_structure":[{{"from":"C_{n}","to":"T_{n}_1","type":"contains"}}],"exercise_mapping":[],"concept_dependencies":[]}}}}
"""


# ── Extraction ───────────────────────────────────────────────────────────────

def _count_topics_for_chapter(data: dict, chap_id: str) -> int:
    return sum(1 for t in data["entities"].get("topics", []) if t.get("chapter_id") == chap_id)


def _count_exercises(data: dict) -> int:
    return len(data["entities"].get("exercises", []))


def _count_subtopics(data: dict) -> int:
    return len(data["entities"].get("subtopics", []))


def _summarize_topics(data: dict, chap_id: str) -> str:
    lines = []
    for t in data["entities"].get("topics", []):
        if t.get("chapter_id") == chap_id:
            lines.append(f"  - {t['id']}: {t.get('name', '?')} (pages {t.get('page_start')}-{t.get('page_end')})")
    return "\n".join(lines) if lines else ""


def extract_chapter_vision(
    doc,
    pages: list,
    chap_num: int,
    chap_title: str,
    language: str,
    global_chapter_list: str,
    topic_start: int = 1,
    exercise_start: int = 1,
    subtopic_start: int = 1,
    prior_topics_summary: str = "",
) -> dict:
    """Extract ontology from a single batch of pages for one chapter."""
    images = [render_page(doc, p) for p in pages if p < len(doc)]
    context = (
        f"Chapter {chap_num}: '{chap_title}' | "
        f"PDF pages {pages[0]+1}-{pages[-1]+1}"
    )
    prompt = _chapter_prompt(
        chap_num, language, context, global_chapter_list,
        topic_start=topic_start,
        exercise_start=exercise_start,
        subtopic_start=subtopic_start,
        prior_topics_summary=prior_topics_summary,
    )
    raw = call_gemini([prompt] + images)
    return robust_json_parse(raw)


def extract_chapter_batched(
    doc,
    pages: list,
    chap_num: int,
    chap_title: str,
    language: str,
    global_chapter_list: str,
) -> dict:
    """
    Extract a chapter's ontology, splitting large chapters into page batches.
    Subsequent batches receive a summary of previously extracted topics so the
    model continues numbering correctly and avoids repeating content.
    """
    chap_id = f"C_{chap_num}"

    if len(pages) <= PAGE_BATCH_SIZE:
        return extract_chapter_vision(
            doc, pages, chap_num, chap_title, language, global_chapter_list
        )

    batches = [pages[i:i + PAGE_BATCH_SIZE] for i in range(0, len(pages), PAGE_BATCH_SIZE)]
    print(f"  [BATCH] {len(pages)} pages -> {len(batches)} batches of <={PAGE_BATCH_SIZE}")

    merged: dict = {
        "entities": {"chapters": [], "topics": [], "subtopics": [], "exercises": [], "sidebars": []},
        "graphs": {"chapter_structure": [], "exercise_mapping": [], "concept_dependencies": []},
    }

    for b_idx, batch_pages in enumerate(batches):
        print(f"  [BATCH] {b_idx+1}/{len(batches)} — pages {batch_pages[0]+1}-{batch_pages[-1]+1}")

        topic_start = _count_topics_for_chapter(merged, chap_id) + 1
        exercise_start = _count_exercises(merged) + 1
        subtopic_start = _count_subtopics(merged) + 1
        prior_summary = _summarize_topics(merged, chap_id) if b_idx > 0 else ""

        try:
            batch_data = extract_chapter_vision(
                doc, batch_pages, chap_num, chap_title, language, global_chapter_list,
                topic_start=topic_start,
                exercise_start=exercise_start,
                subtopic_start=subtopic_start,
                prior_topics_summary=prior_summary,
            )
            _merge(merged, batch_data)
        except Exception as e:
            print(f"  [BATCH ERROR] Batch {b_idx+1} failed: {e}")

        if b_idx < len(batches) - 1:
            time.sleep(INTER_CALL_DELAY)

    return merged


# ── Merge ────────────────────────────────────────────────────────────────────

def _merge(full: dict, chunk: dict):
    """Merge one chapter's extracted data into the running full ontology."""
    chunk_entities = chunk.get("entities", {})

    # Pre-build name+chapter dedup index for topics to catch same-content different-ID duplicates
    topic_name_keys = {
        (t.get("chapter_id", ""), t.get("name", "").strip().lower())
        for t in full["entities"]["topics"]
    }

    for key in ("chapters", "topics", "exercises", "sidebars"):
        seen = {e["id"] for e in full["entities"][key]}
        for entity in chunk_entities.get(key, []):
            eid = entity.get("id")
            if not eid or eid in seen:
                continue
            if key == "topics":
                name_key = (entity.get("chapter_id", ""), entity.get("name", "").strip().lower())
                if name_key in topic_name_keys:
                    continue  # same name, same chapter -> skip duplicate
                inline = entity.pop("subtopics", [])
                st_seen = {e["id"] for e in full["entities"]["subtopics"]}
                for st in inline:
                    st["topic_id"] = entity["id"]
                    if st.get("id") and st["id"] not in st_seen:
                        full["entities"]["subtopics"].append(st)
                        st_seen.add(st["id"])
                topic_name_keys.add(name_key)
            full["entities"][key].append(entity)
            seen.add(eid)

    st_seen = {e["id"] for e in full["entities"]["subtopics"]}
    for st in chunk_entities.get("subtopics", []):
        if st.get("id") and st["id"] not in st_seen:
            full["entities"]["subtopics"].append(st)
            st_seen.add(st["id"])

    for gkey in ("chapter_structure", "exercise_mapping", "concept_dependencies"):
        seen_edges = {(e["from"], e["to"], e.get("type")) for e in full["graphs"][gkey]}
        for edge in chunk.get("graphs", {}).get(gkey, []):
            t = (edge.get("from"), edge.get("to"), edge.get("type"))
            if t not in seen_edges:
                full["graphs"][gkey].append(edge)
                seen_edges.add(t)


# ── Cross-Chapter Dependency Inference ───────────────────────────────────────

_CROSS_DEP_PROMPT = """
You are a curriculum expert analyzing a Grade 1 textbook ontology.

Below are all topics extracted from the textbook, organized by chapter.
Identify MEANINGFUL SEMANTIC PREREQUISITES across chapters — cases where a student
genuinely needs to have mastered a concept in one chapter before engaging with a
concept in a later chapter.

Do NOT create purely sequential links (chapter N -> chapter N+1) unless there is a
clear content reason. Focus on actual concept dependencies:
  - If chapter 5 introduces a vowel and chapter 8 uses words with it, chapter 5 is a prerequisite
  - If chapter 3 teaches basic letter recognition and chapter 12 uses those letters, chapter 3 topics are prerequisites
  - Skill progression: recognition -> reading -> writing -> sentence formation

TOPICS BY CHAPTER:
{topics_summary}

Return ONLY valid JSON (no markdown):
{{
  "cross_chapter_deps": [
    {{"from": "T_8_2", "to": "T_5_1", "type": "depends_on"}}
  ]
}}

Rules:
- "from" is the DEPENDENT topic (appears LATER in the book)
- "to" is the PREREQUISITE topic (must be learned FIRST)
- Only include dependencies where there is a clear content connection
- Both "from" and "to" must be topic IDs that actually exist in the list above
- Limit to the 30 most important cross-chapter dependencies
"""


def _infer_cross_chapter_deps(ontology: dict) -> list:
    """Run a single AI call to infer semantic cross-chapter prerequisites."""
    topics = ontology["entities"]["topics"]
    if len(topics) < 4:
        return []

    by_chapter: dict = {}
    for t in topics:
        cid = t.get("chapter_id", "?")
        by_chapter.setdefault(cid, []).append(
            f"    {t['id']}: {t.get('name', '?')} — {t.get('summary', '')[:120]}"
        )

    lines = []
    for cid in sorted(by_chapter, key=lambda x: int(x.split("_")[-1]) if x.split("_")[-1].isdigit() else 999):
        lines.append(f"  Chapter {cid}:")
        lines.extend(by_chapter[cid])

    summary = "\n".join(lines)

    print("[AI] Inferring cross-chapter semantic dependencies...")
    try:
        raw = call_gemini([_CROSS_DEP_PROMPT.format(topics_summary=summary)])
        data = robust_json_parse(raw)
        deps = data.get("cross_chapter_deps", [])
        print(f"[AI] Found {len(deps)} cross-chapter dependencies.")
        return deps
    except Exception as e:
        print(f"[WARNING] Cross-chapter dep inference failed: {e}")
        return []


# ── Checkpoint ───────────────────────────────────────────────────────────────

def _load_checkpoint(job_dir: Path) -> set:
    cp = job_dir / "checkpoint.json"
    if cp.exists():
        try:
            data = json.loads(cp.read_text(encoding="utf-8"))
            done = set(data.get("done", []))
            if done:
                print(f"[CHECKPOINT] Resuming — {len(done)} chapter(s) already done: {sorted(done)}")
            return done
        except Exception:
            pass
    return set()


def _save_checkpoint(job_dir: Path, done: set):
    cp = job_dir / "checkpoint.json"
    cp.write_text(json.dumps({"done": sorted(done)}), encoding="utf-8")


# ── Classifiers ──────────────────────────────────────────────────────────────

_EXERCISE_TYPES = [
    ("writing_practice",   ["trace", "tracing", "write", "writing", "copy", "fill in", "fill the", "form the"]),
    ("art_activity",       ["colour", "color", "draw", "circle", "underline", "tick", "mark", "highlight"]),
    ("matching_exercise",  ["match", "connect", "join", "pair"]),
    ("reading_exercise",   ["read", "reading", "say aloud", "recite", "repeat", "fluency"]),
    ("comprehension",      ["what", "who", "where", "when", "how many", "which", "why", "answer", "tell", "describe"]),
    ("listening_activity", ["listen", "hear", "sing", "song", "rhyme"]),
    ("counting_activity",  ["count", "number", "how many"]),
]

_SKILL_TYPES = [
    ("reading_skill",       ["read", "fluency", "aloud", "recite", "poem", "text", "rapid"]),
    ("writing_skill",       ["write", "writing", "trace", "tracing", "copy", "letter formation", "form"]),
    ("recognition_skill",   ["recognize", "identify", "match", "find", "circle", "underline", "tick", "identical"]),
    ("comprehension_skill", ["understand", "meaning", "answer", "question", "discuss", "explain", "comprehension"]),
    ("vocabulary_skill",    ["word", "vocabulary", "new words", "formation", "sentence", "matra"]),
    ("listening_skill",     ["listen", "hear", "sing", "song", "rhyme", "poem"]),
    ("counting_skill",      ["count", "number", "numeral", "digit"]),
    ("art_skill",           ["draw", "colour", "color", "art", "creative"]),
]


def classify_exercise(text: str) -> str:
    t = text.lower()
    for etype, keywords in _EXERCISE_TYPES:
        if any(k in t for k in keywords):
            return etype
    return "general_activity"


def classify_skill(text: str) -> str:
    t = text.lower()
    for stype, keywords in _SKILL_TYPES:
        if any(k in t for k in keywords):
            return stype
    return "general_skill"


# ── Page Clamp ───────────────────────────────────────────────────────────────

def _clamp_pages(entity: dict, p_start: int, p_end: int) -> bool:
    """
    Clamp entity page range to [p_start, p_end].
    Returns True when the entity's page_start was completely outside the parent range
    (boundary violation -> entity belongs to a different chapter).
    """
    ps = entity.get("page_start") or 0
    pe = entity.get("page_end") or 0
    boundary_violation = False

    if ps > 0 and p_start > 0 and p_end < 9999:
        if ps > p_end:
            boundary_violation = True   # entity starts after parent ends
            entity["page_start"] = p_start
        elif ps < p_start:
            entity["page_start"] = p_start

    if pe > 0 and p_end < 9999 and pe > p_end:
        entity["page_end"] = p_end

    ps2 = entity.get("page_start") or 0
    pe2 = entity.get("page_end") or 0
    if ps2 and pe2 and ps2 > pe2:
        entity["page_end"] = ps2

    return boundary_violation


# ── Cycle Detection ──────────────────────────────────────────────────────────

def _break_prereq_cycles(topics: list):
    """
    Detect and break cycles in the prerequisite graph using iterative DFS.
    Back-edges (prerequisites that point forward or create loops) are removed.
    """
    prereq_map: dict = {t["id"]: set(t.get("prerequisites", [])) for t in topics}
    topic_set: set = {t["id"] for t in topics}
    cycles_broken = 0

    visited: set = set()
    stack: set = set()

    def dfs(tid: str):
        nonlocal cycles_broken
        if tid in stack:
            return True
        if tid in visited or tid not in topic_set:
            return False
        visited.add(tid)
        stack.add(tid)
        for prereq in list(prereq_map.get(tid, [])):
            if dfs(prereq):
                prereq_map[tid].discard(prereq)
                cycles_broken += 1
                print(f"  [VALIDATE] Cycle broken: removed {tid} -> {prereq}")
        stack.discard(tid)
        return False

    for t in topics:
        if t["id"] not in visited:
            dfs(t["id"])

    if cycles_broken:
        print(f"  [VALIDATE] Removed {cycles_broken} cyclic prerequisite(s).")
        for t in topics:
            t["prerequisites"] = sorted(prereq_map.get(t["id"], []))


# ── Validation ───────────────────────────────────────────────────────────────

def validate_and_fix(ontology: dict) -> dict:
    """Full structural validation and enrichment (see module docstring)."""
    entities = ontology["entities"]

    # 1 & 2: separate phantoms, sort valid chapters
    all_chapters = sorted(entities["chapters"], key=lambda c: c.get("page_start") or 9999)
    valid_chaps = [c for c in all_chapters if (c.get("page_start") or 0) > 0]
    phantom_chaps = [c for c in all_chapters if not (c.get("page_start") or 0) > 0]

    if phantom_chaps:
        print(f"  [VALIDATE] {len(phantom_chaps)} phantom chapter(s) excluded: "
              f"{[c['id'] for c in phantom_chaps]}")

    for c in valid_chaps:
        c["confidence"] = 1.0

    # 3: merge chapters that share the same page_start
    topic_count: dict = {}
    for t in entities.get("topics", []):
        cid = t.get("chapter_id", "")
        topic_count[cid] = topic_count.get(cid, 0) + 1

    chap_id_remap: dict = {}
    seen_starts: dict = {}
    deduped: list = []
    for c in valid_chaps:
        ps = c["page_start"]
        if ps in seen_starts:
            keeper = seen_starts[ps]
            if topic_count.get(c["id"], 0) > topic_count.get(keeper["id"], 0):
                chap_id_remap[keeper["id"]] = c["id"]
                seen_starts[ps] = c
                deduped[-1] = c
                print(f"  [VALIDATE] Merged same-start {keeper['id']} -> {c['id']} "
                      f"(page {ps}, topics: {topic_count.get(keeper['id'],0)} vs "
                      f"{topic_count.get(c['id'],0)})")
            else:
                chap_id_remap[c["id"]] = keeper["id"]
                print(f"  [VALIDATE] Dropped same-start {c['id']} -> {keeper['id']} "
                      f"(page {ps}, topics: {topic_count.get(c['id'],0)} vs "
                      f"{topic_count.get(keeper['id'],0)})")
        else:
            seen_starts[ps] = c
            deduped.append(c)

    valid_chaps = deduped

    # 4: merge consecutive same-title chapters into one spanning chapter
    _part_re = _re.compile(r'\s*\(Part \d+\)\s*$', _re.IGNORECASE)

    def _base_title(t: str) -> str:
        return _part_re.sub("", t).strip()

    for c in valid_chaps:
        c["title"] = _base_title(c.get("title", ""))

    merged: list = []
    i = 0
    while i < len(valid_chaps):
        c = valid_chaps[i]
        title = c.get("title", "").strip()
        j = i + 1
        while j < len(valid_chaps) and valid_chaps[j].get("title", "").strip() == title:
            j += 1
        if j > i + 1:
            group = valid_chaps[i:j]
            c["page_end"] = max(g.get("page_end") or g.get("page_start") or 0 for g in group)
            for other in group[1:]:
                chap_id_remap[other["id"]] = c["id"]
            print(f"  [VALIDATE] Merged {len(group)} consecutive '{title}' chapters: "
                  f"{[g['id'] for g in group]} -> {c['id']}")
        merged.append(c)
        i = j

    valid_chaps = merged

    if chap_id_remap:
        for t in entities.get("topics", []):
            t["chapter_id"] = chap_id_remap.get(t["chapter_id"], t["chapter_id"])

    # 5: assign monotonic page_end; enforce page_end >= page_start
    for i, c in enumerate(valid_chaps):
        c.pop("order", None)
        if i < len(valid_chaps) - 1:
            next_start = valid_chaps[i + 1]["page_start"]
            assigned_end = next_start - 1
            c["page_end"] = max(c["page_start"], assigned_end)
            if assigned_end < c["page_start"]:
                print(f"  [VALIDATE] {c['id']}: page_start={c['page_start']} same as next "
                      f"chapter — single-page boundary. confidence halved.")
                c["confidence"] = round(c.get("confidence", 1.0) * 0.5, 2)
        if (c.get("page_end") or 0) < (c.get("page_start") or 0):
            print(f"  [VALIDATE] {c['id']}: ASSERT page_end < page_start. Correcting.")
            c["page_end"] = c["page_start"]
            c["confidence"] = round(c.get("confidence", 1.0) * 0.5, 2)

    # 6: reassign chapter numbers from sorted position
    for i, c in enumerate(valid_chaps):
        c["number"] = i + 1

    # 8: deduplicate topics by name within each chapter
    seen_topic_keys: dict = {}
    topics_to_keep: list = []
    topic_id_remap: dict = {}

    for t in entities["topics"]:
        key = (t.get("chapter_id", ""), t.get("name", "").strip().lower())
        if key in seen_topic_keys:
            topic_id_remap[t["id"]] = seen_topic_keys[key]
        else:
            seen_topic_keys[key] = t["id"]
            topics_to_keep.append(t)

    if topic_id_remap:
        print(f"  [VALIDATE] Removed {len(topic_id_remap)} duplicate topic(s).")
        for st in entities.get("subtopics", []):
            st["topic_id"] = topic_id_remap.get(st.get("topic_id"), st.get("topic_id"))
        for ex in entities.get("exercises", []):
            ex["topic_id"] = topic_id_remap.get(ex.get("topic_id"), ex.get("topic_id"))
        for sb in entities.get("sidebars", []):
            sb["topic_id"] = topic_id_remap.get(sb.get("topic_id"), sb.get("topic_id"))
    entities["topics"] = topics_to_keep

    # 9: deduplicate exercises by text within each topic
    seen_ex_keys: dict = {}
    exercises_to_keep: list = []
    for ex in entities.get("exercises", []):
        key = (ex.get("topic_id", ""), ex.get("text", "").strip()[:80].lower())
        if key not in seen_ex_keys:
            seen_ex_keys[key] = True
            exercises_to_keep.append(ex)
    removed_ex = len(entities.get("exercises", [])) - len(exercises_to_keep)
    if removed_ex:
        print(f"  [VALIDATE] Removed {removed_ex} duplicate exercise(s).")
    entities["exercises"] = exercises_to_keep

    # 10: filter ghost topics (page_start == 0)
    ghost_topics = [t for t in entities["topics"] if (t.get("page_start") or 0) == 0]
    if ghost_topics:
        ghost_ids = {t["id"] for t in ghost_topics}
        print(f"  [VALIDATE] Removed {len(ghost_topics)} ghost topic(s) with page_start=0.")
        entities["topics"] = [t for t in entities["topics"] if t["id"] not in ghost_ids]

    entities["chapters"] = valid_chaps
    ontology["unresolved_chapters"] = phantom_chaps

    # 11: clip topics / subtopics; flag boundary violations
    chap_ranges: dict = {
        c["id"]: (c.get("page_start") or 0, c.get("page_end") or 9999)
        for c in entities["chapters"]
    }
    valid_topic_ids: set = {t["id"] for t in entities["topics"]}
    topics_by_chapter: dict = {}
    topic_ranges: dict = {}
    violations = 0

    for t in entities["topics"]:
        cid = t.get("chapter_id", "")
        c_start, c_end = chap_ranges.get(cid, (0, 9999))
        violated = _clamp_pages(t, c_start, c_end)
        if violated:
            t["boundary_violation"] = True
            violations += 1
        topics_by_chapter.setdefault(cid, []).append(t)
        topic_ranges[t["id"]] = (t.get("page_start") or 0, t.get("page_end") or 9999)

    if violations:
        print(f"  [VALIDATE] {violations} topic(s) had page_start outside their chapter bounds.")

    overflow_count = 0
    for st in entities.get("subtopics", []):
        tid = st.get("topic_id", "")
        t_start, t_end = topic_ranges.get(tid, (0, 9999))
        before = (st.get("page_start"), st.get("page_end"))
        _clamp_pages(st, t_start, t_end)
        if (st.get("page_start"), st.get("page_end")) != before:
            overflow_count += 1
        if "skill_type" not in st:
            st["skill_type"] = classify_skill(
                st.get("name", "") + " " + st.get("summary", "")
            )
    if overflow_count:
        print(f"  [VALIDATE] Clamped {overflow_count} subtopic page overflow(s).")

    # 12: per-chapter confidence + status
    chapters_with_topics = {t["chapter_id"] for t in entities["topics"]}
    for c in valid_chaps:
        c.setdefault("confidence", 1.0)
        if c["id"] not in chapters_with_topics:
            c["confidence"] = round(c["confidence"] * 0.3, 2)
            print(f"  [VALIDATE] {c['id']}: no topics, confidence={c['confidence']}")
        else:
            page_count = max(1, (c.get("page_end") or 0) - (c.get("page_start") or 0) + 1)
            t_count = sum(1 for t in entities["topics"] if t.get("chapter_id") == c["id"])
            if t_count == 1 and page_count > 8:
                c["confidence"] = round(c["confidence"] * 0.7, 2)

        conf = c["confidence"]
        if conf >= 0.8:
            c["status"] = "verified"
        elif conf >= 0.4:
            c["status"] = "partial"
        else:
            c["status"] = "unverified"

    for c in phantom_chaps:
        c.pop("order", None)
        c["confidence"] = 0.0
        c["status"] = "unverified"

    # 13: prerequisites — clean invalid IDs, infer chain, break cycles
    sorted_chap_ids = [c["id"] for c in valid_chaps]

    def last_topic_before(chap_idx: int):
        for k in range(chap_idx - 1, -1, -1):
            prev = topics_by_chapter.get(sorted_chap_ids[k], [])
            if prev:
                return prev[-1]["id"]
        return None

    for idx, cid in enumerate(sorted_chap_ids):
        for t in topics_by_chapter.get(cid, []):
            clean = [
                p for p in t.get("prerequisites", [])
                if isinstance(p, str) and p.startswith("T_") and p in valid_topic_ids
            ]
            removed = set(t.get("prerequisites", [])) - set(clean)
            if removed:
                print(f"  [VALIDATE] {t['id']}: dropped bad prerequisites {removed}")
            if not clean and idx > 0:
                nearest = last_topic_before(idx)
                if nearest and nearest != t["id"]:
                    clean = [nearest]
            t["prerequisites"] = clean

    _break_prereq_cycles(entities["topics"])

    # 14: classify exercises
    for ex in entities.get("exercises", []):
        if "exercise_type" not in ex:
            ex["exercise_type"] = classify_exercise(ex.get("text", ""))

    # 15: group recurring same-title chapters into curriculum threads
    title_to_chapters: dict = {}
    for c in valid_chaps:
        title_to_chapters.setdefault(c["title"], []).append({
            "chapter_id": c["id"],
            "number": c["number"],
            "page_start": c.get("page_start"),
            "page_end": c.get("page_end"),
        })

    chapter_groups = [
        {"title": title, "sections": sections}
        for title, sections in title_to_chapters.items()
        if len(sections) > 1
    ]
    if chapter_groups:
        ontology["chapter_groups"] = chapter_groups
        recurring = [g["title"] for g in chapter_groups]
        print(f"  [VALIDATE] {len(chapter_groups)} recurring chapter theme(s): {recurring}")

    # 16: quarantine low-confidence chapters
    CONFIDENCE_THRESHOLD = 0.6
    low_conf = [c for c in entities["chapters"]
                if c.get("confidence", 1.0) < CONFIDENCE_THRESHOLD]
    if low_conf:
        entities["chapters"] = [c for c in entities["chapters"]
                                if c.get("confidence", 1.0) >= CONFIDENCE_THRESHOLD]
        for i, c in enumerate(entities["chapters"]):
            c["number"] = i + 1
        ontology["low_confidence_chapters"] = low_conf
        ids = [c["id"] for c in low_conf]
        print(f"  [VALIDATE] Quarantined {len(low_conf)} low-confidence chapter(s) "
              f"(< {CONFIDENCE_THRESHOLD}): {ids}")

    return ontology


# ── Legacy Rebuild ───────────────────────────────────────────────────────────

def _rebuild_legacy(ontology: dict):
    """Rebuild the legacy 'chapters' list for API backward-compatibility."""
    chapters_map = {}
    for c in ontology["entities"]["chapters"]:
        chapters_map[c["id"]] = {
            "chapter_number": c.get("number"),
            "chapter_title": c.get("title"),
            "page_start": c.get("page_start"),
            "page_end": c.get("page_end"),
            "confidence": c.get("confidence", 1.0),
            "topics": [],
        }

    ex_map = {e["id"]: e for e in ontology["entities"]["exercises"]}
    sb_map = {s["id"]: s for s in ontology["entities"]["sidebars"]}
    st_by_topic: dict = {}
    for st in ontology["entities"].get("subtopics", []):
        st_by_topic.setdefault(st.get("topic_id"), []).append(st)

    for t in ontology["entities"]["topics"]:
        cid = t.get("chapter_id")
        if cid not in chapters_map:
            continue
        chapters_map[cid]["topics"].append({
            "topic_name": t.get("name"),
            "concept_summary": t.get("summary"),
            "page_start": t.get("page_start"),
            "page_end": t.get("page_end"),
            "subtopics": [
                {
                    "subtopic_name": s.get("name"),
                    "summary": s.get("summary"),
                    "page_start": s.get("page_start"),
                    "page_end": s.get("page_end"),
                }
                for s in st_by_topic.get(t.get("id"), [])
            ],
            "prerequisites": t.get("prerequisites", []),
            "original_exercises": [
                {"text": ex_map[eid]["text"], "page": ex_map[eid].get("page")}
                for eid in t.get("exercise_ids", []) if eid in ex_map
            ],
            "details_and_sidebars": [
                {"text": sb_map[sid]["text"], "page": sb_map[sid].get("page")}
                for sid in t.get("sidebar_ids", []) if sid in sb_map
            ],
            "status": t.get("status", "untaught"),
            "last_taught_date": t.get("last_taught_date"),
        })

    ontology["chapters"] = sorted(
        chapters_map.values(),
        key=lambda c: c.get("chapter_number") or 999,
    )

    if ontology.get("unresolved_chapters"):
        print(f"  [LEGACY] {len(ontology['unresolved_chapters'])} unresolved chapter(s) in "
              f"ontology['unresolved_chapters'].")


# ── Main Entry Point ───────────────────────────────────────────────────────

def generate_ontology_vision(
    pdf_path: str,
    output_dir: str = "output",
    language: str = "auto",
    progress_cb: ProgressCb = None,
) -> tuple:
    """
    Vision-based ontology generation for textbooks.

    Args:
        pdf_path:    Path to the PDF file.
        output_dir:  Root output directory (checkpoint + ontology.json land here).
        language:    Language name ("Hindi", "Telugu", ...) or "auto" to detect.
        progress_cb: Optional callback (pct: int, message: str) for progress updates.

    Returns:
        (ontology dict, job_dir Path)
    """

    def _progress(pct: int, msg: str):
        if progress_cb:
            try:
                progress_cb(pct, msg)
            except Exception:
                pass

    pdf_name = Path(pdf_path).stem
    job_dir = Path(output_dir) / pdf_name
    job_dir.mkdir(parents=True, exist_ok=True)

    _progress(3, "Detecting textbook language…")
    if language == "auto":
        language = detect_language_vision(pdf_path)
    _progress(6, f"Language: {language}. Detecting chapters…")

    doc = fitz.open(pdf_path)
    detected = detect_chapters_vision(pdf_path)

    if not detected:
        print("[WARNING] No chapters detected. Treating full PDF as one chunk.")
        chunks = [{"title": "Full Book", "pages": list(range(len(doc)))}]
    else:
        print(f"[VISION] {len(detected)} chapters found. Extracting per chapter...")
        chunks = [
            {
                "title": ch["title"],
                "pages": list(range(ch["start_page"], ch["end_page"] + 1)),
            }
            for ch in detected
        ]

    _progress(10, f"{len(chunks)} chapter(s) found. Extracting content…")

    global_chapter_list = "\n".join(
        f"  {i+1}. {ch['title']} (pages {ch['pages'][0]+1}-{ch['pages'][-1]+1})"
        for i, ch in enumerate(chunks)
        if ch["pages"]
    )

    full_ontology = {
        "subject": pdf_name.replace("_", " ").title(),
        "language": language,
        "entities": {
            "chapters": [], "topics": [], "subtopics": [],
            "exercises": [], "sidebars": [],
        },
        "graphs": {
            "chapter_structure": [], "exercise_mapping": [], "concept_dependencies": [],
        },
        "chapters": [],
    }

    ontology_path = job_dir / "ontology.json"

    done_indices = _load_checkpoint(job_dir)

    if done_indices and ontology_path.exists():
        try:
            full_ontology = json.loads(ontology_path.read_text(encoding="utf-8"))
            print(f"[CHECKPOINT] Loaded partial ontology with {len(full_ontology['entities']['topics'])} topics.")
        except Exception:
            pass

    merge_lock = threading.Lock()

    def _process_chunk(idx: int, chunk: dict):
        if not chunk["pages"]:
            return idx, {}, "empty"

        thread_doc = fitz.open(pdf_path)
        context = f"Chapter {idx+1}: '{chunk['title']}'"
        prompt_levels = [
            ("full",       lambda: None),
            ("simplified", lambda: _simplified_prompt(idx + 1, language, context)),
            ("minimal",    lambda: _minimal_prompt(idx + 1, language, context)),
        ]

        print(f"\n[VISION] Chapter {idx+1}/{len(chunks)}: {chunk['title']} ({len(chunk['pages'])} pages)")

        try:
            for level_idx, (label, simple_prompt_fn) in enumerate(prompt_levels):
                try:
                    if label == "full":
                        data = extract_chapter_batched(
                            thread_doc,
                            pages=chunk["pages"],
                            chap_num=idx + 1,
                            chap_title=chunk["title"],
                            language=language,
                            global_chapter_list=global_chapter_list,
                        )
                    else:
                        images = [render_page(thread_doc, p) for p in chunk["pages"] if p < len(thread_doc)]
                        raw = call_gemini([simple_prompt_fn()] + images)
                        data = robust_json_parse(raw)

                    return idx, data, label

                except Exception as exc:
                    print(f"  [FAIL:{label}] Chapter {idx+1}: {exc}")
                    if label == "minimal":
                        (job_dir / f"error_chunk_{idx+1}.txt").write_text(str(exc), encoding="utf-8")
                    if level_idx < len(prompt_levels) - 1:
                        time.sleep(INTER_CALL_DELAY)

            return idx, None, "failed"
        finally:
            thread_doc.close()

    # Mark empty/done chapters immediately; collect chunks that need processing
    pending = []
    for idx, chunk in enumerate(chunks):
        if idx in done_indices:
            print(f"[SKIP] Chapter {idx+1}/{len(chunks)}: {chunk['title']} (already done)")
            continue
        if not chunk["pages"]:
            print(f"[SKIP] Chapter {idx+1}: empty page range.")
            with merge_lock:
                done_indices.add(idx)
                _save_checkpoint(job_dir, done_indices)
            continue
        pending.append((idx, chunk))

    print(f"\n[PARALLEL] Processing {len(pending)} chapters with {CHAPTER_WORKERS} workers...")

    total_chunks = len(chunks)
    completed = len(chunks) - len(pending)

    with ThreadPoolExecutor(max_workers=CHAPTER_WORKERS) as pool:
        futures = {
            pool.submit(_process_chunk, idx, chunk): idx
            for idx, chunk in pending
        }

        for future in as_completed(futures):
            idx, data, label = future.result()

            if label == "empty":
                with merge_lock:
                    done_indices.add(idx)
                    _save_checkpoint(job_dir, done_indices)
                completed += 1
                _progress(min(85, 10 + int(75 * completed / max(1, total_chunks))),
                          f"Extracted {completed}/{total_chunks} chapters…")
                continue

            if data is None:
                print(f"  [ERROR] Chapter {idx+1} failed all retry levels. Moving on.")
                completed += 1
                _progress(min(85, 10 + int(75 * completed / max(1, total_chunks))),
                          f"Extracted {completed}/{total_chunks} chapters…")
                continue

            with merge_lock:
                _merge(full_ontology, data)
                ontology_path.write_text(
                    json.dumps(full_ontology, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                done_indices.add(idx)
                _save_checkpoint(job_dir, done_indices)

                e = full_ontology["entities"]
                print(
                    f"  [OK:{label}] Chapter {idx+1} done — running total: "
                    f"chapters={len(e['chapters'])}, topics={len(e['topics'])}, "
                    f"subtopics={len(e['subtopics'])}, exercises={len(e['exercises'])}"
                )
            completed += 1
            _progress(min(85, 10 + int(75 * completed / max(1, total_chunks))),
                      f"Extracted {completed}/{total_chunks} chapters…")

    # Final validation
    print("\n[VALIDATE] Running structural validation...")
    _progress(88, "Validating and structuring topics…")
    full_ontology = validate_and_fix(full_ontology)

    # Cross-chapter semantic dependency inference
    _progress(93, "Linking related concepts…")
    print("[AI] Inferring cross-chapter semantic dependencies...")
    cross_deps = _infer_cross_chapter_deps(full_ontology)
    existing_edges = {(e["from"], e["to"]) for e in full_ontology["graphs"]["concept_dependencies"]}
    added = 0
    for dep in cross_deps:
        key = (dep.get("from"), dep.get("to"))
        if key not in existing_edges:
            full_ontology["graphs"]["concept_dependencies"].append(dep)
            existing_edges.add(key)
            added += 1
    if added:
        print(f"  [DEPS] Added {added} cross-chapter dependency edges.")

    _rebuild_legacy(full_ontology)

    ontology_path.write_text(
        json.dumps(full_ontology, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    cp = job_dir / "checkpoint.json"
    if cp.exists():
        cp.unlink()

    _progress(97, "Finalizing…")
    print(f"\n[SUCCESS] Vision ontology saved -> {ontology_path}")
    return full_ontology, job_dir
