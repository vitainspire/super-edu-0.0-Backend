"""Admin route: extract a full syllabus from an uploaded textbook PDF using the
vision pipeline. Because extraction takes minutes, upload starts a background job
and the frontend polls the status endpoint until topics are ready.

Mounted under /api/admin/schools, so the paths are:
    POST /api/admin/schools/{schoolId}/syllabus/extract-pdf
    GET  /api/admin/schools/{schoolId}/syllabus/extract-pdf/{jobId}
Both are guarded by require_admin (path :schoolId must match the caller's school).
"""

from typing import Optional

from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel

from ..deps import require_admin
from ..lib.pdf_intake import (
    MAX_PDF_MB, NotAPdf, PdfTooLarge, stage_pdf_from_base64, stage_pdf_from_reader,
)
from ..lib.supabase_clients import create_admin_client
from ..lib.syllabus_pdf_jobs import start_extraction, get_job
from ..lib.syllabus_persist import persist_extraction

router = APIRouter()

# Read from the request in 1 MB slices. UploadFile is already disk-backed past
# its spool threshold, so this never materialises the textbook in memory.
UPLOAD_CHUNK = 1024 * 1024


class ExtractPdfBody(BaseModel):
    pdfBase64: str
    filename: Optional[str] = None
    language: Optional[str] = "auto"
    # Admin-selectable on the upload form. Unrecognised values fall back to the
    # server default rather than erroring — resolve_* in vision_extraction is the
    # single place that decides what is valid, so the client can never inject an
    # arbitrary response format or model id.
    extractionFormat: Optional[str] = None
    modelTier: Optional[str] = None


@router.get("/{schoolId}/syllabus/extract-pdf/options")
def extract_syllabus_pdf_options(schoolId: str, admin: dict = Depends(require_admin)):
    """What the upload runs with.

    No longer a menu. The form used to ask the admin to choose an output format
    (markdown or JSON) and a model tier, but neither is a question they are in a
    position to answer: whether the extractor asks the model for markdown or for
    an ontology is an implementation detail of how the text is parsed on the way
    in, and it has no bearing on what they get back or on what is stored. The
    cheaper tier was a real trade -- around 70% less, at the risk of a weaker
    model transliterating non-Latin script instead of transcribing it -- and
    that is not a gamble to offer per upload.

    So the standard settings are simply used, and this endpoint reports them for
    display. Both remain overridable per request for testing; unrecognised
    values still fall back to the default in resolve_*.
    """
    from ..lib.vision_extraction import (
        resolve_extraction_format, resolve_model_tier, model_for_tier,
    )

    tier = resolve_model_tier(None)
    return {
        "format": resolve_extraction_format(None),
        "tier": tier,
        "model": model_for_tier(tier),
        "maxUploadMb": MAX_PDF_MB,
    }


def _started(job_id: str, size_bytes: int) -> dict:
    job = get_job(job_id) or {}
    # Echo the resolved settings so the UI shows what is actually running, not
    # what was asked for — they differ whenever the request sent something unknown.
    return {
        "jobId": job_id,
        "bytes": size_bytes,
        "extractionFormat": job.get("extractionFormat"),
        "modelTier": job.get("modelTier"),
        "model": job.get("model"),
    }


@router.post("/{schoolId}/syllabus/extract-pdf/upload", status_code=202)
async def extract_syllabus_pdf_upload(
    schoolId: str,
    file: UploadFile = File(...),
    language: Optional[str] = Form("auto"),
    extractionFormat: Optional[str] = Form(None),
    modelTier: Optional[str] = Form(None),
    board: Optional[str] = Form(None),
    grade: Optional[str] = Form(None),
    subject: Optional[str] = Form(None),
    admin: dict = Depends(require_admin),
):
    """Upload a textbook as multipart and start extraction.

    The preferred path. The PDF is streamed straight to a temp file and the
    process never holds more than a chunk of it, which is what lets a textbook
    of any realistic size through — the JSON route below cannot, because
    base64 in a request body has to be parsed whole before it can be decoded.

    Nothing is stored: the temp directory is deleted when the job ends. board/
    grade/subject identify the upload for the Drive archive filename only —
    the real, durable grade+subject association is still set at save time
    (SaveExtractionBody), same as before this was added.
    """

    try:
        work_dir, pdf_path, size = await stage_pdf_from_reader(
            file.read, file.filename or "textbook.pdf"
        )
    except PdfTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except NotAPdf as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = start_extraction(
        work_dir, pdf_path, file.filename or "textbook.pdf", language or "auto",
        extraction_format=extractionFormat, model_tier=modelTier,
        board=board, grade=grade, subject=subject,
    )
    return _started(job_id, size)


@router.post("/{schoolId}/syllabus/extract-pdf", status_code=202)
def extract_syllabus_pdf(schoolId: str, body: ExtractPdfBody, admin: dict = Depends(require_admin)):
    """Legacy JSON path, kept so older clients keep working.

    Prefer /extract-pdf/upload. By the time this function runs the whole
    base64 payload is already in memory as a parsed JSON string — nothing here
    can undo that, it only avoids adding a second full-size copy on the way to
    disk.
    """
    if not body.pdfBase64 or not body.pdfBase64.strip():
        raise HTTPException(status_code=400, detail="pdfBase64 is required")
    try:
        work_dir, pdf_path, size = stage_pdf_from_base64(
            body.pdfBase64, body.filename or "textbook.pdf"
        )
    except PdfTooLarge as exc:
        raise HTTPException(status_code=413, detail=str(exc))
    except NotAPdf as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    job_id = start_extraction(
        work_dir, pdf_path, body.filename or "textbook.pdf", body.language or "auto",
        extraction_format=body.extractionFormat, model_tier=body.modelTier,
    )
    return _started(job_id, size)


@router.get("/{schoolId}/syllabus/extract-pdf/{jobId}")
def extract_syllabus_pdf_status(schoolId: str, jobId: str, admin: dict = Depends(require_admin)):
    job = get_job(jobId)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return {
        "status": job["status"],        # pending | running | done | error
        "progress": job["progress"],    # 0-100
        "message": job["message"],
        "topics": job.get("topics"),    # populated only when status == done
        "error": job.get("error"),      # populated only when status == error
        "extractionFormat": job.get("extractionFormat"),
        "modelTier": job.get("modelTier"),
        "model": job.get("model"),
        # What the extractor could not make sense of. Surfaced so the admin knows
        # which topics to check on the review panel instead of trusting silently.
        "warnings": job.get("warnings") or [],
        # True while AI_EXTRACTION_ENABLED is off in syllabus_pdf_jobs.py — the
        # PDF still gets archived to Drive, there just aren't any AI-extracted
        # topics to review, which the frontend needs to tell apart from a real
        # "no topics found" failure.
        "aiSkipped": job.get("aiSkipped", False),
    }


class SaveExtractionBody(BaseModel):
    jobId: str
    grade: str
    subject: str
    excludeTopicIds: Optional[list[str]] = None


@router.post("/{schoolId}/syllabus/save-extraction")
def save_syllabus_extraction(schoolId: str, body: SaveExtractionBody, admin: dict = Depends(require_admin)):
    """Persists a finished PDF-extraction job's FULL ontology (chapters, topics,
    subtopics, exercises, sidebars, concept dependencies) into the syllabus_*
    tables in one request — see syllabus_persist.persist_extraction. Topics the
    admin removed during review (excludeTopicIds, the ontology's own topic ids)
    are dropped, along with their subtopics/exercises/sidebars/dependencies."""
    if not body.grade or not body.subject.strip():
        raise HTTPException(status_code=400, detail="grade and subject required")

    job = get_job(body.jobId)
    if not job or job.get("status") != "done" or not job.get("ontology"):
        raise HTTPException(status_code=404, detail="Extraction job not found, not finished, or already cleared")

    ac = create_admin_client()
    try:
        summary = persist_extraction(
            ac, schoolId, body.grade, body.subject.strip(),
            job["ontology"], job.get("filename") or "textbook.pdf",
            set(body.excludeTopicIds or []),
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return summary
