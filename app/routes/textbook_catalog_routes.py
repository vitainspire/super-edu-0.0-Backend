"""Admin routes over the mirrored textbook catalog (see
app/lib/textbook_catalog.py). Mounted under /api/admin/schools:

    POST /api/admin/schools/{schoolId}/textbook-catalog/sync
    GET  /api/admin/schools/{schoolId}/textbook-catalog
    GET  /api/admin/schools/{schoolId}/textbook-catalog/coverage

The catalog is global content, not per-school — every school reads the same
published books — but the routes are school-scoped so they sit behind the
same require_admin gate as everything else an admin touches, rather than
inventing a second auth path for one feature.
"""
import time
from fastapi import APIRouter, Depends, HTTPException, Request

from ..lib.supabase_clients import create_admin_client
from ..lib.textbook_catalog import sync_catalog, resolve_book_id
from ..lib.logger import api_log, get_client_ip
from ..deps import require_admin

router = APIRouter()


@router.post("/{schoolId}/textbook-catalog/sync")
def post_sync(schoolId: str, request: Request, admin: dict = Depends(require_admin)):
    """Refresh the book and chapter lists from the publishing service. Safe
    to run repeatedly — it upserts, and deliberately leaves already-cached
    chapter prose alone. Run it whenever new books are published; a nightly
    schedule is the intended cadence."""
    ip = get_client_ip(request)
    t0 = time.time()
    result = sync_catalog()
    api_log("textbook-catalog-sync", ip, (time.time() - t0) * 1000, False,
            "ok" if result.get("ok") else "error", error=result.get("error"))
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("error") or "Sync failed")
    return result


@router.get("/{schoolId}/textbook-catalog")
def get_catalog(schoolId: str, admin: dict = Depends(require_admin)):
    """What's mirrored right now, and how much of each book has its prose
    cached — the answer to "is this book ready to ground lessons with?"."""
    ac = create_admin_client()
    try:
        books = ac.table("textbook_catalog_books").select("*").order("grade").order("subject").execute().data or []
        chapters = ac.table("textbook_catalog_chapters").select("book_id, chapter_number, content_synced_at").execute().data or []
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Catalog unavailable — apply migration 0009? ({e})")

    cached_by_book: dict = {}
    for c in chapters:
        entry = cached_by_book.setdefault(c["book_id"], {"chapters": 0, "withText": 0})
        entry["chapters"] += 1
        if c.get("content_synced_at"):
            entry["withText"] += 1

    return {
        "books": [
            {**b, "mirroredChapters": cached_by_book.get(b["book_id"], {}).get("chapters", 0),
             "chaptersWithText": cached_by_book.get(b["book_id"], {}).get("withText", 0)}
            for b in books
        ]
    }


@router.get("/{schoolId}/textbook-catalog/coverage")
def get_coverage(schoolId: str, admin: dict = Depends(require_admin)):
    """Which of this school's own syllabus chapters can actually be grounded.

    The useful question isn't "how many books exist" but "when a teacher of
    Grade 5 Science opens a topic, will the lesson be grounded or invented?"
    Anything listed as unmatched is a lesson that will still be generated
    from the model's general knowledge.
    """
    ac = create_admin_client()
    try:
        syllabus = (
            ac.table("syllabus_chapters")
            .select("grade, subject, chapter_number, title")
            .eq("school_id", schoolId).order("grade").order("chapter_number")
            .execute().data or []
        )
        mirrored = ac.table("textbook_catalog_chapters").select("book_id, chapter_number").execute().data or []
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"Coverage unavailable — apply migration 0009? ({e})")

    have = {(m["book_id"], m["chapter_number"]) for m in mirrored}
    matched, unmatched = [], []
    for s in syllabus:
        book_id = resolve_book_id(s.get("grade"), s.get("subject"))
        row = {"grade": s.get("grade"), "subject": s.get("subject"),
               "chapterNumber": s.get("chapter_number"), "title": s.get("title"), "bookId": book_id}
        (matched if (book_id, s.get("chapter_number")) in have else unmatched).append(row)

    total = len(matched) + len(unmatched)
    return {
        "grounded": len(matched), "ungrounded": len(unmatched), "total": total,
        "percent": round(len(matched) / total * 100) if total else 0,
        "matched": matched, "unmatched": unmatched,
    }
