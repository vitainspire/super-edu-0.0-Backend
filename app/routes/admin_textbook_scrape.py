"""Admin route: fetch a textbook PDF from a board's e-textbooks site and stage
it on disk, so the admin doesn't have to find and download it by hand before
running the (separate, external) pdf pipeline + scripts/publish_textbook.

Mounted under /api/admin/schools, so the paths are:
    GET  /api/admin/schools/{schoolId}/textbook-scrape/subjects
    POST /api/admin/schools/{schoolId}/textbook-scrape
    GET  /api/admin/schools/{schoolId}/textbook-scrape/{jobId}
All guarded by require_admin (path :schoolId must match the caller's school).

This deliberately stops at "here is the downloaded file and its path." Turning
that PDF into chapter.json (content markdown, topics, images) is out of scope
-- see app/lib/textbook_scrape_jobs.py's module docstring for why the file is
kept rather than discarded once the job finishes.
"""

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..deps import require_admin
from ..lib import ts_scert_textbooks
from ..lib.textbook_scrape_jobs import get_job, start_scrape_job

router = APIRouter()


def _validate_board(board: str) -> None:
    if board not in ts_scert_textbooks.SUPPORTED_BOARDS:
        raise HTTPException(
            status_code=400,
            detail=f"Board '{board}' is not supported yet -- only TS (Telangana) has a scraper.",
        )


def _validate_class(raw: str) -> int:
    try:
        klass = int(raw)
    except (TypeError, ValueError):
        klass = -1
    if not (1 <= klass <= 10):
        raise HTTPException(status_code=400, detail="class must be a number from 1 to 10.")
    return klass


@router.get("/{schoolId}/textbook-scrape/subjects")
def list_scrape_subjects(
    schoolId: str,
    board: str,
    klass: str = Query(..., alias="class"),
    medium: str = Query(...),
    admin: dict = Depends(require_admin),
):
    """No download -- just reads the class/medium row so the Subject dropdown
    reflects what is actually on the page."""
    _validate_board(board)
    klass_int = _validate_class(klass)
    if not medium.strip():
        raise HTTPException(status_code=400, detail="medium is required.")

    try:
        subjects = ts_scert_textbooks.list_subjects(klass_int, medium.strip())
    except ts_scert_textbooks.NoClassTable as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ts_scert_textbooks.NoMediumRow as e:
        raise HTTPException(status_code=404, detail=str(e))
    except ts_scert_textbooks.SiteShapeChanged as e:
        raise HTTPException(status_code=502, detail=str(e))

    return {"class": klass_int, "medium": medium.strip(), "subjects": subjects}


class StartScrapeBody(BaseModel):
    board: str
    subject: str
    medium: str
    class_number: str = Field(alias="class")


@router.post("/{schoolId}/textbook-scrape", status_code=202)
def start_textbook_scrape(schoolId: str, body: StartScrapeBody, admin: dict = Depends(require_admin)):
    _validate_board(body.board)
    klass_int = _validate_class(body.class_number)
    if not body.subject.strip():
        raise HTTPException(status_code=400, detail="subject is required.")
    if not body.medium.strip():
        raise HTTPException(status_code=400, detail="medium is required.")

    job_id = start_scrape_job(body.board, klass_int, body.subject.strip(), body.medium.strip())
    return {"jobId": job_id}


@router.get("/{schoolId}/textbook-scrape/{jobId}")
def textbook_scrape_status(schoolId: str, jobId: str, admin: dict = Depends(require_admin)):
    job = get_job(jobId)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found or expired")
    return {
        "status": job["status"],      # pending | running | done | error
        "progress": job["progress"],  # 0-100
        "message": job["message"],
        "files": job.get("files"),    # populated only when status == done
        "error": job.get("error"),    # populated only when status == error
    }
