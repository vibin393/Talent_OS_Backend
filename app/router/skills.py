# ruff: file-ignore[line-too-long]
"""Skills graph & proficiency endpoints (Epic 37, FR-187..FR-191). New Phase 3 routes only; the P1 catalogue routes
(``/skills``) stay in ``router/candidates.py``. Each endpoint calls exactly one ``skills_service`` function.

* ``/skill-graph/*`` -- node view, search, tenant edges (``skill_graph:manage``), settings, expansion preview.
* ``/candidates/{id}/skills/...`` -- evidence history / verify / supersede, skill profile, suggestions.
* ``/skills/insights`` -- supply and demand insight (``skills_insight:read``).
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.candidates import SkillOut
from app.schemas.skills import (
    EvidenceIn,
    EvidenceOut,
    ExpandPreviewIn,
    ExpandPreviewOut,
    SkillEdgeIn,
    SkillEdgeOut,
    SkillEdgeUpdate,
    SkillGraphSettingsIn,
    SkillGraphSettingsOut,
    SkillInsightsOut,
    SkillNodeOut,
    SkillProfileRow,
    SkillSuggestionAcceptIn,
    SkillSuggestionOut,
    SkillSuggestionRejectIn,
)
from app.services import skills_service as svc

router = APIRouter(prefix="/skill-graph", tags=["skill-graph"])
candidate_router = APIRouter(prefix="/candidates", tags=["skill-graph"])
insights_router = APIRouter(prefix="/skills", tags=["skill-graph"])

Manage = Annotated[Principal, Depends(require("skill_graph:manage"))]
Read = Annotated[Principal, Depends(require("candidate:read"))]
Update = Annotated[Principal, Depends(require("candidate:update"))]
Insight = Annotated[Principal, Depends(require("skills_insight:read"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep
KeyQ = Annotated[str, Query(min_length=8, max_length=60)]


# ================================================================ graph admin
@router.get("/search", response_model=Page[SkillOut])
async def search(
    _: Read, db: DB, params: Paging, q: Annotated[str | None, Query(max_length=200)] = None
) -> Page[SkillOut]:
    return await svc.search_skills(get_context(), db, q, params)


@router.get("/skills/{key}", response_model=SkillNodeOut)
async def get_node(key: str, _: Read, db: DB) -> SkillNodeOut:
    return await svc.skill_node(get_context(), db, key)


@router.get("/edges", response_model=list[SkillEdgeOut])
async def list_edges(
    _: Read,
    db: DB,
    key: Annotated[str | None, Query(max_length=60)] = None,
    kind: Annotated[str | None, Query(pattern="^(parent_of|related_to|implies)$")] = None,
) -> list[SkillEdgeOut]:
    return await svc.list_edges(get_context(), db, key=key, kind=kind)


@router.post("/edges", response_model=SkillEdgeOut, status_code=status.HTTP_201_CREATED)
async def create_edge(body: SkillEdgeIn, _: Manage, db: DB) -> SkillEdgeOut:
    return await svc.create_edge(get_context(), db, body)


@router.patch("/edges/{edge_id}", response_model=SkillEdgeOut)
async def update_edge(edge_id: uuid.UUID, body: SkillEdgeUpdate, _: Manage, db: DB) -> SkillEdgeOut:
    return await svc.update_edge(get_context(), db, edge_id, body)


@router.delete("/edges/{edge_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_edge(edge_id: uuid.UUID, _: Manage, db: DB) -> Response:
    await svc.delete_edge(get_context(), db, edge_id)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/settings", response_model=SkillGraphSettingsOut)
async def get_settings(_: Read, db: DB) -> SkillGraphSettingsOut:
    return await svc.get_settings(get_context(), db)


@router.put("/settings", response_model=SkillGraphSettingsOut)
async def put_settings(body: SkillGraphSettingsIn, _: Manage, db: DB) -> SkillGraphSettingsOut:
    return await svc.update_settings(get_context(), db, body)


@router.post("/expand-preview", response_model=ExpandPreviewOut)
async def expand_preview(body: ExpandPreviewIn, _: Read, db: DB) -> ExpandPreviewOut:
    return await svc.expand_preview(get_context(), db, body)


# ================================================================ candidate evidence and suggestions
@candidate_router.get("/{candidate_id}/skill-profile", response_model=list[SkillProfileRow])
async def skill_profile(candidate_id: uuid.UUID, _: Read, db: DB) -> list[SkillProfileRow]:
    return await svc.skill_profile(get_context(), db, candidate_id)


@candidate_router.get(
    "/{candidate_id}/skills/{candidate_skill_id}/evidence", response_model=list[EvidenceOut]
)
async def list_evidence(
    candidate_id: uuid.UUID, candidate_skill_id: uuid.UUID, _: Read, db: DB
) -> list[EvidenceOut]:
    return await svc.list_evidence(get_context(), db, candidate_id, candidate_skill_id)


@candidate_router.post(
    "/{candidate_id}/skills/{candidate_skill_id}/evidence",
    response_model=EvidenceOut,
    status_code=status.HTTP_201_CREATED,
)
async def add_evidence(
    candidate_id: uuid.UUID, candidate_skill_id: uuid.UUID, body: EvidenceIn, _: Read, db: DB
) -> EvidenceOut:
    return await svc.create_evidence(get_context(), db, candidate_id, candidate_skill_id, body)


@candidate_router.post(
    "/{candidate_id}/skills/{candidate_skill_id}/evidence/{evidence_id}/supersede", response_model=EvidenceOut
)
async def supersede_evidence(
    candidate_id: uuid.UUID, candidate_skill_id: uuid.UUID, evidence_id: uuid.UUID, _: Read, db: DB
) -> EvidenceOut:
    return await svc.supersede_evidence(get_context(), db, candidate_id, candidate_skill_id, evidence_id)


@candidate_router.get("/{candidate_id}/skill-suggestions", response_model=list[SkillSuggestionOut])
async def list_suggestions(
    candidate_id: uuid.UUID,
    _: Read,
    db: DB,
    status_: Annotated[
        str | None, Query(alias="status", pattern="^(pending|accepted|rejected|edited)$")
    ] = "pending",
) -> list[SkillSuggestionOut]:
    return await svc.list_suggestions(get_context(), db, candidate_id, status_)


@candidate_router.post(
    "/{candidate_id}/skill-suggestions/{suggestion_id}/accept", response_model=SkillSuggestionOut
)
async def accept_suggestion(
    candidate_id: uuid.UUID, suggestion_id: uuid.UUID, body: SkillSuggestionAcceptIn, _: Update, db: DB
) -> SkillSuggestionOut:
    return await svc.accept_suggestion(get_context(), db, candidate_id, suggestion_id, body.proficiency)


@candidate_router.post(
    "/{candidate_id}/skill-suggestions/{suggestion_id}/reject", response_model=SkillSuggestionOut
)
async def reject_suggestion(
    candidate_id: uuid.UUID, suggestion_id: uuid.UUID, body: SkillSuggestionRejectIn, _: Update, db: DB
) -> SkillSuggestionOut:
    return await svc.reject_suggestion(get_context(), db, candidate_id, suggestion_id, body.reason)


# ================================================================ insights
@insights_router.get("/insights", response_model=SkillInsightsOut)
async def skill_insights(
    _: Insight,
    db: DB,
    category: Annotated[str | None, Query(max_length=100)] = None,
    business_unit_id: uuid.UUID | None = None,
    skill: Annotated[list[str] | None, Query(max_length=50)] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SkillInsightsOut:
    return await svc.skill_insights(
        get_context(), db, category=category, business_unit_id=business_unit_id, skill_refs=skill, limit=limit
    )


routers = [router, candidate_router, insights_router]
