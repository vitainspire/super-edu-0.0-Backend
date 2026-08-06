"""Get an uploaded PDF onto disk without ever holding it in memory, and make
sure it is gone again afterwards.

Uploaded textbooks are never stored. They land in a temp directory, the
pipeline reads them, and the directory is removed — the durable artefacts are
the extracted chapters, not the PDF.

Why streaming rather than the base64 route this replaces
--------------------------------------------------------
The original path was `readAsDataURL` in the browser -> `{"pdfBase64": "..."}`
-> `base64.b64decode` -> write. That holds the file whole three times over: as
a base64 string inside the parsed JSON body, as the decoded bytes, and again in
the thread arguments the job was started with. Base64 is 4/3 the size to begin
with, so a 60 MB textbook cost roughly 150 MB of process memory before a single
page was read, and *that* is the only reason the 60 MB ceiling existed.

Real SCERT books go well past it — one Class 5 English book in this project's
own corpus is 392 MB — so the ceiling was not a safety margin, it was a wall
across ordinary input.

Streaming to disk makes the peak cost one chunk. The size limit stays, because
an unbounded upload is still a way to fill a disk, but it is now a real limit
rather than a consequence of how the bytes were carried.
"""

import base64
import binascii
import os
import re
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

# Big enough for any real textbook. Overridable so a small deployment can pull
# it down without editing code.
MAX_PDF_MB = int(os.getenv("MAX_PDF_UPLOAD_MB", "512"))
MAX_PDF_BYTES = MAX_PDF_MB * 1024 * 1024

CHUNK_BYTES = 1024 * 1024
TEMP_PREFIX = "textbook_pdf_"
# Anything older than this belonged to a process that died mid-ingest.
ORPHAN_AGE_SECONDS = 6 * 3600


class PdfTooLarge(ValueError):
    pass


class NotAPdf(ValueError):
    pass


def safe_stem(filename: str) -> str:
    """A filename we are willing to create. Never trust the client's."""
    stem = Path(filename or "textbook").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return stem[:80] or "textbook"


def _check_magic(head: bytes) -> None:
    # %PDF may sit behind a short junk preamble; the spec tolerates it and real
    # exports do it, so look in the first 1KB rather than only at byte zero.
    if b"%PDF" not in head[:1024]:
        raise NotAPdf("That file is not a PDF.")


class _Sink:
    """Accumulates chunks into a file, checking the magic bytes and the ceiling.

    Shared by the sync and async paths so the two cannot drift on what counts
    as a valid or an oversized upload.
    """

    def __init__(self, handle):
        self.handle = handle
        self.written = 0
        self.first = True

    def feed(self, chunk: bytes) -> None:
        if not chunk:
            return
        if self.first:
            _check_magic(chunk)
            self.first = False
        self.written += len(chunk)
        # Checked while writing rather than from Content-Length: the header is
        # the client's claim, and a chunked upload has none at all.
        if self.written > MAX_PDF_BYTES:
            raise PdfTooLarge(
                f"PDF is larger than the {MAX_PDF_MB} MB limit. Raise "
                "MAX_PDF_UPLOAD_MB if this book is genuinely this big."
            )
        self.handle.write(chunk)

    def finish(self) -> int:
        if self.written == 0:
            raise NotAPdf("The uploaded file is empty.")
        return self.written


def _write_stream(chunks: Iterator[bytes], dest: Path) -> int:
    with open(dest, "wb") as handle:
        sink = _Sink(handle)
        for chunk in chunks:
            sink.feed(chunk)
        return sink.finish()


async def stage_pdf_from_reader(read, filename: str) -> tuple[Path, Path, int]:
    """Stream from an async `read(size) -> bytes` (Starlette's UploadFile.read).

    This is the path that matters. Each chunk goes to disk as it arrives, so
    the process never holds more than CHUNK_BYTES of the textbook, and an
    oversized upload is refused partway through rather than after it has all
    been buffered.
    """
    work_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        pdf_path = work_dir / f"{safe_stem(filename)}.pdf"
        with open(pdf_path, "wb") as handle:
            sink = _Sink(handle)
            while True:
                chunk = await read(CHUNK_BYTES)
                if not chunk:
                    break
                sink.feed(chunk)
            size = sink.finish()
        return work_dir, pdf_path, size
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def _b64_chunks(pdf_base64: str) -> Iterator[bytes]:
    """Decode a base64 payload incrementally.

    Kept for the older JSON callers. It cannot undo the fact that the whole
    string already arrived in memory, but it at least avoids adding a second
    full-size copy of the decoded bytes on top of it.
    """
    raw = pdf_base64
    if raw.startswith("data:"):
        comma = raw.find(",")
        if comma != -1:
            raw = raw[comma + 1 :]
    raw = re.sub(r"\s+", "", raw)
    # Multiple of 4 keeps each slice self-contained; 3 bytes out per 4 in.
    step = (CHUNK_BYTES // 3) * 4
    for start in range(0, len(raw), step):
        piece = raw[start : start + step]
        try:
            yield base64.b64decode(piece, validate=False)
        except (binascii.Error, ValueError) as exc:
            raise NotAPdf(f"Could not decode the uploaded PDF: {exc}")


@contextmanager
def temp_pdf_from_chunks(chunks: Iterator[bytes], filename: str):
    """Stream chunks into a temp directory, yield (pdf_path, size), then delete.

    The directory goes in `finally`, so it is removed whether extraction
    succeeded, failed, or raised on the way in. Nothing here is a durable
    artefact: what survives an ingest is the chapters written to the database.
    """
    work_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        pdf_path = work_dir / f"{safe_stem(filename)}.pdf"
        size = _write_stream(chunks, pdf_path)
        yield pdf_path, size
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)


def stage_pdf_from_chunks(chunks: Iterator[bytes], filename: str) -> tuple[Path, Path, int]:
    """Same, but hands the caller ownership of the directory.

    For work that outlives the request — a background job started by an upload
    cannot hold a context manager open across the response. The caller MUST
    call discard() in its own `finally`.
    """
    work_dir = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX))
    try:
        pdf_path = work_dir / f"{safe_stem(filename)}.pdf"
        size = _write_stream(chunks, pdf_path)
        return work_dir, pdf_path, size
    except BaseException:
        shutil.rmtree(work_dir, ignore_errors=True)
        raise


def stage_pdf_from_base64(pdf_base64: str, filename: str) -> tuple[Path, Path, int]:
    return stage_pdf_from_chunks(_b64_chunks(pdf_base64), filename)


def discard(work_dir: Path | None) -> None:
    if work_dir is not None:
        shutil.rmtree(work_dir, ignore_errors=True)


def sweep_orphans(max_age_seconds: int = ORPHAN_AGE_SECONDS) -> int:
    """Delete staging directories left behind by a process that died.

    `finally` covers every exit except the ones that skip Python entirely — a
    SIGKILL, an OOM kill, a container stopped mid-ingest. Those leave a whole
    textbook on the deployment's disk with nothing tracking it. Run at startup.
    """
    root = Path(tempfile.gettempdir())
    cutoff = time.time() - max_age_seconds
    removed = 0
    try:
        candidates = list(root.glob(f"{TEMP_PREFIX}*"))
    except OSError:
        return 0
    for path in candidates:
        try:
            if path.is_dir() and path.stat().st_mtime < cutoff:
                shutil.rmtree(path, ignore_errors=True)
                removed += 1
        except OSError:
            continue
    return removed
