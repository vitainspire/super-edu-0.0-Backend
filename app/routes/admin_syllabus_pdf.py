"""Admin route: extract a full syllabus from an uploaded textbook PDF using the
vision pipeline. Because extraction takes minutes, upload starts a background job
and the frontend polls the status endpoint until topics are ready.

Mounted under /api/admin/schools, so the paths are:
    POST /api/admin/schools/{schoolId}/syllabus/extract-pdf
    GET  /api/admin/schools/{schoolId}/syllabus/extract-pdf/{jobId}
Both are guarded by require_admin (path :schoolId must match the caller's school).
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from ..deps import require_admin
from ..lib.supabase_clients import create_admin_client
from ..lib.syllabus_pdf_jobs import start_extraction, get_job
from ..lib.syllabus_persist import persist_extraction

router = APIRouter()


class ExtractPdfBody(BaseModel):
    pdfBase64: str
    filename: Optional[str] = None
    language: Optional[str] = "auto"


@router.post("/{schoolId}/syllabus/extract-pdf", status_code=202)
def extract_syllabus_pdf(schoolId: str, body: ExtractPdfBody, admin: dict = Depends(require_admin)):
    if not body.pdfBase64 or not body.pdfBase64.strip():
        raise HTTPException(status_code=400, detail="pdfBase64 is required")
    job_id = start_extraction(
        body.pdfBase64,
        body.filename or "textbook.pdf",
        body.language or "auto",
    )
    return {"jobId": job_id}


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
