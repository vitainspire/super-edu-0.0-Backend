"""Auth-gated image lookups that aren't schools-scoped like admin_textbooks.py.

Mirrors:
    backend/src/app/api/textbook-image/[imageId]/route.ts

The sibling POST /workbook-image from that migration group is NOT here — it
already lives in ai_routes2.py, whose version additionally requires a signed-in
user rather than relying on IP rate-limiting alone.
"""
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse

from ..deps import require_user
from ..lib.supabase_clients import create_admin_client
from ..lib.textbook_persist import BUCKET
from ..lib.textbook_catalog import resolve_image_url

router = APIRouter()


# ─── GET /textbook-image/{imageId} — 302 to a freshly signed storage URL ──────
#
# smart_lesson_routes.py bakes `/api/textbook-image/${id}` into the generated
# lesson jsonb at creation time, and PrepSheetView.tsx renders it as a bare
# <img src>. An <img> tag cannot attach the Authorization header this route
# requires, so those tags are still served by the same-origin,
# cookie-authenticated Next.js route. This backend copy exists for JSON
# consumers that can send the bearer token.
#
# Two possible sources for {imageId}, tried in order:
#   1. This app's own PDF-ingestion pipeline (textbook_images — a school's own
#      uploaded, licensed scan). School-scoped: a viewer must belong to the
#      school that ingested it.
#   2. The published-textbook REST API mirror (textbook_catalog.py) — public
#      TS SCERT content, same book available to every school, so no
#      per-school ownership check applies. Always re-fetched live rather than
#      served from any cache: these are the API's own signed Supabase Storage
#      URLs and expire a few hours after being issued, so a URL baked into a
#      saved lesson would go stale — this route re-signs it fresh on every
#      view instead, the same principle the ingestion path below already
#      applies with create_signed_url.
SIGNED_URL_TTL_SECONDS = 60 * 60


@router.get("/textbook-image/{imageId}")
def get_textbook_image(imageId: str, user: dict = Depends(require_user)):
    ac = create_admin_client()

    image_res = (
        ac.table("textbook_images").select("storage_path, school_id")
        .eq("id", imageId).maybe_single().execute()
    )
    image = image_res.data if image_res else None

    if image:
        # Textbook scans are licensed material, so the viewer has to belong to
        # the school that ingested them — being signed in is not enough.
        teacher_res = ac.table("teachers").select("school_id").eq("user_id", user["id"]).maybe_single().execute()
        teacher = teacher_res.data if teacher_res else None
        viewer_school: Optional[str] = (teacher or {}).get("school_id")
        if not viewer_school:
            admin_res = ac.table("admins").select("school_id").eq("user_id", user["id"]).maybe_single().execute()
            admin_row = admin_res.data if admin_res else None
            viewer_school = (admin_row or {}).get("school_id")

        if not viewer_school or viewer_school != image.get("school_id"):
            raise HTTPException(status_code=403, detail="Forbidden")

        try:
            signed = ac.storage.from_(BUCKET).create_signed_url(image["storage_path"], SIGNED_URL_TTL_SECONDS)
        except Exception:  # noqa: BLE001 — treated the same as a missing signature below
            signed = None
        signed_url = (signed or {}).get("signedURL") or (signed or {}).get("signedUrl")
        if not signed_url:
            raise HTTPException(status_code=502, detail="Could not sign the image")

        # Redirect rather than proxy the bytes: storage serves them closer to
        # the reader, and this route stays a cheap lookup. Cached for well
        # under the signature's life, so a reader never follows an expired one.
        return RedirectResponse(signed_url, status_code=302, headers={"Cache-Control": "private, max-age=1800"})

    # Not a locally-ingested image — try the published-textbook mirror.
    catalog_hit = resolve_image_url(imageId, ac)
    if not catalog_hit:
        raise HTTPException(status_code=404, detail="Not found")
    # Short cache: the fetched URL is only good for a few hours, and it was
    # just re-signed fresh, so there is no reason to hold onto it long.
    return RedirectResponse(catalog_hit["url"], status_code=302, headers={"Cache-Control": "private, max-age=1800"})
