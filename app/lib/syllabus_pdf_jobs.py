"""In-memory background-job manager for PDF syllabus extraction.

An admin uploads a textbook PDF; extraction (vision_extraction.generate_ontology_vision)
takes several minutes, so it runs in a daemon thread and the frontend polls for
progress. Jobs live in-process only — fine for this one-off admin setup flow; a
server restart simply drops in-flight jobs (the admin re-uploads).

The rich ontology is mapped down to the syllabus editor's shape:
    {topic, description, weekNumber, subTopics: [...]}
so the existing review-and-save UI can consume it unchanged.
"""

import base64
import binascii
import re
import shutil
import tempfile
import threading
import time
import uuid
from pathlib import Path

_JOBS: dict[str, dict] = {}
_LOCK = threading.Lock()

_JOB_TTL_SECONDS = 3600          # prune finished jobs after an hour
_MAX_PDF_BYTES = 60 * 1024 * 1024  # 60 MB decoded ceiling


def _now() -> float:
    return time.time()


def _prune_locked():
    """Drop jobs that finished more than _JOB_TTL_SECONDS ago. Caller holds _LOCK."""
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


def _map_ontology_to_topics(ontology: dict) -> list[dict]:
    """Flatten the ontology into the syllabus editor's REVIEW-list shape, in reading order.
    This is display-only (the review panel before Save) — the actual DB persistence
    (see syllabus_persist.persist_extraction) reads the full ontology directly so no
    exercise/sidebar/dependency data needs to round-trip through this shape.

    Each ontology topic -> one review row:
      id          = the ontology's own topic id (e.g. "T_2_1") — stable within this
                    extraction, used to track which topics the admin excluded on review
      topic       = topic name (original script, as printed)
      description = English concept summary (the 'context')
      subTopics   = names of that topic's subtopics
      weekNumber  = the topic's chapter number (groups a chapter's topics together)
      exerciseCount / sidebarCount = counts only, so the review panel can hint at
                    the richer content that will be saved alongside the topic
    """
    entities = ontology.get("entities", {}) or {}
    topics = entities.get("topics", []) or []
    subtopics = entities.get("subtopics", []) or []
    exercises = entities.get("exercises", []) or []
    sidebars = entities.get("sidebars", []) or []
    chapters = {c.get("id"): c for c in (entities.get("chapters", []) or [])}

    subs_by_topic: dict[str, list] = {}
    for s in subtopics:
        subs_by_topic.setdefault(s.get("topic_id"), []).append(s)

    ex_count_by_topic: dict[str, int] = {}
    for e in exercises:
        tid = e.get("topic_id")
        ex_count_by_topic[tid] = ex_count_by_topic.get(tid, 0) + 1

    sb_count_by_topic: dict[str, int] = {}
    for sb in sidebars:
        tid = sb.get("topic_id")
        sb_count_by_topic[tid] = sb_count_by_topic.get(tid, 0) + 1

    def chap_number(t: dict) -> int:
        c = chapters.get(t.get("chapter_id"))
        num = c.get("number") if c else None
        return num if isinstance(num, int) else 999

    ordered = sorted(
        topics,
        key=lambda t: (chap_number(t), t.get("page_start") or 0, t.get("id") or ""),
    )

    out: list[dict] = []
    for i, t in enumerate(ordered):
        name = (t.get("name") or "").strip()
        if not name:
            continue
        sub_names = [(s.get("name") or "").strip() for s in subs_by_topic.get(t.get("id"), [])]
        sub_names = [s for s in sub_names if s]
        num = chap_number(t)
        out.append({
            "id": t.get("id"),
            "topic": name,
            "description": (t.get("summary") or "").strip(),
            "weekNumber": num if num != 999 else (i + 1),
            "subTopics": sub_names,
            "exerciseCount": ex_count_by_topic.get(t.get("id"), 0),
            "sidebarCount": sb_count_by_topic.get(t.get("id"), 0),
        })
    return out


def _safe_stem(filename: str) -> str:
    stem = Path(filename or "textbook").stem
    stem = re.sub(r"[^A-Za-z0-9_-]+", "_", stem).strip("_")
    return stem or "textbook"


def _decode_pdf(pdf_base64: str) -> bytes:
    raw = pdf_base64
    if raw.startswith("data:"):
        comma = raw.find(",")
        if comma != -1:
            raw = raw[comma + 1:]
    try:
        data = base64.b64decode(raw, validate=False)
    except (binascii.Error, ValueError) as e:
        raise ValueError(f"Could not decode the uploaded PDF: {e}")
    if not data:
        raise ValueError("The uploaded PDF is empty.")
    if len(data) > _MAX_PDF_BYTES:
        raise ValueError("PDF is too large (max 60 MB).")
    if not data[:5].startswith(b"%PDF"):
        raise ValueError("The uploaded file is not a valid PDF.")
    return data


def start_extraction(pdf_base64: str, filename: str, language: str) -> str:
    """Kick off a background extraction job and return its id immediately."""
    job_id = uuid.uuid4().hex
    with _LOCK:
        _prune_locked()
        _JOBS[job_id] = {
            "status": "pending",
            "progress": 0,
            "message": "Queued…",
            "topics": None,
            "error": None,
            "createdAt": _now(),
            "finishedAt": 0,
        }

    thread = threading.Thread(
        target=_run_job,
        args=(job_id, pdf_base64, filename, language),
        name=f"syllabus-pdf-{job_id[:8]}",
        daemon=True,
    )
    thread.start()
    return job_id


def _run_job(job_id: str, pdf_base64: str, filename: str, language: str):
    work_dir: Path | None = None
    try:
        _update(job_id, status="running", progress=1, message="Preparing PDF…")

        data = _decode_pdf(pdf_base64)

        work_dir = Path(tempfile.mkdtemp(prefix="syllabus_pdf_"))
        pdf_path = work_dir / f"{_safe_stem(filename)}.pdf"
        pdf_path.write_bytes(data)

        # Imported lazily so a missing OPENROUTER_API_KEY (or PyMuPDF) surfaces as a
        # job error rather than crashing server startup.
        from .vision_extraction import generate_ontology_vision

        def cb(pct: int, message: str):
            _update(job_id, progress=max(1, min(99, int(pct))), message=message)

        ontology, _ = generate_ontology_vision(
            str(pdf_path),
            output_dir=str(work_dir / "out"),
            language=language or "auto",
            progress_cb=cb,
        )

        topics = _map_ontology_to_topics(ontology)
        if not topics:
            _update(
                job_id,
                status="error",
                progress=100,
                message="No topics found",
                error="Could not extract any syllabus topics from this PDF.",
                finishedAt=_now(),
            )
            return

        _update(
            job_id,
            status="done",
            progress=100,
            message=f"{len(topics)} topics extracted",
            topics=topics,
            ontology=ontology,  # Full ontology, kept in-memory so Save can persist everything
            filename=filename,
            finishedAt=_now(),
        )
    except Exception as e:  # noqa: BLE001 — surface any failure to the poller
        _update(
            job_id,
            status="error",
            progress=100,
            message="Extraction failed",
            error=str(e) or "Unexpected error during extraction.",
            finishedAt=_now(),
        )
    finally:
        if work_dir is not None:
            shutil.rmtree(work_dir, ignore_errors=True)
