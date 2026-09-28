"""Mirror of the published-textbook service, and the bridge from a school's
syllabus to the actual pages of the child's book.

Two jobs:

1. **Sync** — keep textbook_catalog_books/_chapters current (new books get
   published there regularly). Catalog metadata is cheap and refreshed often;
   chapter prose is bulk and fetched lazily the first time a chapter is
   actually taught.

2. **Link** — answer "which chapter of which book is this syllabus topic?"
   That is the whole integration. Everything else is plumbing.

The linking is not as simple as it looks, for two reasons found in the real
data:

  * Subject names don't match. A school's syllabus says `science`; the book
    is `environmental_studies`. Hence SUBJECT_ALIASES.
  * Page numbers don't match. syllabus_chapters says chapter 2 starts on
    page 23; the printed book says 25 — the syllabus was extracted from a
    PDF whose pagination is offset by the front matter. So chapter_number is
    the join key (verified against title), and page offset is *computed*
    from the pair rather than assumed to be zero.
"""
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

import httpx

from .supabase_clients import create_admin_client

API_BASE = "https://eduteach-textbook-api.onrender.com"

# Render free tier sleeps; a cold start is 30-60s. Generous on the sync path
# (a background job can wait), and callers on the teacher-facing path read
# from the mirror instead of ever paying this.
SYNC_TIMEOUT_S = 90.0

# How stale cached chapter prose may be before a re-fetch. Published chapters
# effectively never change once reviewed, so this is long.
CONTENT_MAX_AGE_DAYS = 30

# The school's syllabus vocabulary -> the textbook service's vocabulary.
# Keyed on the lowercased syllabus subject.
SUBJECT_ALIASES = {
    "science": "environmental_studies",
    "evs": "environmental_studies",
    "environmental science": "environmental_studies",
    "environmental studies": "environmental_studies",
    "maths": "maths",
    "mathematics": "maths",
    "math": "maths",
    "english": "english",
    "telugu": "telugu",
    "hindi": "hindi",
    "social": "social_studies",
    "social studies": "social_studies",
}

DEFAULT_BOARD = "ts_scert"
DEFAULT_LANGUAGE = "en"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _table_missing(err: Exception) -> bool:
    text = str(err).lower()
    return "could not find the table" in text or "does not exist" in text or "pgrst205" in text


def resolve_book_id(grade, subject: str, board: str = DEFAULT_BOARD, language: str = DEFAULT_LANGUAGE) -> str:
    """Rebuild the service's deterministic id: {board}_class{grade}_{subject}_{language}."""
    subj = SUBJECT_ALIASES.get((subject or "").strip().lower(), (subject or "").strip().lower().replace(" ", "_"))
    return f"{board}_class{str(grade).strip()}_{subj}_{language}"


# ── sync ────────────────────────────────────────────────────────────────────

def sync_catalog(ac=None) -> dict:
    """Refresh the book list and every book's chapter list. Metadata only —
    no chapter prose — so this stays cheap enough to run on a schedule."""
    ac = ac or create_admin_client()
    try:
        with httpx.Client(timeout=SYNC_TIMEOUT_S) as client:
            books = client.get(f"{API_BASE}/published/books").json()
    except Exception as e:
        return {"ok": False, "error": f"catalog fetch failed: {e}", "books": 0, "chapters": 0}

    if not isinstance(books, list):
        return {"ok": False, "error": "unexpected catalog shape", "books": 0, "chapters": 0}

    book_rows, chapter_rows = [], []
    for b in books:
        book_id = b.get("book_id")
        if not book_id:
            continue
        book_rows.append({
            "book_id": book_id, "board": b.get("board"), "grade": str(b.get("grade") or ""),
            "subject": b.get("subject"), "language": b.get("language"),
            "total_chapters": b.get("total_chapters"), "chapters_published": b.get("chapters_published"),
            "synced_at": _now(),
        })
        try:
            with httpx.Client(timeout=SYNC_TIMEOUT_S) as client:
                chapters = client.get(f"{API_BASE}/published/books/{book_id}/chapters").json()
        except Exception as e:
            print(f"[textbook_catalog] chapter list failed for {book_id}: {e}")
            continue
        for c in chapters or []:
            if c.get("chapter_number") is None:
                continue
            chapter_rows.append({
                "book_id": book_id, "chapter_number": c["chapter_number"],
                "chapter_title": c.get("chapter_title"),
                "page_start": c.get("page_start"), "page_end": c.get("page_end"),
                "synced_at": _now(),
            })

    try:
        if book_rows:
            ac.table("textbook_catalog_books").upsert(book_rows, on_conflict="book_id").execute()
        if chapter_rows:
            # Deliberately does not touch content/content_synced_at — a
            # metadata refresh must not throw away prose already cached.
            ac.table("textbook_catalog_chapters").upsert(chapter_rows, on_conflict="book_id,chapter_number").execute()
    except Exception as e:
        if _table_missing(e):
            return {"ok": False, "error": "textbook_catalog tables missing — apply migration 0009", "books": 0, "chapters": 0}
        return {"ok": False, "error": str(e), "books": 0, "chapters": 0}

    return {"ok": True, "books": len(book_rows), "chapters": len(chapter_rows), "syncedAt": _now()}


def find_chapter_for_image(image_id: str, ac=None) -> Optional[dict]:
    """Which (book_id, chapter_number) an image_id belongs to, by scanning the
    cached `images` column every mirrored chapter carries. Needed because
    `image_id` alone (e.g. "img_c5ch2_04") doesn't say which book/chapter it's
    from, and resolve_image_url below needs that to re-fetch a live URL."""
    ac = ac or create_admin_client()
    try:
        rows = (
            ac.table("textbook_catalog_chapters").select("book_id, chapter_number, images")
            .not_.is_("images", "null").execute().data or []
        )
    except Exception as e:
        if not _table_missing(e):
            print(f"[textbook_catalog] find_chapter_for_image failed: {e}")
        return None
    for r in rows:
        if any((i or {}).get("imageId") == image_id for i in (r.get("images") or [])):
            return {"bookId": r["book_id"], "chapterNumber": r["chapter_number"]}
    return None


def resolve_image_url(image_id: str, ac=None) -> Optional[dict]:
    """A fresh, live URL for one image from the source API — never served from
    the cache, since these are the API's own signed Supabase Storage URLs and
    expire a few hours after being issued (see api_figures' module docstring
    for where that was first observed). Re-fetching the whole chapter on every
    image request is deliberately not optimised away: images are viewed far
    less often than chapters are read, and correctness (never a stale link)
    matters more here than one extra request.

    Returns {"url": ..., "caption": ...} or None if the image can't be found
    or the source API is unreachable right now.
    """
    ac = ac or create_admin_client()
    where = find_chapter_for_image(image_id, ac)
    if not where:
        return None
    try:
        with httpx.Client(timeout=SYNC_TIMEOUT_S) as client:
            data = client.get(
                f"{API_BASE}/published/books/{where['bookId']}/chapters/{where['chapterNumber']}"
            ).json()
    except Exception as e:
        print(f"[textbook_catalog] resolve_image_url live fetch failed: {e}")
        return None
    for i in (data.get("images") or []):
        if i.get("image_id") == image_id and i.get("url"):
            return {"url": i["url"], "caption": i.get("caption")}
    return None


def get_chapter_content(book_id: str, chapter_number: int, ac=None, force: bool = False) -> Optional[dict]:
    """Cached chapter prose. Fetches from the service only on a miss or when
    the cache is older than CONTENT_MAX_AGE_DAYS, so the teacher-facing path
    normally never touches the network."""
    ac = ac or create_admin_client()
    try:
        row = (
            ac.table("textbook_catalog_chapters").select("*")
            .eq("book_id", book_id).eq("chapter_number", chapter_number)
            .maybe_single().execute()
        )
        row = row.data if row else None
    except Exception as e:
        if _table_missing(e):
            print("[textbook_catalog] tables missing — apply migration 0009")
            return None
        print(f"[textbook_catalog] chapter read failed: {e}")
        return None

    if not row:
        return None

    fresh = False
    if row.get("content") and row.get("content_synced_at") and not force:
        try:
            when = datetime.fromisoformat(str(row["content_synced_at"]).replace("Z", "+00:00"))
            fresh = datetime.now(timezone.utc) - when < timedelta(days=CONTENT_MAX_AGE_DAYS)
        except ValueError:
            fresh = False
    if fresh:
        return row

    try:
        with httpx.Client(timeout=SYNC_TIMEOUT_S) as client:
            data = client.get(f"{API_BASE}/published/books/{book_id}/chapters/{chapter_number}").json()
    except Exception as e:
        print(f"[textbook_catalog] content fetch failed for {book_id}/{chapter_number}: {e}")
        # Stale cached prose beats no prose at all.
        return row if row.get("content") else None

    # Image URLs are signed and expire — keep only what grounding uses.
    images = [
        {
            "imageId": i.get("image_id"), "caption": i.get("caption"),
            "usage": i.get("usage"), "orderIndex": i.get("order_index"),
        }
        for i in (data.get("images") or [])
    ]
    update = {
        "content": data.get("content"), "images": images,
        "chapter_title": data.get("chapter_title") or row.get("chapter_title"),
        "page_start": data.get("page_start") or row.get("page_start"),
        "page_end": data.get("page_end") or row.get("page_end"),
        "content_synced_at": _now(),
    }
    try:
        ac.table("textbook_catalog_chapters").update(update).eq("id", row["id"]).execute()
    except Exception as e:
        print(f"[textbook_catalog] content cache write failed: {e}")
    return {**row, **update}


# ── linking ─────────────────────────────────────────────────────────────────

def _norm_title(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def find_textbook_chapter(grade, subject: str, chapter_number: Optional[int], title: Optional[str], ac=None) -> Optional[dict]:
    """Map a syllabus chapter onto a textbook chapter.

    chapter_number is the join key; the title is a sanity check, not a
    requirement, so a school that renamed "Let's Grow Trees" slightly still
    links. Returns None rather than guessing when the book isn't in the
    mirror — ungrounded is a fine outcome, wrongly grounded is not.
    """
    ac = ac or create_admin_client()
    book_id = resolve_book_id(grade, subject)
    try:
        book = ac.table("textbook_catalog_books").select("book_id").eq("book_id", book_id).maybe_single().execute()
        if not book or not book.data:
            return None
    except Exception as e:
        if not _table_missing(e):
            print(f"[textbook_catalog] book lookup failed: {e}")
        return None

    try:
        rows = ac.table("textbook_catalog_chapters").select("*").eq("book_id", book_id).execute().data or []
    except Exception as e:
        print(f"[textbook_catalog] chapter lookup failed: {e}")
        return None
    if not rows:
        return None

    if chapter_number is not None:
        hit = next((r for r in rows if r["chapter_number"] == chapter_number), None)
        if hit:
            return hit

    if title:
        want = _norm_title(title)
        hit = next((r for r in rows if _norm_title(r.get("chapter_title")) == want), None)
        if hit:
            return hit
        hit = next((r for r in rows if want and (want in _norm_title(r.get("chapter_title")) or _norm_title(r.get("chapter_title")) in want)), None)
        if hit:
            return hit
    return None


def slice_pages(content: str, page_start: Optional[int], page_end: Optional[int]) -> str:
    """Cut a chapter down to a page range using its own `<!-- page N -->`
    markers. A chapter runs ~11k words (~15k tokens) and covers several
    syllabus topics, so sending all of it for one topic is both expensive
    and less focused than sending the right three pages."""
    if not content or page_start is None:
        return content or ""
    end = page_end if page_end is not None else page_start

    parts = re.split(r"<!--\s*page\s+(\d+)\s*-->", content)
    if len(parts) < 3:
        return content

    kept = []
    # parts = [pre, "25", body, "26", body, ...]
    for i in range(1, len(parts) - 1, 2):
        try:
            page_no = int(parts[i])
        except ValueError:
            continue
        if page_start <= page_no <= end:
            kept.append(f"<!-- page {page_no} -->{parts[i + 1]}")
    return "".join(kept).strip() or content


def grounding_for_syllabus_chapter(
    grade, subject: str, chapter_number: Optional[int], chapter_title: Optional[str],
    syllabus_page_start: Optional[int] = None,
    topic_page_start: Optional[int] = None, topic_page_end: Optional[int] = None,
    ac=None,
) -> Optional[dict]:
    """The one function prep-material generation calls.

    Returns the real text of the book for this topic, or None if this book
    isn't mirrored yet.

    Handles the page-offset problem: syllabus_chapters.page_start came from a
    PDF that includes front matter, so it can disagree with the printed page
    number for the same chapter. The difference between the two is the
    offset, and it's applied to the topic's page range before slicing —
    otherwise a topic on printed pages 27-29 would be cut from pages 25-27
    and quietly return the wrong section.
    """
    ac = ac or create_admin_client()
    chapter = find_textbook_chapter(grade, subject, chapter_number, chapter_title, ac)
    if not chapter:
        return None

    full = get_chapter_content(chapter["book_id"], chapter["chapter_number"], ac)
    if not full or not full.get("content"):
        return None

    content = full["content"]
    used_pages = None

    if topic_page_start is not None and full.get("page_start") is not None:
        offset = 0
        if syllabus_page_start is not None:
            offset = full["page_start"] - syllabus_page_start
        start = topic_page_start + offset
        end = (topic_page_end + offset) if topic_page_end is not None else start
        sliced = slice_pages(content, start, end)
        if sliced and sliced != content:
            content = sliced
            used_pages = {"start": start, "end": end, "offsetApplied": offset}

    return {
        "bookId": chapter["book_id"],
        "chapterNumber": chapter["chapter_number"],
        "chapterTitle": full.get("chapter_title") or chapter.get("chapter_title"),
        "pageStart": full.get("page_start"),
        "pageEnd": full.get("page_end"),
        "text": content,
        "images": full.get("images") or [],
        "slicedTo": used_pages,
    }
