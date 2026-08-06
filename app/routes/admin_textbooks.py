"""Admin routes over ingested textbook content -- the chapters produced by the
pdf/ pipeline and stored by lib/textbook_persist.py.

Mounted under /api/admin/schools, so the paths are:
    GET    /api/admin/schools/{schoolId}/textbooks
    GET    /api/admin/schools/{schoolId}/textbooks/{bookId}/chapters
    GET    /api/admin/schools/{schoolId}/textbooks/chapters/{chapterId}
    PATCH  /api/admin/schools/{schoolId}/textbooks/chapters/{chapterId}/published
    POST   /api/admin/schools/{schoolId}/textbooks/{bookId}/published
    DELETE /api/admin/schools/{schoolId}/textbooks/{bookId}
All guarded by require_admin (path :schoolId must match the caller's school).

The review step these exist for is not ceremony. Chapter boundaries are
detected from the PDF's outline or, failing that, from font size; a mis-split
book produces chapters that are individually valid and collectively wrong, and
every automated gate passes them because no text was lost -- it just landed in
the wrong chapter. So ingestion writes rows unpublished, an admin reads the
split, and only then does anything become servable.
"""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel

from ..deps import require_admin
from ..lib.supabase_clients import create_admin_client
from ..lib.textbook_persist import BUCKET

router = APIRouter()

# Long enough for a lesson, short enough that a leaked URL stops working before
# it is worth passing around. Textbook scans are licensed material.
SIGNED_URL_TTL_SECONDS = 60 * 60


def _book_or_404(ac, school_id: str, book_id: str) -> dict:
    row = (
        ac.table("textbook_books").select("*")
        .eq("id", book_id).eq("school_id", school_id)
        .maybe_single().execute()
    )
    if not row or not row.data:
        raise HTTPException(status_code=404, detail="Book not found")
    return row.data


def _chapter_or_404(ac, school_id: str, chapter_id: str, columns: str) -> dict:
    row = (
        ac.table("textbook_chapters").select(columns)
        .eq("id", chapter_id).eq("school_id", school_id)
        .maybe_single().execute()
    )
    if not row or not row.data:
        raise HTTPException(status_code=404, detail="Chapter not found")
    return row.data


@router.get("/{schoolId}/textbooks")
def list_textbooks(schoolId: str, admin: dict = Depends(require_admin)):
    """Every ingested book, with how much of it has been reviewed."""
    ac = create_admin_client()
    books = (
        ac.table("textbook_books").select("*")
        .eq("school_id", schoolId)
        .order("grade").order("subject")
        .execute().data or []
    )
    if not books:
        return {"books": []}

    chapters = (
        ac.table("textbook_chapters")
        .select("book_uuid, published")
        .in_("book_uuid", [b["id"] for b in books])
        .execute().data or []
    )
    by_book: dict[str, list[dict]] = {}
    for chapter in chapters:
        by_book.setdefault(chapter["book_uuid"], []).append(chapter)

    return {
        "books": [
            {
                "id": book["id"],
                "bookId": book["book_id"],
                "board": book["board"],
                "grade": book["grade"],
                "subject": book["subject"],
                "language": book["language"],
                "sourcePdf": book["source_pdf"],
                "sourcePdfSha256": book["source_pdf_sha256"],
                "chapterCount": len(by_book.get(book["id"], [])),
                "publishedCount": sum(
                    1 for c in by_book.get(book["id"], []) if c["published"]
                ),
                "createdAt": book["created_at"],
                "updatedAt": book["updated_at"],
            }
            for book in books
        ]
    }


@router.get("/{schoolId}/textbooks/{bookId}/chapters")
def list_chapters(schoolId: str, bookId: str, admin: dict = Depends(require_admin)):
    """The chapter list for the review panel.

    content_markdown is deliberately not selected: it is ~25KB per chapter and
    a sixteen-chapter book would make this a 400KB response to render a list of
    titles. The per-chapter endpoint returns it.
    """
    ac = create_admin_client()
    book = _book_or_404(ac, schoolId, bookId)
    chapters = (
        ac.table("textbook_chapters")
        .select(
            "id, chapter_number, chapter_title, page_start, page_end, published, "
            "published_at, content_sha256, parser, generated_at, topics, images_manifest"
        )
        .eq("book_uuid", bookId).order("chapter_number")
        .execute().data or []
    )

    def summarise(chapter: dict) -> dict:
        images = chapter.get("images_manifest") or []
        topics = chapter.get("topics") or []
        return {
            "id": chapter["id"],
            "chapterNumber": chapter["chapter_number"],
            "chapterTitle": chapter["chapter_title"],
            "pageStart": chapter["page_start"],
            "pageEnd": chapter["page_end"],
            "published": chapter["published"],
            "publishedAt": chapter["published_at"],
            "contentSha256": chapter["content_sha256"],
            "parser": chapter["parser"],
            "generatedAt": chapter["generated_at"],
            "topicCount": len(topics),
            "topics": [{"id": t["id"], "title": t["title"]} for t in topics],
            "illustrationCount": sum(1 for i in images if not i.get("decorative")),
            "captionedCount": sum(
                1 for i in images if not i.get("decorative") and i.get("caption")
            ),
            "decorativeCount": sum(1 for i in images if i.get("decorative")),
        }

    return {
        "book": {
            "id": book["id"],
            "bookId": book["book_id"],
            "board": book["board"],
            "grade": book["grade"],
            "subject": book["subject"],
            "language": book["language"],
            "sourcePdf": book["source_pdf"],
        },
        "chapters": [summarise(c) for c in chapters],
    }


@router.get("/{schoolId}/textbooks/chapters/{chapterId}")
def get_chapter(
    schoolId: str,
    chapterId: str,
    includeImages: bool = Query(True, description="Sign URLs for the illustrations."),
    admin: dict = Depends(require_admin),
):
    """One chapter, whole -- markdown, topic index, and signed image URLs.

    The markdown comes back exactly as stored. Anything that rewrites it before
    a tutor sees it belongs on the serving side, where it can be applied
    identically for every request; done here it would vary per caller, and a
    prefix that varies per caller is not a cached prefix.
    """
    ac = create_admin_client()
    chapter = _chapter_or_404(ac, schoolId, chapterId, "*")

    images = []
    if includeImages:
        rows = (
            ac.table("textbook_images").select("*")
            .eq("chapter_uuid", chapterId).order("order_index")
            .execute().data or []
        )
        signed = _sign(ac, [r["storage_path"] for r in rows])
        images = [
            {
                "imageId": row["image_id"],
                "sourcePage": row["source_page"],
                "caption": row["caption"],
                "width": row["width"],
                "height": row["height"],
                "decorative": row["decorative"],
                "url": signed.get(row["storage_path"]),
            }
            for row in rows
        ]

    return {
        "id": chapter["id"],
        "bookId": chapter["book_uuid"],
        "chapterNumber": chapter["chapter_number"],
        "chapterTitle": chapter["chapter_title"],
        "pageStart": chapter["page_start"],
        "pageEnd": chapter["page_end"],
        "published": chapter["published"],
        "contentMarkdown": chapter["content_markdown"],
        "contentSha256": chapter["content_sha256"],
        "topics": chapter["topics"],
        "images": images,
        "parser": chapter["parser"],
        "generatedAt": chapter["generated_at"],
    }


def _sign(ac, paths: list[str]) -> dict[str, Optional[str]]:
    """Signed URLs keyed by storage path.

    Falls back to signing one at a time: create_signed_urls fails the whole
    batch if any single object is missing, and one absent illustration should
    cost that illustration, not the chapter.
    """
    if not paths:
        return {}
    try:
        results = ac.storage.from_(BUCKET).create_signed_urls(paths, SIGNED_URL_TTL_SECONDS)
        return {
            item.get("path"): item.get("signedURL") or item.get("signedUrl")
            for item in results or []
        }
    except Exception:  # noqa: BLE001 -- fall back rather than lose the chapter
        signed: dict[str, Optional[str]] = {}
        for path in paths:
            try:
                result = ac.storage.from_(BUCKET).create_signed_url(
                    path, SIGNED_URL_TTL_SECONDS
                )
                signed[path] = result.get("signedURL") or result.get("signedUrl")
            except Exception:  # noqa: BLE001
                signed[path] = None
        return signed


class PublishedBody(BaseModel):
    published: bool


def _published_patch(published: bool) -> dict:
    return {
        "published": published,
        "published_at": datetime.now(timezone.utc).isoformat() if published else None,
    }


@router.patch("/{schoolId}/textbooks/chapters/{chapterId}/published")
def set_chapter_published(
    schoolId: str, chapterId: str, body: PublishedBody, admin: dict = Depends(require_admin)
):
    ac = create_admin_client()
    _chapter_or_404(ac, schoolId, chapterId, "id")
    ac.table("textbook_chapters").update(_published_patch(body.published)).eq(
        "id", chapterId
    ).execute()
    return {"id": chapterId, "published": body.published}


@router.post("/{schoolId}/textbooks/{bookId}/published")
def set_book_published(
    schoolId: str, bookId: str, body: PublishedBody, admin: dict = Depends(require_admin)
):
    """Publish or unpublish every chapter of a book at once.

    Offered because reviewing sixteen chapters and then clicking sixteen
    toggles is how the sixteenth stops getting reviewed.
    """
    ac = create_admin_client()
    _book_or_404(ac, schoolId, bookId)
    ac.table("textbook_chapters").update(_published_patch(body.published)).eq(
        "book_uuid", bookId
    ).execute()
    count = (
        ac.table("textbook_chapters").select("id", count="exact")
        .eq("book_uuid", bookId).execute().count or 0
    )
    return {"bookId": bookId, "published": body.published, "chapters": count}


@router.delete("/{schoolId}/textbooks/{bookId}")
def delete_book(schoolId: str, bookId: str, admin: dict = Depends(require_admin)):
    """Remove a book, its chapters, and its assets.

    Storage is emptied before the rows go: the paths are only discoverable
    through them, and objects orphaned in a bucket are invisible until the bill
    arrives.
    """
    ac = create_admin_client()
    book = _book_or_404(ac, schoolId, bookId)

    chapter_ids = [
        c["id"]
        for c in (
            ac.table("textbook_chapters").select("id")
            .eq("book_uuid", bookId).execute().data or []
        )
    ]
    paths = [
        row["storage_path"]
        for row in (
            ac.table("textbook_images").select("storage_path")
            .in_("chapter_uuid", chapter_ids).execute().data or []
        )
    ] if chapter_ids else []
    if paths:
        try:
            ac.storage.from_(BUCKET).remove(paths)
        except Exception:  # noqa: BLE001 -- the rows still have to go
            pass

    # textbook_chapters/topics/images cascade from the book row.
    ac.table("textbook_books").delete().eq("id", bookId).execute()
    return {"deleted": bookId, "bookId": book["book_id"], "assetsRemoved": len(paths)}
