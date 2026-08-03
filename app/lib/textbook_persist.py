"""Persist one built chapter -- the pdf/ pipeline's chapter.json plus its
assets/ -- into textbook_books / textbook_chapters / textbook_topics /
textbook_images and the textbook-assets bucket.

See db/migrations/022_textbook_content.sql for the schema and for why this is
not a second copy of syllabus_*.

Two things here are deliberate.

1. content_sha256 decides whether there is any work to do. The pipeline hashes
   the markdown it produced; if the stored hash matches, the chapter is byte
   identical and re-uploading it would churn 3MB of PNGs to arrive back where
   we started. Re-running the publisher over a whole book is therefore cheap
   and safe, which is what makes it usable as a retry.

2. The chapter row holds content_markdown, topics and images_manifest whole.
   textbook_topics and textbook_images are written from the same data but are
   never read back to build a tutor payload -- that reads the chapter row. A
   prefix assembled by joining rows is a prefix whose bytes depend on join
   order, and Gemini's implicit cache only rewards a stable one.
"""

import json
import mimetypes
from pathlib import Path
from typing import Callable, Iterable, Optional

BUCKET = "textbook-assets"

# Written unpublished. A mis-split book produces chapters that are individually
# valid and collectively wrong, and no automated check can catch that, so a
# human confirms the split before anything is servable.
PUBLISH_ON_INGEST = False


class ChapterPublishError(RuntimeError):
    """The chapter could not be stored. Nothing partial was left behind."""


def load_chapter(chapter_dir: Path) -> dict:
    path = chapter_dir / "chapter.json"
    if not path.exists():
        raise ChapterPublishError(f"{chapter_dir}: no chapter.json")
    return json.loads(path.read_text(encoding="utf-8"))


def storage_prefix(school_id: str, book_id: str, chapter_number: int) -> str:
    return f"{school_id}/{book_id}/ch{chapter_number:02d}"


def _upsert_book(ac, school_id: str, meta: dict) -> str:
    """Find or create the book row, and return its uuid.

    Matched on the PDF's hash rather than its filename or its slug: the same
    book arrives named differently every time, and a re-ingest under a corrected
    subject would otherwise create a second book holding the same pages.
    """
    sha = meta.get("source_pdf_sha256")
    row = {
        "school_id": school_id,
        "book_id": meta["book_id"],
        "board": meta["board"],
        "grade": str(meta["class"]),
        "subject": meta["subject"],
        "language": meta["language"],
        "source_pdf": meta["source_pdf"],
        "source_pdf_sha256": sha,
    }
    if not sha:
        raise ChapterPublishError(
            f"{meta['book_id']}: chapter.json has no source_pdf_sha256. It was "
            "built before that field existed -- rebuild it with src.ingest_book."
        )

    existing = (
        ac.table("textbook_books")
        .select("id, book_id, subject")
        .eq("school_id", school_id)
        .eq("source_pdf_sha256", sha)
        .eq("language", meta["language"])
        .limit(1)
        .execute()
        .data
        or []
    )
    if existing:
        book_uuid = existing[0]["id"]
        # The slug carries board/grade/subject/language, so a change to it means
        # the operator corrected the metadata. Follow it rather than keeping the
        # first answer, which is how a book ends up filed under the wrong subject
        # forever.
        if existing[0]["book_id"] != meta["book_id"]:
            ac.table("textbook_books").update(row).eq("id", book_uuid).execute()
        return book_uuid

    created = ac.table("textbook_books").insert(row).execute().data or []
    if not created:
        raise ChapterPublishError(f"{meta['book_id']}: could not create book row")
    return created[0]["id"]


def _flatten_topics(topics: list[dict], parent_key: Optional[str] = None) -> Iterable[dict]:
    """Depth-first, so order_index reproduces reading order."""
    for topic in topics:
        yield {"topic": topic, "parent_key": parent_key}
        yield from _flatten_topics(topic.get("subtopics", []) or [], topic["id"])


def _topic_rows(chapter_uuid: str, school_id: str, topics: list[dict]) -> list[dict]:
    rows = []
    for index, entry in enumerate(_flatten_topics(topics)):
        topic = entry["topic"]
        rows.append(
            {
                "chapter_uuid": chapter_uuid,
                "school_id": school_id,
                "topic_key": topic["id"],
                "parent_key": entry["parent_key"],
                "title": topic["title"],
                "level": topic["level"],
                "page": topic.get("page"),
                "char_start": topic["start"],
                "char_end": topic["end"],
                "numbered": bool(topic.get("numbered")),
                "image_ids": topic.get("images") or [],
                "order_index": index,
            }
        )
    return rows


def _image_rows(
    chapter_uuid: str, school_id: str, manifest: list[dict], prefix: str
) -> list[dict]:
    rows = []
    for index, image in enumerate(manifest):
        rows.append(
            {
                "chapter_uuid": chapter_uuid,
                "school_id": school_id,
                "image_id": image["image_id"],
                "storage_path": f"{prefix}/{Path(image['local_path']).name}",
                "source_page": image["source_page"],
                "caption": image.get("caption"),
                "width": image.get("width"),
                "height": image.get("height"),
                "bytes": image.get("bytes"),
                "decorative": bool(image.get("decorative")),
                "order_index": index,
            }
        )
    return rows


def _clear_prefix(ac, prefix: str) -> None:
    """Drop whatever is under this chapter's storage prefix.

    Run before re-uploading a changed chapter: an illustration the new build no
    longer produces would otherwise sit in the bucket forever, unreferenced and
    unbilled to anyone's attention.
    """
    try:
        existing = ac.storage.from_(BUCKET).list(prefix)
    except Exception:  # noqa: BLE001 -- an absent prefix is not an error here
        return
    paths = [f"{prefix}/{item['name']}" for item in existing or []]
    if paths:
        ac.storage.from_(BUCKET).remove(paths)


def _upload_assets(ac, chapter_dir: Path, manifest: list[dict], prefix: str) -> int:
    uploaded = 0
    for image in manifest:
        source = chapter_dir / image["local_path"]
        if not source.exists():
            raise ChapterPublishError(
                f"{chapter_dir.name}: manifest lists {image['local_path']} but the "
                "file is missing. Rebuild the chapter rather than publishing a "
                "manifest that points at nothing."
            )
        content_type = mimetypes.guess_type(source.name)[0] or "image/png"
        ac.storage.from_(BUCKET).upload(
            f"{prefix}/{source.name}",
            source.read_bytes(),
            {"content-type": content_type, "upsert": "true"},
        )
        uploaded += 1
    return uploaded


def publish_chapter(
    ac,
    school_id: str,
    chapter_dir: Path,
    *,
    force: bool = False,
    publish: bool = PUBLISH_ON_INGEST,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Store one built chapter. Idempotent; returns a summary of what it did."""
    chapter = load_chapter(chapter_dir)
    meta = chapter["metadata"]
    manifest = chapter.get("images_manifest") or []
    note = progress or (lambda _message: None)

    book_uuid = _upsert_book(ac, school_id, meta)
    prefix = storage_prefix(school_id, meta["book_id"], meta["chapter_number"])

    existing = (
        ac.table("textbook_chapters")
        .select("id, content_sha256, published")
        .eq("book_uuid", book_uuid)
        .eq("chapter_number", meta["chapter_number"])
        .limit(1)
        .execute()
        .data
        or []
    )
    unchanged = bool(existing) and existing[0]["content_sha256"] == meta["content_sha256"]
    if unchanged and not force:
        note(f"{chapter_dir.name}: unchanged")
        return {
            "chapter_uuid": existing[0]["id"],
            "book_uuid": book_uuid,
            "action": "unchanged",
            "topics": 0,
            "images": 0,
        }

    row = {
        "book_uuid": book_uuid,
        "school_id": school_id,
        "chapter_number": meta["chapter_number"],
        "chapter_title": meta["chapter_title"],
        "translation_group": meta.get("translation_group"),
        "page_start": meta["source_pages"][0],
        "page_end": meta["source_pages"][1],
        "content_markdown": chapter["content_markdown"],
        "content_sha256": meta["content_sha256"],
        "images_manifest": manifest,
        "topics": chapter.get("topics") or [],
        "schema_version": chapter.get("schema_version", "1.0"),
        "parser": meta.get("parser"),
        "generated_at": meta.get("generated_at"),
    }
    # Publishing state belongs to the reviewer, not to the build. Re-running the
    # publisher over an already-approved chapter must not silently unpublish it.
    if publish or not existing:
        row["published"] = publish

    if existing:
        chapter_uuid = existing[0]["id"]
        ac.table("textbook_chapters").update(row).eq("id", chapter_uuid).execute()
        action = "updated"
    else:
        created = ac.table("textbook_chapters").insert(row).execute().data or []
        if not created:
            raise ChapterPublishError(f"{chapter_dir.name}: could not create chapter row")
        chapter_uuid = created[0]["id"]
        action = "created"

    # Projections are derived, so they are replaced wholesale rather than
    # reconciled -- a topic tree that gained a heading has different ids all the
    # way down, and merging that is more ways to be wrong than it is worth.
    ac.table("textbook_topics").delete().eq("chapter_uuid", chapter_uuid).execute()
    ac.table("textbook_images").delete().eq("chapter_uuid", chapter_uuid).execute()

    topic_rows = _topic_rows(chapter_uuid, school_id, chapter.get("topics") or [])
    if topic_rows:
        ac.table("textbook_topics").insert(topic_rows).execute()

    _clear_prefix(ac, prefix)
    _upload_assets(ac, chapter_dir, manifest, prefix)
    image_rows = _image_rows(chapter_uuid, school_id, manifest, prefix)
    if image_rows:
        ac.table("textbook_images").insert(image_rows).execute()

    count = (
        ac.table("textbook_chapters")
        .select("id", count="exact")
        .eq("book_uuid", book_uuid)
        .execute()
        .count
        or 0
    )
    ac.table("textbook_books").update({"chapter_count": count}).eq("id", book_uuid).execute()

    note(
        f"{chapter_dir.name}: {action}, {len(topic_rows)} topics, {len(image_rows)} images"
    )
    return {
        "chapter_uuid": chapter_uuid,
        "book_uuid": book_uuid,
        "action": action,
        "topics": len(topic_rows),
        "images": len(image_rows),
    }


def publish_book(
    ac,
    school_id: str,
    chapter_dirs: list[Path],
    *,
    force: bool = False,
    publish: bool = PUBLISH_ON_INGEST,
    progress: Optional[Callable[[str], None]] = None,
) -> dict:
    """Publish several chapters, carrying on past a failure.

    One bad chapter must not abandon the rest, for the same reason ingest_book
    does not: a book with one unreadable page still has fifteen good chapters,
    and making the operator re-run everything to get them is how a whole import
    gets abandoned.
    """
    published, failed = [], []
    for chapter_dir in sorted(chapter_dirs):
        try:
            published.append(
                publish_chapter(
                    ac, school_id, chapter_dir,
                    force=force, publish=publish, progress=progress,
                )
            )
        except Exception as exc:  # noqa: BLE001 -- reported per chapter, not raised
            failed.append({"chapter_dir": chapter_dir.name, "error": str(exc)})
            if progress:
                progress(f"{chapter_dir.name}: FAILED -- {exc}")
    return {
        "published": published,
        "failed": failed,
        "topics": sum(p["topics"] for p in published),
        "images": sum(p["images"] for p in published),
    }
