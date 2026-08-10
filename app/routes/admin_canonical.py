"""Admin review/management for the Phase B canonical library (concepts,
competencies, vocabulary, contexts) — shared across every school, not scoped
to one, so these routes use require_any_admin rather than require_admin (no
:schoolId in the path to check against; see deps.py for the trust-boundary
note).

Mounted at /api/admin/canonical:
    GET    /api/admin/canonical/{kind}
    GET    /api/admin/canonical/{kind}/{entityId}/topics
    PATCH  /api/admin/canonical/{kind}/{entityId}
    POST   /api/admin/canonical/{kind}/merge
    DELETE /api/admin/canonical/{kind}/{entityId}
    POST   /api/admin/canonical/{kind}/{entityId}/split

All business logic lives in app/lib/canonical_mapping.py, which is plain
Python raising ValueError for bad input — this module's only job is mapping
that onto HTTP status codes.
"""

from typing import Literal, Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import require_any_admin
from ..lib.supabase_clients import create_admin_client
from ..lib import canonical_mapping as cm

router = APIRouter()

Kind = Literal["concepts", "competencies", "vocabulary", "contexts"]


@router.get("/{kind}")
def list_entries(kind: Kind, search: Optional[str] = None, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    return {"entries": cm.list_canonical(ac, kind, search=search)}


@router.get("/{kind}/{entityId}/topics")
def entry_topics(kind: Kind, entityId: str, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    return {"topics": cm.get_canonical_topics(ac, kind, entityId)}


class RenameBody(BaseModel):
    name: str = Field(min_length=1, max_length=300)


@router.patch("/{kind}/{entityId}")
def rename_entry(kind: Kind, entityId: str, body: RenameBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    try:
        return cm.rename_canonical(ac, kind, entityId, body.name)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class MergeBody(BaseModel):
    keepId: str = Field(min_length=1)
    mergeIds: list[str] = Field(min_length=1, max_length=50)


@router.post("/{kind}/merge")
def merge_entries(kind: Kind, body: MergeBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    try:
        return cm.merge_canonical(ac, kind, body.keepId, body.mergeIds)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/{kind}/{entityId}")
def delete_entry(kind: Kind, entityId: str, force: bool = False, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    try:
        return cm.delete_canonical(ac, kind, entityId, force=force)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))


class SplitBody(BaseModel):
    sourceName: str = Field(min_length=1, max_length=300)


@router.post("/{kind}/{entityId}/split")
def split_entry(kind: Kind, entityId: str, body: SplitBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    try:
        return cm.split_canonical(ac, kind, entityId, body.sourceName)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
