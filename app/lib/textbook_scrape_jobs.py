"""In-memory background-job manager for the TS SCERT textbook scraper.

Same pattern as app/lib/syllabus_pdf_jobs.py: an admin starts a fetch, the
site round-trip + download takes a few seconds to a couple of minutes, so it
runs in a daemon thread and the frontend polls for progress. Jobs live
in-process only -- a server restart drops in-flight jobs, and the admin just
clicks Start again.

Unlike syllabus_pdf_jobs, the downloaded PDF is NOT deleted when the job
finishes. There, the uploaded PDF is disposable -- the durable artefact is
the extracted content written to the database. Here there is no database
write at all (chapter extraction is a separate, external, manual step); the
PDF sitting in its staging directory *is* the deliverable, so it is kept.
"""

import os
import threading
import time
import uuid
from pathlib import Path

from . import ts_scert_textbooks

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

_JOB_TTL_SECONDS = 3600  # prune finished jobs after an hour (job records, not the downloaded files)

# Where downloaded PDFs land, one subdirectory per job so concurrent jobs
# never collide or skip each other's files. Overridable so a deployment can
# point it at a persistent volume.
STAGING_ROOT = Path(os.getenv("TEXTBOOK_SCRAPE_DIR", "var/textbook_scrapes")).resolve()


def _now() -> float:
    return time.time()


def _prune_locked():
    """Drop job RECORDS that finished more than _JOB_TTL_SECONDS ago. Caller holds _LOCK.
    Does not touch anything on disk -- downloaded files are never auto-deleted."""
    cutoff = _now() - _JOB_TTL_SECONDS
    stale = [
        jid for jid, j in _JOBS.items()
        if j["status"] in ("done", "error") and j.get("finishedAt", 0) < cutoff
    ]
    for jid in stale:
        _JOBS.pop(jid, None)


def get_job(job_id: str) -> dict | None:
    with _LOCK:
        job = _JOBS.get(job_id)
        return dict(job) if job else None


def _update(job_id: str, **fields):
    with _LOCK:
        job = _JOBS.get(job_id)
        if job:
            job.update(fields)


def start_scrape_job(board: str, klass: int, subject: str, medium: str) -> str:
    """Kick off a background scrape+download job and return its id immediately.

    Callers must validate board/klass/subject/medium before calling this --
    it does no input validation itself, matching start_extraction's contract
    in syllabus_pdf_jobs.py (the route layer 400s on bad input before a job
    is ever spawned).
    """
    job_id = uuid.uuid4().hex
    with _LOCK:
        _prune_locked()
        _JOBS[job_id] = {
            "status": "pending",
            "progress": 0,
            "message": "Queued…",
            "files": None,
            "error": None,
            "createdAt": _now(),
            "finishedAt": 0,
        }

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, klass, subject, medium),
        name=f"textbook-scrape-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job_id


def _run_job(job_id: str, klass: int, subject: str, medium: str):
    try:
        _update(job_id, status="running", progress=1, message="Contacting scert.telangana.gov.in…")

        dest_dir = STAGING_ROOT / job_id
        dest_dir.mkdir(parents=True, exist_ok=True)

        def cb(done: int, total: int, message: str):
            pct = 5 + int(90 * done / max(total, 1))
            _update(job_id, progress=max(1, min(99, pct)), message=message)

        files = ts_scert_textbooks.download_textbooks(klass, medium, subject, dest_dir, progress_cb=cb)

        _update(
            job_id,
            status="done",
            progress=100,
            message=f"{len(files)} file(s) downloaded",
            files=files,
            finishedAt=_now(),
        )
    except ts_scert_textbooks.ScrapeError as e:
        _update(
            job_id,
            status="error",
            progress=100,
            message="Fetch failed",
            error=str(e),
            finishedAt=_now(),
        )
    except Exception as e:  # noqa: BLE001 -- network failures, gdown errors, etc.
        _update(
            job_id,
            status="error",
            progress=100,
            message="Fetch failed",
            error=str(e) or "Unexpected error while fetching the textbook.",
            finishedAt=_now(),
        )
