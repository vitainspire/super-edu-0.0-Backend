"""Storage-proxy routes: serve stored media through the backend rather than
linking straight to Supabase Storage, because Storage's own served headers
are wrong for the content (an anti-XSS override on public HTML).

Note: /api/textbook-image/[imageId] (a similar proxy, but auth-gated) was
deliberately NOT moved here. It's rendered as a bare <img src>, which cannot
carry the Authorization bearer header this backend's auth relies on — its
old same-origin version worked only because the browser auto-attached the
Supabase SSR session cookie. Moving it would need a different auth
mechanism (e.g. a query-string token) or a data-format change to persisted
lessons, so it stays a thin Next.js route for now."""
from fastapi import APIRouter, HTTPException, Response

from ..lib.supabase_clients import create_admin_client

router = APIRouter()

_SIMULATIONS_BUCKET = "simulations"


# GET /api/simulation/{classId}/{prepMaterialId}
#
# Proxies the generated simulation HTML instead of linking straight to the
# Storage public URL. Supabase Storage deliberately overrides Content-Type to
# text/plain (plus a locked-down CSP) for publicly-served "renderable" types
# like text/html — an anti-XSS hardening measure, not a bug, and not
# something the upload's contentType option can override. The actual file
# bytes are fine; downloading it server-side and re-serving it ourselves with
# the right header is what lets the iframe render it as a page instead of
# showing raw source.
@router.get("/simulation/{classId}/{prepMaterialId}")
def simulation(classId: str, prepMaterialId: str):
    ac = create_admin_client()
    try:
        data = ac.storage.from_(_SIMULATIONS_BUCKET).download(f"{classId}/{prepMaterialId}.html")
    except Exception:
        raise HTTPException(status_code=404, detail="Simulation not found")
    if not data:
        raise HTTPException(status_code=404, detail="Simulation not found")

    return Response(
        content=data,
        media_type="text/html; charset=utf-8",
        headers={"Cache-Control": "no-store"},
    )
