"""The Tool Gateway: everything the agents are allowed to read from.

The agents themselves do no I/O. Every fact they work from — chapter text,
competencies, activity templates, class context, the adaptive state — arrives
through a function here. That is not architectural tidiness for its own sake:
it is what lets the whole graph be exercised against the ingested textbook
folder on disk, with no Supabase and no database, which is how the prompts get
iterated on.

Two curriculum sources, same output shape:

  * Supabase `textbook_chapters` — the production path, published chapters only.
  * An ingested textbook folder — `chapters/chapters.json` plus either
    `chapters/NN_slug.md` or the per-page `pages/page_NNNN.md` files. This is
    the shape the Class 3 Maths ingest on disk already has, page markers and
    all, so it feeds `textbook_prompt_block` without conversion.
"""
import asyncio
import json
import os
import re
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from . import db
from .llm import call_json
from .deps import (
    create_admin_client,
    env_int,
    fetch_feedback_context,
    gather_class_context,
    get_recommended_activities,
    grade_band_for,
    personalization_tier_line,
    resolve_canonical,
    title_score,
)

# A chapter can run to twenty pages of transcription, and the sequencing agent
# needs all of it — a chapter truncated before its last pages produces a topic
# sequence that silently stops short of the syllabus. Sized for that rather than
# for prompt economy: ~90k characters is ~23k tokens, which the configured models
# hold comfortably, and this prompt runs once per chapter.
#
# A per-topic prompt gets nothing like this much; its slice is bounded by
# TOPIC_EXCERPT_CHARS.
MAX_CHAPTER_CHARS = env_int("PREP_FLOW_MAX_CHAPTER_CHARS", 90_000)
TOPIC_EXCERPT_CHARS = env_int("PREP_FLOW_TOPIC_EXCERPT_CHARS", 6_000)

_PAGE_MARKER = re.compile(r"<!--\s*page\s+(\d+)\s*-->", re.I)


# ── Curriculum source: an ingested textbook folder ───────────────────────────

def _chapter_slug_candidates(root: Path, number: int, title: str) -> list[Path]:
    chapters_dir = root / "chapters"
    if not chapters_dir.is_dir():
        return []
    stem = re.sub(r"[^a-z0-9]+", "_", (title or "").lower()).strip("_")
    exact = list(chapters_dir.glob(f"{number:02d}_*.md"))
    named = [p for p in chapters_dir.glob("*.md") if stem and stem in p.stem]
    return exact + [p for p in named if p not in exact]


_MD_IMAGE = re.compile(r"!\[([^\]]{12,})\]\(([^)]+?([^/)]+\.(?:jpe?g|png|webp)))\)", re.I)
_HTML_IMAGE = re.compile(r'(<img\b[^>]*?\bsrc="([^"]+?([^/"]+\.(?:jpe?g|png|webp)))"[^>]*?>)', re.I)


def _caption_map(chapter_markdown: str) -> dict[str, str]:
    """image filename -> the vision pass's description of it.

    The ingest writes these descriptions into the assembled chapter markdown and
    nowhere else — the per-page files and the per-page JSON both carry the image
    with an empty caption. They are worth recovering: "a woven basket with two
    handles" is exactly the kind of concrete thing the Real Life and Explore
    sections are supposed to build on.
    """
    captions: dict[str, str] = {}
    for match in _MD_IMAGE.finditer(chapter_markdown or ""):
        caption = " ".join(match.group(1).split())
        filename = match.group(3).lower()
        if caption and caption.lower() != "image":
            captions.setdefault(filename, caption)
    return captions


def _apply_captions(page_markdown: str, captions: dict[str, str]) -> str:
    """Put the recovered captions back onto the page-stitched markdown.

    As a single `[Figure: …]` prose line before the tag, and not also as a
    caption= attribute. The attribute would buy nothing — these tags carry `src`,
    not the `id` that textbook_grounding.anchors_in matches on — while doubling
    the bytes each caption costs. With ~54 captions in a twenty-page chapter that
    difference decided whether the last two pages fitted under
    MAX_CHAPTER_CHARS, which is not a trade worth making for a duplicate.
    """
    if not captions:
        return page_markdown

    def replace(match: re.Match) -> str:
        tag, filename = match.group(1), match.group(3).lower()
        caption = captions.get(filename)
        if not caption:
            return tag
        return f"[Figure: {caption}]\n{tag}"

    return _HTML_IMAGE.sub(replace, page_markdown)


def _pages_between(root: Path, start: int, end: int) -> str:
    """Concatenate the per-page markdown for a page range, in order.

    The page files already carry `<!-- page N -->`, so the result is directly
    usable as textbook grounding: the model can cite (Page 21) and a teacher can
    open the book to it.
    """
    pages_dir = root / "pages"
    if not pages_dir.is_dir():
        return ""
    chunks: list[str] = []
    for page in range(int(start or 1), int(end or start or 1) + 1):
        candidate = pages_dir / f"page_{page:04d}.md"
        if not candidate.exists():
            continue
        text = candidate.read_text(encoding="utf-8", errors="replace").strip()
        if not text:
            continue
        if not _PAGE_MARKER.search(text[:200]):
            text = f"<!-- page {page} -->\n{text}"
        chunks.append(text)
    return "\n\n".join(chunks)


# ── Curriculum source: the CLASSIFIED page format ────────────────────────────
#
# A richer ingest that labels every block by what it IS rather than leaving the
# reader to infer it:
#
#     [TOPIC]     a section the book itself names
#     [SUBTOPIC]  a section nested inside a TOPIC
#     [CONCEPT]   the teaching content
#     [CONTENT]   the newer ingest's name for the same thing
#     [ACTIVITY]  the book's own exercises — reusable word for word
#     [FIGURE: …] an illustration, with the vision pass's description INLINE
#     [FIGURE DESCRIPTION: …]  that description as a block of its own
#     [OTHER]     page furniture: footers, running heads, page numbers
#
# Worth preferring over the plain pages for four reasons. Concept is
# textbook-only, so knowing which blocks are actually concept and which are
# exercises makes that section far easier to ground. The book's own activities
# become identifiable, which the pilot's grounding block already asks for
# ("Reuse the book's own exercises and activities where they fit, word for
# word"). TOPIC and SUBTOPIC are the only structure a classified chapter has at
# all -- rendered as real markdown headings they become the anchors
# `sequencing.chapter_outline` cuts the chapter on, which without them returns
# one anonymous span covering every page of the chapter. And OTHER can be
# dropped, so "Government's Gift for Students' Progress" stops eating the
# excerpt budget on every page.
#
# The vocabulary below is a TABLE rather than a hardcoded alternation because
# the ingest that writes these labels lives upstream of this repo and is still
# growing. A label added there before it is added here has to degrade to "kept
# as text, nuance lost" -- never to "block silently dropped", which is exactly
# what an alternation that fails to match does.

# label -> how everything downstream should treat it.
#   canonical:      the label the rest of the pipeline sees. CONTENT is a
#                   second name for CONCEPT and is folded into it at parse, so
#                   no consumer has to learn both spellings.
#   heading_level:  rendered as a markdown heading of this depth rather than as
#                   a [LABEL] line, which is what makes it anchorable.
#   figure:         carries a picture, or a picture's description.
#   drop:           page furniture, kept out of the excerpt entirely.
CLASSIFIED_LABELS: dict[str, dict] = {
    "TOPIC":              {"canonical": "TOPIC",     "heading_level": 2},
    "SUBTOPIC":           {"canonical": "SUBTOPIC",  "heading_level": 3},
    "CONCEPT":            {"canonical": "CONCEPT"},
    "CONTENT":            {"canonical": "CONCEPT"},
    "ACTIVITY":           {"canonical": "ACTIVITY"},
    "OTHER":              {"canonical": "OTHER",     "drop": True},
    "FIGURE":             {"canonical": "FIGURE",    "figure": True},
    "FIGURE DESCRIPTION": {"canonical": "FIGURE_DESCRIPTION", "figure": True},
    "FIGURE_DESCRIPTION": {"canonical": "FIGURE_DESCRIPTION", "figure": True},
}


def _alternation(names) -> str:
    """Longest label first, so "FIGURE DESCRIPTION" is never read as a "FIGURE"
    whose id happens to be the word DESCRIPTION, and "SUBTOPIC" is never read as
    a stray "TOPIC"."""
    return "|".join(re.escape(n) for n in sorted(names, key=len, reverse=True))


_WORD_ALT = _alternation([n for n, s in CLASSIFIED_LABELS.items() if not s.get("figure")])
_FIGURE_ALT = _alternation([n for n, s in CLASSIFIED_LABELS.items() if s.get("figure")])
_LABEL_ALT = _alternation(CLASSIFIED_LABELS)

_CLASSIFIED_BLOCK = re.compile(rf"^\[({_WORD_ALT})[\]:]\s*(.*)$", re.M)
_CLASSIFIED_FIGURE = re.compile(rf"^\[(?:{_FIGURE_ALT}):\s*(.*?)\]\s*$", re.M | re.S)
_ANY_CLASSIFIED = re.compile(rf"^\[(?:{_LABEL_ALT})[\]:]", re.M)

# One scanner for both block shapes. `[LABEL] body` and `[LABEL: body]` are both
# accepted for word labels -- the local ingest writes the first and the API-side
# one writes the second, and no caller should have to care which. A figure may
# arrive bare (`[FIGURE]`), captioned (`[FIGURE: ...]`), or fully resolved
# (`[FIGURE fig_11_1 -> imgs/x.jpg: ...]`, which is what render_classified
# emits), so this also round-trips its own output.
# Any label-shaped line, known to this table or not. Two jobs: it ends the
# previous block, and it lets an unrecognised label through as a block of its
# own. Both matter because the classifier upstream is still gaining labels — a
# lookahead that only knew the labels in the table above let an unknown
# [GLOSSARY] line be swallowed into the heading before it, which is worse than
# either dropping it or keeping it, since it corrupts a block that WAS
# understood.
_GENERIC_LABEL = r"[A-Z][A-Z0-9 _-]*"

# Figure shapes are tried FIRST, because "[FIGURE: ...]" is also a well-formed
# generic label line and the generic branch would otherwise claim it.
_BLOCK_SCANNER = re.compile(
    rf"^\[(?P<fig>{_FIGURE_ALT})"
    rf"(?:\s+(?P<fid>[^\]:>]+?))?"
    rf"(?:\s*->\s*(?P<fpath>[^\]:]+?))?"
    rf"(?:\s*:\s*(?P<fbody>.*?))?\]\s*$"
    rf"|^\[(?P<word>{_GENERIC_LABEL})(?P<wterm>[\]:])\s*(?P<wbody>.*?)"
    rf"(?=^\[[A-Z]|\Z)",
    re.M | re.S,
)

# A heading needs at least one letter. That keeps a page number the classifier
# labelled TOPIC from becoming an anchor, without applying the length floor
# below -- a real section title is legitimately short ("FAMILY" is six
# characters) and that floor would eat every one of them.
_HAS_LETTER = re.compile(r"[^\W\d_]", re.UNICODE)

# Blocks this short are page numbers and running heads the classifier labelled
# CONCEPT ("3", "FAMILY"). Keeping them costs excerpt budget and teaches the
# model that one-word concepts are normal.
_MIN_BLOCK_CHARS = 12


def has_classified_pages(root: Path) -> bool:
    directory = root / "pages_classified"
    return directory.is_dir() and any(directory.glob("page_*.md"))


def parse_classified_page(text: str, page: int, image_paths: list[str] = None) -> list[dict]:
    """One classified page as labelled blocks, in document order.

    `image_paths` are the `<img src>` values from the SAME page's plain markdown,
    used to give each figure a real file to point at. They are attached ONLY when
    the counts agree — see attach_figure_paths for why guessing is worse than
    leaving them unattached.
    """
    blocks: list[dict] = []
    for match in _BLOCK_SCANNER.finditer(text):
        if match.group("word"):
            name = match.group("word").strip()
            spec = CLASSIFIED_LABELS.get(name)
            body = " ".join((match.group("wbody") or "").split())
            # The `[LABEL: body]` spelling closes with a bracket the
            # `[LABEL] body` spelling does not have.
            if match.group("wterm") == ":" and body.endswith("]"):
                body = body[:-1].rstrip()
            if spec is None:
                # A label added upstream before it was added here. Kept, with
                # its label, so it still reaches the excerpt and the model can
                # read it as prose — only the routing is lost, and teaching it
                # is one row in the table above.
                if len(body) >= _MIN_BLOCK_CHARS:
                    blocks.append({"label": name, "text": body, "page": page})
                continue
            if spec.get("heading_level"):
                if not _HAS_LETTER.search(body):
                    continue
            elif len(body) < _MIN_BLOCK_CHARS:
                continue
            blocks.append({"label": spec["canonical"], "text": body, "page": page})
        else:
            spec = CLASSIFIED_LABELS[match.group("fig")]
            block = {"label": spec["canonical"],
                     "text": " ".join((match.group("fbody") or "").split()),
                     "page": page}
            # An id or path the ingest already resolved is better than one this
            # function would assign by position, so it is kept.
            if match.group("fid"):
                block["id"] = match.group("fid").strip()
            if match.group("fpath"):
                block["path"] = match.group("fpath").strip()
            blocks.append(block)

    blocks = _bind_figure_descriptions(blocks)
    # A figure with no description is not pointable: the sheet could only tell
    # the teacher "look at the third picture on page 12", which is not something
    # a teacher can act on mid-lesson. Dropped after binding, never before —
    # before binding, the description that would have made it usable has not
    # arrived yet.
    blocks = [b for b in blocks if b["label"] != "FIGURE" or b["text"]]

    figures = [b for b in blocks if b["label"] == "FIGURE"]
    paths = image_paths or []
    # Fail closed. A figure reference pointing at the wrong picture is worse than
    # one pointing at nothing: the sheet tells the teacher to hold up an image
    # that shows something else, and they find out in front of the class. Same
    # reasoning textbook_grounding applies to chapter matching.
    aligned = len(figures) == len(paths) and bool(paths)
    for i, figure in enumerate(figures, start=1):
        figure.setdefault("id", f"fig_{page}_{i}")
        if not figure.get("path"):
            figure["path"] = paths[i - 1] if aligned else None
    return blocks


def _bind_figure_descriptions(blocks: list[dict]) -> list[dict]:
    """`[FIGURE DESCRIPTION: …]` belongs to the figure above it.

    The newer ingest splits a picture in two: the figure, and the vision pass's
    description of it. Nothing downstream wants that as two blocks — a figure
    with no description cannot be pointed at and a description with no figure
    cannot be shown — so the description is folded into the figure it describes.

    One that arrives with no figure above it is promoted to a figure of its own
    rather than dropped. That is the same call `api_figures` already makes for a
    caption whose image id is missing: the description is evidence a real
    picture is printed there, and losing it loses the picture.
    """
    out: list[dict] = []
    for block in blocks:
        if block["label"] != "FIGURE_DESCRIPTION":
            out.append(block)
            continue
        previous = out[-1] if out else None
        if (previous is not None and previous["label"] == "FIGURE"
                and previous["page"] == block["page"]):
            if block["text"] and block["text"] not in previous["text"]:
                previous["text"] = f'{previous["text"]} {block["text"]}'.strip()
            for key in ("id", "path"):
                if block.get(key) and not previous.get(key):
                    previous[key] = block[key]
        else:
            out.append({**block, "label": "FIGURE"})
    return out


def _plain_image_paths(root: Path, page: int) -> list[str]:
    plain = root / "pages" / f"page_{page:04d}.md"
    if not plain.exists():
        return []
    return re.findall(r'<img\b[^>]*?\bsrc="([^"]+)"',
                      plain.read_text(encoding="utf-8", errors="replace"), re.I)


def render_classified(blocks: list[dict], *, include_other: bool = False) -> str:
    """Labelled blocks back to markdown, keeping the labels.

    The labels stay in the text on purpose: they are the whole value of this
    format. A prompt that can see which lines are the book's own exercises can
    reuse them verbatim, and one that can see which are CONCEPT can ground the
    Concept section on exactly those.

    TOPIC and SUBTOPIC are the exception, and become real ##/### headings
    instead. That is not cosmetic. `sequencing.chapter_outline` reads markdown
    headings and nothing else, so a [TOPIC] left as a label is a section the
    sequencer cannot anchor a period to — which is why a classified chapter
    used to yield exactly one anchor, "(the chapter's opening)", spanning every
    page in it. Rendered as a heading it anchors, and every existing consumer of
    a heading picks it up unchanged: the per-topic excerpt, the page range that
    excerpt reports, `span_by_heading`, and the viewer.
    """
    out: list[str] = []
    current_page = None
    for block in blocks:
        label = block["label"]
        spec = CLASSIFIED_LABELS.get(label) or {}
        if spec.get("drop") and not include_other:
            continue
        if block["page"] != current_page:
            current_page = block["page"]
            out.append(f"\n<!-- page {current_page} -->")
        if label == "FIGURE":
            marker = f"[FIGURE {block['id']}" if block.get("id") else "[FIGURE"
            path = f" -> {block['path']}" if block.get("path") else ""
            out.append(f"{marker}{path}: {block['text']}]")
        elif spec.get("heading_level"):
            # The blank line goes in as its own entry rather than inside the
            # heading, so the join below still starts the heading on a line of
            # its own when the block before it ran long.
            out.append("")
            out.append("#" * spec["heading_level"] + " " + block["text"])
        else:
            out.append(f"[{label}] {block['text']}")
    return "\n".join(out).strip()


def load_classified_pages(root: Path, start: int, end: int) -> tuple[str, list[dict]]:
    """(markdown, figures) for a page range, from the classified ingest."""
    directory = root / "pages_classified"
    blocks: list[dict] = []
    for page in range(int(start or 1), int(end or start or 1) + 1):
        path = directory / f"page_{page:04d}.md"
        if not path.exists():
            continue
        blocks += parse_classified_page(
            path.read_text(encoding="utf-8", errors="replace"), page,
            _plain_image_paths(root, page))
    figures = [{"id": b["id"], "page": b["page"], "caption": b["text"],
                "path": b.get("path")}
               for b in blocks if b["label"] == "FIGURE" and b.get("id")]
    return render_classified(blocks), figures


def list_local_chapters(folder: str) -> list[dict]:
    """Every chapter in an ingested folder, with whether text is actually
    available for it. A partial ingest (pages 9–30 of a 129-page book) lists all
    thirteen chapters but can only teach two of them, and a caller that cannot
    see that difference generates a chapter of prep material from an empty
    string."""
    root = Path(folder)
    # Ingests nest the book folder inside a folder of the same name.
    if not (root / "chapters").is_dir() and (root / root.name / "chapters").is_dir():
        root = root / root.name
    index = root / "chapters" / "chapters.json"
    if not index.exists():
        raise FileNotFoundError(f"no chapters/chapters.json under {root}")

    entries = json.loads(index.read_text(encoding="utf-8"))
    out: list[dict] = []
    for i, entry in enumerate(entries, start=1):
        title = entry.get("title") or f"Chapter {i}"
        start, end = entry.get("start_page"), entry.get("end_page")
        available = 0
        pages_dir = root / "pages"
        if pages_dir.is_dir() and start:
            available = sum(
                1 for p in range(int(start), int(end or start) + 1)
                if (pages_dir / f"page_{p:04d}.md").exists()
            )
        has_chapter_md = bool(_chapter_slug_candidates(root, i, title))
        out.append({
            "chapterNumber": i,
            "chapterTitle": title,
            "pageStart": start,
            "pageEnd": end,
            "pagesIngested": available,
            "pagesTotal": (int(end or start or 0) - int(start or 0) + 1) if start else 0,
            "hasChapterMarkdown": has_chapter_md,
            "usable": bool(has_chapter_md or available),
            "root": str(root),
        })
    return out


def load_local_chapter(folder: str, *, chapter_number: int = None,
                       chapter_title: str = None) -> dict:
    """One chapter's verbatim text from an ingested folder.

    Built from the per-page files, not the assembled `chapters/NN_slug.md`, and
    that choice matters more than it looks. The assembled file reads better — it
    carries the vision pass's figure captions — but it has NO `<!-- page N -->`
    markers, and this pipeline needs them for two things it cannot do without:
    slicing a per-topic excerpt (without markers every topic in the chapter gets
    the same first 6000 characters, so Concept grounding stops meaning anything)
    and citing (Page N) so a teacher can point at the book.

    So the page files provide the spine and the assembled file is mined for its
    captions, which are grafted back on. Best of both, with the markers intact.
    """
    chapters = list_local_chapters(folder)
    if not chapters:
        raise ValueError(f"no chapters found in {folder}")
    root = Path(chapters[0]["root"])

    chosen: Optional[dict] = None
    if chapter_number:
        chosen = next((c for c in chapters if c["chapterNumber"] == chapter_number), None)
        if chosen is None:
            raise ValueError(
                f"chapter {chapter_number} not in this book (1–{len(chapters)})")
    elif chapter_title:
        scored = sorted(
            chapters, key=lambda c: title_score(chapter_title, c["chapterTitle"]), reverse=True)
        best = scored[0]
        if title_score(chapter_title, best["chapterTitle"]) <= 0:
            raise ValueError(
                f'no chapter matches "{chapter_title}". Available: '
                + ", ".join(f'{c["chapterNumber"]}. {c["chapterTitle"]}' for c in chapters))
        chosen = best
    else:
        chosen = next((c for c in chapters if c["usable"]), chapters[0])

    assembled = ""
    for path in _chapter_slug_candidates(root, chosen["chapterNumber"], chosen["chapterTitle"]):
        assembled = path.read_text(encoding="utf-8", errors="replace").strip()
        if assembled:
            break

    # The classified ingest wins when it covers this chapter. It carries the same
    # page markers plus a semantic label on every block and an inline description
    # for every figure — strictly more than the plain pages, minus the furniture.
    figures: list[dict] = []
    if has_classified_pages(root):
        classified, figures = load_classified_pages(
            root, chosen["pageStart"], chosen["pageEnd"])
        if classified and page_index(classified):
            return {
                "chapterNumber": chosen["chapterNumber"],
                "chapterTitle": chosen["chapterTitle"],
                "pageStart": chosen["pageStart"], "pageEnd": chosen["pageEnd"],
                "markdown": classified[:MAX_CHAPTER_CHARS],
                "truncated": len(classified) > MAX_CHAPTER_CHARS,
                "hasPageMarkers": True,
                "captionsRecovered": len(figures),
                "figures": figures,
                "images": [],
                "source": "local",
                "sourceDetail": "classified",
            }

    stitched = _pages_between(root, chosen["pageStart"], chosen["pageEnd"])
    captions = _caption_map(assembled)

    if stitched and page_index(stitched):
        markdown, detail = _apply_captions(stitched, captions), "pages+captions"
    elif assembled and page_index(assembled):
        markdown, detail = assembled, "chapter-markdown"
    elif stitched:
        markdown, detail = _apply_captions(stitched, captions), "pages"
    elif assembled:
        # No markers anywhere. Usable, but per-topic grounding degrades to a
        # proportional slice and (Page N) citations are impossible — which is why
        # the caller is told, rather than left to wonder why every Concept section
        # looks the same.
        markdown, detail = assembled, "chapter-markdown-no-pages"
    else:
        raise ValueError(
            f'chapter {chosen["chapterNumber"]} ("{chosen["chapterTitle"]}") has no '
            f'ingested text — {chosen["pagesIngested"]}/{chosen["pagesTotal"]} pages '
            f'present. Pick a chapter with usable=true from list_local_chapters().')

    return {
        "chapterNumber": chosen["chapterNumber"],
        "chapterTitle": chosen["chapterTitle"],
        "pageStart": chosen["pageStart"],
        "pageEnd": chosen["pageEnd"],
        "markdown": markdown[:MAX_CHAPTER_CHARS],
        "truncated": len(markdown) > MAX_CHAPTER_CHARS,
        "hasPageMarkers": bool(page_index(markdown[:MAX_CHAPTER_CHARS])),
        "captionsRecovered": len(captions),
        "figures": [],
        "images": [],
        "source": "local",
        "sourceDetail": detail,
    }


# ── Curriculum source: Supabase ──────────────────────────────────────────────

async def load_db_chapter(school_id: str, grade: str, subject: str, *,
                          chapter_number: int = None,
                          chapter_title: str = None) -> Optional[dict]:
    """The published chapter for this school/grade/subject. Published only, for
    the reason textbook_grounding gives: an unreviewed chapter may be mis-split,
    and a whole batch of prep material built on the wrong pages is a far more
    expensive mistake than one lesson."""
    def _query() -> Optional[dict]:
        ac = create_admin_client()
        books = (
            ac.table("textbook_books").select("id")
            .eq("school_id", school_id).eq("grade", str(grade)).ilike("subject", subject)
            .execute()
        ).data or []
        if not books:
            return None
        q = (
            ac.table("textbook_chapters")
            .select("id, chapter_number, chapter_title, page_start, page_end, content_markdown, topics")
            .in_("book_uuid", [b["id"] for b in books])
            .eq("published", True)
        )
        if chapter_number is not None:
            q = q.eq("chapter_number", chapter_number)
        rows = q.order("chapter_number").execute().data or []
        if not rows:
            return None
        if chapter_title and chapter_number is None:
            rows = sorted(rows, key=lambda r: title_score(chapter_title, r["chapter_title"]),
                          reverse=True)
        chapter = rows[0]
        images = (
            ac.table("textbook_images")
            .select("id, image_id, caption, source_page, decorative")
            .eq("chapter_uuid", chapter["id"]).order("order_index").execute()
        ).data or []
        return {
            "chapterNumber": chapter["chapter_number"],
            "chapterTitle": chapter["chapter_title"],
            "pageStart": chapter.get("page_start"),
            "pageEnd": chapter.get("page_end"),
            "markdown": (chapter.get("content_markdown") or "")[:MAX_CHAPTER_CHARS],
            "truncated": len(chapter.get("content_markdown") or "") > MAX_CHAPTER_CHARS,
            "topics": chapter.get("topics") or [],
            "images": [
                {"imageId": i["image_id"], "caption": i.get("caption"),
                 "sourcePage": i.get("source_page")}
                for i in images if not i.get("decorative")
            ],
            "source": "supabase",
        }

    try:
        return await asyncio.to_thread(_query)
    except Exception as exc:
        print(f"[prep_flow:tools] chapter lookup failed: {exc}")
        return None


async def load_chapter(*, school_id: str = None, grade: str = "", subject: str = "",
                       chapter_number: int = None, chapter_title: str = None,
                       local_folder: str = None) -> dict:
    """The chapter to generate from, whichever source is available.

    A local folder always wins when given — it is passed explicitly, so asking
    for it and silently getting a database chapter instead would be the wrong
    surprise.
    """
    if local_folder:
        return load_local_chapter(local_folder, chapter_number=chapter_number,
                                  chapter_title=chapter_title)
    if school_id:
        chapter = await load_db_chapter(school_id, grade, subject,
                                        chapter_number=chapter_number,
                                        chapter_title=chapter_title)
        if chapter:
            return chapter
    raise ValueError(
        "no curriculum source: pass local_folder, or a school_id whose "
        "textbook_chapters has a published chapter for this grade/subject")


# ── Curriculum source: the published textbook API ────────────────────────────
#
# The third source, and structurally the best of them. A folder ingest gives
# page text and, if a vision pass ran, captions grafted inline — but no image
# ids, so `figureRefs` has nothing to reference and the id checks sit idle. This
# API gives what the pipeline was actually designed around: `<img id="...">`
# anchored in the text at the position the picture appears, and a parallel list
# carrying each id's caption. Ids, captions and position, which is everything a
# sheet needs to tell a class "look at this one" and everything the validator
# needs to know the reference is real.
#
# The image URLs are dead on the current deployment (`id=None` on every record)
# and it does not matter. Nothing here wants the pixels: the child has the
# printed book open, and the sheet only has to name the picture accurately
# enough for a teacher to point at it.

API_BASE = os.environ.get(
    "PREP_FLOW_TEXTBOOK_API", "https://eduteach-textbook-api.onrender.com")

# Fetched chapters are cached on disk for the run. A topic search reads every
# chapter of a book — sixteen requests against a free-tier host that cold-starts
# — and doing that again for the generation that follows would be rude and slow.
_API_CACHE = Path(tempfile.gettempdir()) / "prep_flow_textbook_api"


def _api_get(path: str, *, timeout: int = 45, cache: bool = True):
    """One GET against the textbook API, cached on disk. Returns None on failure.

    Never raises: a chapter that will not load is one chapter missing from a
    search, and losing the whole run to a cold-starting host would be the wrong
    trade for a source that is only ever read.
    """
    url = f"{API_BASE.rstrip('/')}/{path.lstrip('/')}"
    key = _API_CACHE / (re.sub(r"[^A-Za-z0-9_.-]", "_", path).strip("_") + ".json")
    if cache and key.exists():
        try:
            return json.loads(key.read_text(encoding="utf-8"))
        except Exception:
            pass
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        print(f"[prep_flow:tools] textbook API {path}: {exc}")
        return None
    if cache:
        try:
            key.parent.mkdir(parents=True, exist_ok=True)
            key.write_text(json.dumps(payload), encoding="utf-8")
        except Exception:
            pass
    return payload


def list_api_books() -> list[dict]:
    """Every published book, with the grade and subject the pipeline needs."""
    books = _api_get("/published/books") or []
    return [{
        "bookId": b.get("book_id"),
        "board": b.get("board"),
        "grade": str(b.get("grade") or ""),
        "subject": b.get("subject") or "",
        "language": b.get("language") or "en",
        "chapterCount": b.get("chapter_count") or 0,
    } for b in books if b.get("book_id")]


def list_api_chapters(book_id: str) -> list[dict]:
    """A book's chapters. Shaped like list_local_chapters so callers can share code."""
    chapters = _api_get(f"/published/books/{book_id}/chapters") or []
    out = []
    for c in chapters:
        pages = (c.get("page_end") or 0) - (c.get("page_start") or 0) + 1
        out.append({
            "chapterNumber": c.get("chapter_number"),
            "chapterTitle": c.get("chapter_title") or "",
            "pageStart": c.get("page_start"),
            "pageEnd": c.get("page_end"),
            "pagesTotal": max(0, pages),
            "pagesIngested": max(0, pages),
            "hasChapterMarkdown": True,
            # The API only publishes chapters that are ready, so anything listed
            # here is generatable — unlike a folder, where a half-ingested
            # chapter is common and has to be screened out.
            "usable": True,
        })
    return sorted(out, key=lambda c: c["chapterNumber"] or 0)


def api_figures(content: str, images: list) -> list[dict]:
    """Image records with the page each one actually sits on.

    The page comes from where the `<img id>` tag falls between the page markers,
    not from any field — the API does not carry one, and inferring it from
    position is exact rather than a guess. An image that is declared but never
    anchored in the text is dropped: a sheet cannot point a class at a picture
    when nobody knows which page to open.

    `path` is now the API's own `images[].url` when present — some chapters
    (first seen: ts_scert_class5_environmental_studies_en, chapter 2) serve a
    real Supabase Storage signed URL per image, where earlier ones only ever
    had `id=None`. Used as-is, on request. WORTH KNOWING: it's a SIGNED url —
    the token in it expires a few hours after the API generated it — so a
    figure persisted with this path (a chapter cached to disk by _api_get(),
    or a material written to prep_flow_materials) will have a working image
    link at generation time and a dead one days later. Nothing here re-signs
    it; that would need its own refresh step if a durable link is wanted.
    """
    captions = {i.get("image_id"): (i.get("caption") or "").strip()
                for i in (images or []) if i.get("image_id")}
    urls = {i.get("image_id"): i.get("url") for i in (images or [])
            if i.get("image_id") and i.get("url")}
    marks = [(m.start(), int(m.group(1)))
             for m in re.finditer(r"<!-- page (\d+) -->", content or "")]
    figures = []
    for m in re.finditer(r'<img id="([^"]+)"', content or ""):
        image_id = m.group(1)
        caption = captions.get(image_id)
        # A missing caption alone used to drop the figure entirely — right
        # when there was nothing else to identify it by. A real image url is
        # its own reason to keep it: a teacher can still be shown the actual
        # picture and asked to describe it, which beats no picture and no
        # caption both.
        if not caption and not urls.get(image_id):
            continue
        page = next((p for pos, p in reversed(marks) if pos <= m.start()), None)
        figures.append({"id": image_id, "page": page, "caption": caption,
                        "path": urls.get(image_id)})
    return figures


# The API serves TWO shapes under the same endpoint, apparently by which ingest
# ran on that book — nothing in the response says which, so content is what has
# to be sniffed. The EVS book above has plain markdown with `<!-- page N -->`
# markers, which `api_figures` already handles. The English book has neither: no
# page markers anywhere, every block labelled [CONCEPT]/[ACTIVITY], and every
# figure's vision-pass caption written straight into the flow after its marker —
# `[FIGURE, not shown]` when the API has no id for it, `<img id="...">` (with the
# SAME caption text, verified against `images[]`) when it does.
_API_BLOCK_MARKER = re.compile(
    rf'\[(?:{_LABEL_ALT})\]'
    rf'|\[FIGURE(?: DESCRIPTION)?, not shown\]'
    rf'|<img id="([^"]+)"\s*/?>')


def parse_api_classified(content: str) -> tuple[str, list[dict]]:
    """The English-book shape above, into the same `[LABEL] text` / `[FIGURE …:
    caption]` markdown `render_classified` produces for a local classified
    ingest — so both sources read identically to everything downstream, and the
    book's own activities stay identifiable as exactly that.

    Page numbers are the one thing this cannot recover: the API exposes the
    chapter's overall page_start/page_end but nothing per-block, so a chapter in
    this shape falls back to proportional grounding like any
    chapter-markdown-no-pages source — that is a limit of what the API returns,
    not something a client-side parser can fix.

    A `[FIGURE, not shown]` caption has no id, so — same fail-closed reasoning as
    parse_classified_page — it is kept as prose (it is often still a real,
    useful description) but never added to `figures`: there is nothing a
    generated sheet could tell a teacher to point at.
    """
    markers = list(_API_BLOCK_MARKER.finditer(content or ""))
    blocks: list[str] = []
    figures: list[dict] = []
    for i, m in enumerate(markers):
        start = m.end()
        end = markers[i + 1].start() if i + 1 < len(markers) else len(content)
        text = " ".join(content[start:end].split())
        marker, image_id = m.group(0), m.group(1)
        if image_id:                                    # <img id="...">
            if text:
                blocks.append(f"[FIGURE {image_id}: {text}]")
                figures.append({"id": image_id, "page": None, "caption": text, "path": None})
        elif marker.endswith(", not shown]"):
            if text:
                blocks.append(f"[FIGURE: {text}]")
        else:
            # Same vocabulary table the local ingest is read through, so a label
            # the upstream classifier starts emitting is understood on both
            # paths at once rather than on whichever one was remembered.
            name = marker.strip("[]")
            spec = CLASSIFIED_LABELS.get(name) or {}
            if spec.get("drop"):
                continue
            if spec.get("figure"):
                # No id, so — same fail-closed rule as the branch above — this
                # is kept as prose but never enters `figures`.
                if text:
                    blocks.append(f"[FIGURE: {text}]")
            elif spec.get("heading_level"):
                if _HAS_LETTER.search(text):
                    blocks.append("#" * spec["heading_level"] + " " + text)
            elif len(text) >= _MIN_BLOCK_CHARS:
                blocks.append(f"[{spec.get('canonical') or name}] {text}")
    return "\n\n".join(blocks), figures


def load_api_chapter(book_id: str, chapter_number: int) -> Optional[dict]:
    """One chapter from the API, in the same shape as load_local_chapter."""
    data = _api_get(f"/published/books/{book_id}/chapters/{chapter_number}")
    if not data or not (data.get("content") or "").strip():
        return None
    content = data["content"]

    if _ANY_CLASSIFIED.search(content):
        markdown, figures = parse_api_classified(content)
        detail = f"{book_id}/{chapter_number} (classified, no page markers)"
    else:
        markdown = content
        figures = api_figures(content, data.get("images") or [])
        detail = f"{book_id}/{chapter_number}"

    return {
        "chapterNumber": data.get("chapter_number") or chapter_number,
        "chapterTitle": data.get("chapter_title") or "",
        "pageStart": data.get("page_start"),
        "pageEnd": data.get("page_end"),
        "markdown": markdown[:MAX_CHAPTER_CHARS],
        "truncated": len(markdown) > MAX_CHAPTER_CHARS,
        "hasPageMarkers": bool(page_index(markdown[:MAX_CHAPTER_CHARS])),
        "captionsRecovered": len(figures),
        "figures": figures,
        "images": [],
        "source": "api",
        "sourceDetail": detail,
    }


# ── Page slicing, for per-topic grounding ────────────────────────────────────

def page_index(markdown: str) -> list[tuple[int, int, int]]:
    """(page number, start offset, end offset) for each `<!-- page N -->` run."""
    marks = [(int(m.group(1)), m.start()) for m in _PAGE_MARKER.finditer(markdown)]
    if not marks:
        return []
    out = []
    for i, (page, start) in enumerate(marks):
        end = marks[i + 1][1] if i + 1 < len(marks) else len(markdown)
        out.append((page, start, end))
    return out


def excerpt_for_pages(markdown: str, start: Optional[int], end: Optional[int],
                      limit: int = TOPIC_EXCERPT_CHARS) -> str:
    """The chapter text for a page range, keeping the page markers.

    The markers are load-bearing twice over: the Concept section cites them as
    (Page N), and the validator uses the sliced text as the ONLY thing Concept
    bullets are allowed to be grounded in.
    """
    pages = page_index(markdown)
    if not pages or start is None:
        return markdown[:limit]
    lo, hi = int(start), int(end if end is not None else start)
    spans = [(s, e) for page, s, e in pages if lo <= page <= hi]
    if not spans:
        return markdown[:limit]
    return markdown[spans[0][0]:spans[-1][1]][:limit]


def proportional_excerpt(markdown: str, index: int, total: int,
                         limit: int = TOPIC_EXCERPT_CHARS, overlap: int = 400) -> str:
    """Topic `index` of `total`'s share of a chapter with no page markers.

    The last-resort grounding, and the alternative to something much worse:
    handing every topic in the chapter the same first 6000 characters. That looks
    like grounding, passes every check, and quietly makes forty Concept sections
    teach page one. A proportional slice is a guess, but it is a guess that moves
    through the chapter, and the overlap keeps a topic that straddles a boundary
    from losing half its material.
    """
    text = markdown or ""
    if total <= 1 or len(text) <= limit:
        return text[:limit]
    span = max(1, len(text) // total)
    start = max(0, span * (index - 1) - overlap)
    return text[start:start + max(span + 2 * overlap, 1200)][:limit]


def span_by_heading(markdown: str, heading: str) -> Optional[tuple[int, int]]:
    """(start, end) offsets of the section a heading owns — or None.

    Split out from excerpt_by_heading because the OFFSETS are the useful thing,
    not just the text. A topic anchored to "5. Let's play with match-sticks"
    knows what it teaches; where that lands in the book is then a fact to be
    looked up, not a number for a model to guess at. See pages_for_span.
    """
    if not heading.strip():
        return None
    pattern = re.compile(
        r"^(#{1,6})\s*" + re.escape(heading.strip()) + r"\s*$", re.M | re.I)
    match = pattern.search(markdown)
    if not match:
        loose = re.search(re.escape(heading.strip()[:60]), markdown, re.I)
        if not loose:
            return None
        return (loose.start(), min(len(markdown), loose.start() + TOPIC_EXCERPT_CHARS))
    level = len(match.group(1))
    rest = markdown[match.end():]
    nxt = re.search(rf"^#{{1,{level}}}\s+\S", rest, re.M)
    return (match.start(), match.end() + (nxt.start() if nxt else len(rest)))


def pages_for_span(markdown: str, start: int, end: int) -> tuple[Optional[int], Optional[int]]:
    """The page range a stretch of chapter actually sits on.

    The inversion this whole module needed: pages DERIVED from the text a topic
    was given, rather than a range invented alongside it. The two used to be
    independent, and on the Class 3 Maths chapter they disagreed — topic 6 was
    recorded as page 24 while its excerpt ran into 25 and 26, so every "(Page
    24)" on the sheet sent the teacher to the wrong page mid-lesson, and the
    grounding check compared the Concept section against pages the topic was
    never taught from.

    Any page whose own span overlaps the excerpt counts, so a topic straddling a
    page break reports both pages rather than only the one it started on.
    """
    pages = page_index(markdown)
    if not pages:
        return (None, None)
    hit = [page for page, s, e in pages if s < end and e > start]
    if not hit:
        return (None, None)
    return (min(hit), max(hit))


def excerpt_by_heading(markdown: str, heading: str, limit: int = TOPIC_EXCERPT_CHARS) -> str:
    """The stretch of chapter following a heading, up to the next same-or-higher
    heading. Used when the sequencing agent anchors a topic to a heading rather
    than a page — finer-grained than a page, since a page often holds two."""
    span = span_by_heading(markdown, heading)
    if not span:
        return ""
    return markdown[span[0]:span[1]][:limit]


# ── Competencies and activities ──────────────────────────────────────────────

async def resolve_knowledge(knowledge: dict, kinds: tuple = None,
                            capture_seeded: dict = None) -> dict:
    """Raw extracted names -> canonical library ids, for all four kinds.

    Runs in a thread: resolve_canonical is synchronous Supabase I/O and can make
    an LLM call of its own, and blocking the loop on it while forty topics wait
    is exactly the stall this graph is trying to avoid.

    SIDE EFFECT, same as the pilot route's: names the library has never seen are
    CREATED here. That is resolve_canonical's designed behaviour, not a leak.

    `capture_seeded`, if given a dict, is filled in place with {kind:
    {"created": [...], "matchedExisting": [...], "matchedViaLLM": [...]}} for
    every kind resolved — the provenance record of which of THIS topic's names
    actually grew the shared library versus which already existed. Optional and
    additive: omitting it changes nothing about what this function returns.

    Fails PER KIND, not all-or-nothing: `create_admin_client()`
    (supabase_clients.py) is a shared connection pool that several topics hit
    concurrently, and a transient `ConnectionTerminated` on, say, "contexts"
    used to take "concepts", "competencies" and "vocabulary" down with it too
    — resolve_canonical() already retries once on its own connection calls,
    but if even that fails, only the kind that actually failed comes back
    empty here, not the whole topic's canonical resolution.
    """
    # `kinds` exists so competencies can be left out. They are a join key onto
    # activity templates, and resolve_canonical's create-over-merge bias turns an
    # unmatched competency into an orphan row no template points at.
    selected = kinds or ("concepts", "competencies", "vocabulary", "contexts")

    def _resolve() -> dict:
        ac = create_admin_client()
        result = {}
        for kind in selected:
            kind_trace = {} if capture_seeded is not None else None
            try:
                result[kind] = resolve_canonical(ac, kind, knowledge.get(kind) or [], trace=kind_trace)
            except Exception as exc:
                print(f"[prep_flow:tools] canonical resolution failed for '{kind}': {exc}")
                result[kind] = {}
                kind_trace = None
            if capture_seeded is not None:
                capture_seeded[kind] = kind_trace or {}
        return result

    try:
        return await asyncio.to_thread(_resolve)
    except Exception as exc:
        # _resolve() itself only reaches here for something outside the per-kind
        # loop above (asyncio.to_thread plumbing, create_admin_client() itself
        # raising) — genuinely all-or-nothing, unlike a single kind's failure.
        print(f"[prep_flow:tools] canonical resolution failed: {exc}")
        return {kind: {} for kind in selected}


_MAP_PROMPT = """You are matching topic-specific teaching skills onto a fixed library of general
pedagogical competencies. The library is a controlled vocabulary — you may not
add to it, only match against it.

THE LIBRARY ({count} competencies):
{library}

SKILLS EXTRACTED FROM THE TEXTBOOK, grouped by topic:
{skills}

For each extracted skill, list every library competency that a lesson teaching
that skill would genuinely have students PRACTISE.

- MATCH GENEROUSLY. The library is deliberately general and the extracted skills
  are deliberately specific; they will almost never be worded alike. "Identify
  family members" is practised by "Describe people" and by "Observe Objects".
  "Construct a family tree" is practised by "Sequence events" and "Organize
  ideas". Wording similarity is not the test — whether the children would be
  doing that thing is.
- Up to 3 library competencies per skill, best first. Matching everything to
  everything is as useless as matching nothing.
- Return an empty list ONLY when a lesson on that skill would practise nothing in
  the library at all. That should be rare.
- Copy library names EXACTLY as written above.

Return ONLY valid JSON, no markdown fences:
{{
  "mappings": [
    {{"topic": 1, "skill": "Identify family members", "library": ["Describe people", "Observe Objects"]}}
  ]
}}
"""

# The whole library goes into the prompt, so it has to stay printable. 241 names
# is ~1k tokens; well past this the mapping call needs pre-filtering (by subject
# or grade band) rather than a bigger prompt.
MAX_LIBRARY_FOR_MAPPING = env_int("PREP_FLOW_MAX_MAPPING_LIBRARY", 600)


async def map_to_library_competencies(extracted: dict, grade=None) -> dict:
    """Map each topic's extracted competencies onto EXISTING library competencies.

    This is the deliberate opposite of `resolve_canonical`, and the difference is
    the whole point. That function is biased toward CREATING a new entry over
    merging two that might not be the same thing — correct for concepts, where a
    duplicate is a minor annoyance. It is wrong here, because competencies are a
    JOIN KEY: an unmatched competency does not cost a duplicate row, it costs
    every activity template the topic could have used.

    Observed: a Class 3 EVS topic extracted "Identify family members",
    "Construct a family tree" and three siblings. None matched, so the topic drew
    0 of 581 templates — while "Describe people", "Observe Objects" and
    "Sequence events" sat in the library with seven runnable activities between
    them.

    Read-only by design. It never creates a competency, so running generation
    over a whole book cannot bury 241 well-linked competencies under thousands of
    topic-specific orphans that no template will ever point at.

    Returns {topic_index: {"ids": [...], "matched": {...}, "unmatched": [...]}}.
    """
    wanted = {int(i): [n for n in (names or []) if isinstance(n, str) and n.strip()]
              for i, names in (extracted or {}).items()}
    if not any(wanted.values()):
        return {i: {"ids": [], "matched": {}, "unmatched": []} for i in wanted}

    def _library() -> list:
        return (create_admin_client().table("competencies")
                .select("id, name, aliases").limit(MAX_LIBRARY_FOR_MAPPING)
                .execute().data or [])

    try:
        library = await asyncio.to_thread(_library)
    except Exception as exc:
        print(f"[prep_flow:tools] could not read the competency library: {exc}")
        return {i: {"ids": [], "matched": {}, "unmatched": names}
                for i, names in wanted.items()}

    by_lower: dict[str, dict] = {}
    for row in library:
        by_lower[(row.get("name") or "").strip().lower()] = row
        for alias in (row.get("aliases") or []):
            by_lower.setdefault((alias or "").strip().lower(), row)

    out = {i: {"ids": [], "matched": {}, "unmatched": []} for i in wanted}
    needs_llm: dict[int, list[str]] = {}
    for index, names in wanted.items():
        for name in names:
            hit = by_lower.get(name.strip().lower())
            if hit:
                out[index]["ids"].append(hit["id"])
                out[index]["matched"][name] = [hit["name"]]
            else:
                needs_llm.setdefault(index, []).append(name)

    if needs_llm and library:
        # ONE call for the whole chapter, not one per topic. Forty topics of
        # per-topic mapping would re-send the entire library forty times.
        skills = "\n".join(
            f"  topic {index}: " + "; ".join(names)
            for index, names in sorted(needs_llm.items()))
        try:
            data = await call_json(
                _MAP_PROMPT.format(
                    count=len(library),
                    library="\n".join(f"  - {r['name']}" for r in library),
                    skills=skills),
                label="competency-mapping", required=("mappings",),
                temperature=0.1, max_tokens=4000)
        except Exception as exc:
            print(f"[prep_flow:tools] competency mapping failed: {exc}")
            for index, names in needs_llm.items():
                out[index]["unmatched"] = names
            return out

        claimed: dict[int, set] = {}
        for entry in data.get("mappings") or []:
            if not isinstance(entry, dict):
                continue
            try:
                index = int(entry.get("topic"))
            except (TypeError, ValueError):
                continue
            if index not in out:
                continue
            skill = (entry.get("skill") or "").strip()
            names = [n for n in (entry.get("library") or []) if isinstance(n, str)][:3]
            resolved = []
            for name in names:
                hit = by_lower.get(name.strip().lower())
                if hit:
                    resolved.append(hit)
                else:
                    # A name the model invented rather than copied. Dropped
                    # silently would look like a legitimate no-match.
                    print(f"[prep_flow:tools] mapping returned '{name}', which is "
                          f"not in the library — ignoring")
            if resolved:
                out[index]["matched"][skill] = [r["name"] for r in resolved]
                out[index]["ids"] += [r["id"] for r in resolved]
                claimed.setdefault(index, set()).add(skill)

        for index, names in needs_llm.items():
            out[index]["unmatched"] = [n for n in names
                                       if n not in claimed.get(index, set())]

    for index in out:
        out[index]["ids"] = sorted(set(out[index]["ids"]))
    return out


async def pedagogy_activities(competency_ids: list, grade, resource_level: int) -> list:
    if not competency_ids:
        return []

    def _lookup() -> list:
        ac = create_admin_client()
        return get_recommended_activities(
            ac, competency_ids, resource_level=resource_level,
            grade_band=grade_band_for(grade),
        )

    try:
        return await asyncio.to_thread(_lookup)
    except Exception as exc:
        print(f"[prep_flow:tools] pedagogy lookup failed: {exc}")
        return []


async def class_context(class_id: Optional[str], teacher_id: Optional[str],
                        chapter_title: str = "") -> dict:
    """Who this chapter is being generated for: interests, weak topics, the
    teaching profile, and any per-class feedback already on record. Best-effort
    throughout — a chapter still generates for a class with no history."""
    if not class_id:
        return {}

    def _gather() -> dict:
        # gather_class_context/fetch_feedback_context are `async def` but do
        # only blocking Supabase I/O. asyncio.run is safe here precisely because
        # this runs on a worker thread that has no loop of its own — awaiting
        # them on the main loop instead would stall every other topic.
        ac = create_admin_client()
        base = asyncio.run(
            gather_class_context(ac, class_id, teacher_id, exclude_topic=chapter_title))
        feedback = asyncio.run(fetch_feedback_context(ac, class_id, chapter_title))
        profile = base.get("teachingProfile") or {}
        return {
            "totalStudents": base.get("totalStudents"),
            "classInterests": base.get("classInterests") or [],
            "weakTopics": base.get("weakTopics") or [],
            "personalization": personalization_tier_line(profile.get("personalization")),
            "topicInsight": feedback.get("topicInsight"),
            "classProfile": feedback.get("classProfile"),
        }

    try:
        return await asyncio.to_thread(_gather)
    except Exception as exc:
        print(f"[prep_flow:tools] class context failed: {exc}")
        return {}


async def adaptive_state(school_id: Optional[str], grade: str, subject: str) -> dict:
    """The Feedback Optimization Agent's output, read at Context Assembly time.

    This single call is the closing of the loop in the diagram: everything the
    telemetry/pattern/optimization side learned arrives here and nowhere else.
    """
    return await db.fetch_active_adaptive_state(db.scope_key(school_id, grade, subject))


# ── Curriculum reasoning cache ───────────────────────────────────────────────

async def cached_reasoning(key: str) -> dict:
    """Reasoning already derived for this chapter, or {} to derive it.

    Distinct from `adaptive_state` above in what it is scoped to, and that is the
    whole reason it caches: adaptive state is per cohort and changes as teachers
    respond, while reasoning is a property of the textbook, the grade and the
    subject. Re-deriving it for a second class of the same grade would spend a
    call to get the same answer.
    """
    return await db.fetch_reasoning(key)


async def store_reasoning(key: str, *, grade: str, subject: str, chapter_title: str,
                          chapter_number: Optional[int], reasoning: dict) -> None:
    """Best-effort. A failed write means the next run derives it again."""
    await db.save_reasoning(key, grade=grade, subject=subject,
                            chapter_title=chapter_title, chapter_number=chapter_number,
                            reasoning=reasoning)
