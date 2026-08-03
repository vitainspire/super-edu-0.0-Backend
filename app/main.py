import os
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .routes import (
    health, misc, admin_auth, admin_schools, admin_classes, admin_teachers,
    admin_timetable, admin_misc, admin_students, admin_grade_syllabus, admin_schedule_ai,
    admin_substitutes, teacher, student, scanner, ai_routes, ai_routes2, vision_routes, scanner_ai_routes,
    admin_syllabus_pdf, admin_textbooks,
)

app = FastAPI(title="EduTeach backend")

app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.environ.get("FRONTEND_ORIGIN", "http://localhost:3000")],
    allow_methods=["*"],
    allow_headers=["Content-Type", "Authorization", "X-Student-Token", "X-Scanner-Token"],
)


@app.on_event("startup")
def _sweep_stale_pdf_uploads():
    """Delete staging directories left by a process that died mid-ingest.

    Uploads are streamed to a temp directory and removed in a `finally`, which
    covers every exit except the ones that skip Python entirely — a SIGKILL, an
    OOM kill, a container stopped part-way. Those leave a whole textbook on
    disk with nothing tracking it, so the next start clears them.
    """
    from .lib.pdf_intake import sweep_orphans

    removed = sweep_orphans()
    if removed:
        print(f"[startup] removed {removed} orphaned PDF upload director(ies)")


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError):
    """FastAPI defaults to 422 for body/query validation failures — the
    Next.js/Express originals used 400 with an {error, details} shape (via
    zod's parseBody). Normalize to that convention so the API contract is
    unchanged from the frontend's perspective."""
    details: dict[str, list[str]] = {}
    for err in exc.errors():
        field = ".".join(str(p) for p in err["loc"] if p not in ("body", "query"))
        details.setdefault(field, []).append(err["msg"])
    return JSONResponse(status_code=400, content={"error": "Invalid request", "details": details})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception):
    print(f"[unhandled] {exc}")
    return JSONResponse(status_code=500, content={"error": "Server error"})


app.include_router(health.router, prefix="/api")
app.include_router(misc.router, prefix="/api")
app.include_router(admin_auth.router, prefix="/api/admin")
app.include_router(admin_schools.router, prefix="/api/admin/schools")
app.include_router(admin_classes.router, prefix="/api/admin/schools")
app.include_router(admin_teachers.router, prefix="/api/admin/schools")
app.include_router(admin_timetable.router, prefix="/api/admin/schools")
app.include_router(admin_misc.router, prefix="/api/admin/schools")
app.include_router(admin_students.router, prefix="/api/admin/schools")
app.include_router(admin_grade_syllabus.router, prefix="/api/admin/schools")
app.include_router(admin_syllabus_pdf.router, prefix="/api/admin/schools")
app.include_router(admin_textbooks.router, prefix="/api/admin/schools")
app.include_router(admin_substitutes.router, prefix="/api/admin/schools")
app.include_router(admin_schedule_ai.router, prefix="/api/admin")
app.include_router(teacher.router, prefix="/api/teacher")
app.include_router(student.router, prefix="/api/student")
app.include_router(scanner.router, prefix="/api/scanner")
app.include_router(ai_routes.router, prefix="/api")
app.include_router(ai_routes2.router, prefix="/api")
app.include_router(vision_routes.router, prefix="/api")
app.include_router(scanner_ai_routes.router, prefix="/api")


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="0.0.0.0", port=int(os.environ.get("PORT", 4000)), reload=True)
