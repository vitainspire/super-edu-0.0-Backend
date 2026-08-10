"""Phase C: the Pedagogy Library — admin authoring + the lookup a lesson
generator calls. Shared across every school (curated teaching knowledge, not
school data), so these use require_any_admin like admin_canonical.py.

Mounted at /api/admin/pedagogy:
    GET    /api/admin/pedagogy/activities                 (Stage 2 lookup)
    GET    /api/admin/pedagogy/activity-templates          (admin list)
    POST   /api/admin/pedagogy/activity-templates          (admin create)
    PATCH  /api/admin/pedagogy/activity-templates/{id}      (admin update)
    DELETE /api/admin/pedagogy/activity-templates/{id}      (admin delete)
    POST   /api/admin/pedagogy/activity-templates/{id}/competencies
    DELETE /api/admin/pedagogy/activity-templates/{id}/competencies/{competencyId}
    POST   /api/admin/pedagogy/activity-templates/{id}/contexts
    DELETE /api/admin/pedagogy/activity-templates/{id}/contexts/{contextId}

All business logic lives in app/lib/pedagogy_library.py, which is plain Python
raising ValueError for bad input — this module only maps that onto HTTP.
"""

from typing import Optional
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from ..deps import require_any_admin
from ..lib.supabase_clients import create_admin_client
from ..lib import pedagogy_library as pl

router = APIRouter()


@router.get("/activities")
def recommended_activities(
    competencyIds: str,
    resourceLevel: Optional[int] = None,
    gradeBand: Optional[str] = None,
    admin: dict = Depends(require_any_admin),
):
    """Stage 2 lookup: comma-separated competency ids in, matching activity
    templates (each with its full context list) out."""
    ids = [c.strip() for c in competencyIds.split(",") if c.strip()]
    ac = create_admin_client()
    return {"activities": pl.get_recommended_activities(ac, ids, resource_level=resourceLevel, grade_band=gradeBand)}


@router.get("/activity-templates")
def list_templates(
    gradeBand: Optional[str] = None, category: Optional[str] = None,
    admin: dict = Depends(require_any_admin),
):
    ac = create_admin_client()
    return {"templates": pl.list_activity_templates(ac, grade_band=gradeBand, category=category)}


class CreateTemplateBody(BaseModel):
    gradeBand: str = Field(min_length=1)
    category: str = Field(min_length=1, max_length=100)
    name: str = Field(min_length=1, max_length=300)
    description: Optional[str] = None
    resourceLevel: Optional[int] = None
    durationMin: Optional[int] = None
    durationMax: Optional[int] = None
    grouping: Optional[str] = None
    classroomType: Optional[str] = None
    bloomLevel: Optional[str] = None
    flnCompatible: Optional[bool] = None
    assessmentMethod: Optional[str] = None


def _fields(body: "CreateTemplateBody | PatchTemplateBody") -> dict:
    return {
        "description": body.description,
        "resource_level": body.resourceLevel,
        "duration_min": body.durationMin,
        "duration_max": body.durationMax,
        "grouping": body.grouping,
        "classroom_type": body.classroomType,
        "bloom_level": body.bloomLevel,
        "fln_compatible": body.flnCompatible,
        "assessment_method": body.assessmentMethod,
    }


@router.post("/activity-templates")
def create_template(body: CreateTemplateBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    try:
        return pl.create_activity_template(
            ac, body.gradeBand, body.category, body.name, **_fields(body)
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


class PatchTemplateBody(BaseModel):
    description: Optional[str] = None
    resourceLevel: Optional[int] = None
    durationMin: Optional[int] = None
    durationMax: Optional[int] = None
    grouping: Optional[str] = None
    classroomType: Optional[str] = None
    bloomLevel: Optional[str] = None
    flnCompatible: Optional[bool] = None
    assessmentMethod: Optional[str] = None


@router.patch("/activity-templates/{templateId}")
def update_template(templateId: str, body: PatchTemplateBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    try:
        return pl.update_activity_template(ac, templateId, **_fields(body))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@router.delete("/activity-templates/{templateId}")
def delete_template(templateId: str, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    return pl.delete_activity_template(ac, templateId)


class LinkCompetencyBody(BaseModel):
    competencyId: str = Field(min_length=1)


@router.post("/activity-templates/{templateId}/competencies")
def add_competency(templateId: str, body: LinkCompetencyBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    pl.link_competency(ac, templateId, body.competencyId)
    return {"ok": True}


@router.delete("/activity-templates/{templateId}/competencies/{competencyId}")
def remove_competency(templateId: str, competencyId: str, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    pl.unlink_competency(ac, templateId, competencyId)
    return {"ok": True}


class LinkContextBody(BaseModel):
    contextId: str = Field(min_length=1)


@router.post("/activity-templates/{templateId}/contexts")
def add_context(templateId: str, body: LinkContextBody, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    pl.link_context(ac, templateId, body.contextId)
    return {"ok": True}


@router.delete("/activity-templates/{templateId}/contexts/{contextId}")
def remove_context(templateId: str, contextId: str, admin: dict = Depends(require_any_admin)):
    ac = create_admin_client()
    pl.unlink_context(ac, templateId, contextId)
    return {"ok": True}
