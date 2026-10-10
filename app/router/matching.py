"""Matching endpoints (Epic 5): demand matches + funnel + recompute, match actions, tenant matching settings.
Each endpoint calls one service function (``app.services.matching``). Demand-scoped endpoints also require
``demand:read`` through the demands service (404 outside the caller's scope)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, status

from app.core.context import get_context
from app.core.permissions import Principal, require
from app.dependencies.ai import require_ai_feature
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.matching import (
    DismissIn,
    FunnelResponse,
    MatchingSettingsIn,
    MatchingSettingsOut,
    MatchListOut,
    MatchOut,
    RecomputeOut,
    RediscoveryOut,
)
from app.services import matching as svc

router = APIRouter(tags=["matching"])

MatchRead = Annotated[Principal, Depends(require("match:read"))]
MatchWrite = Annotated[Principal, Depends(require("match:write"))]
Configure = Annotated[Principal, Depends(require("matching:configure"))]
DB = TenantDBSessionDep


@router.get("/demands/{demand_id}/matches", response_model=MatchListOut)
async def list_matches(
    demand_id: uuid.UUID,
    _: MatchRead,
    db: DB,
    include_dismissed: bool = False,
    page: Annotated[int, Query(ge=1, le=1000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 25,
) -> Any:
    return await svc.service.list_matches(
        get_context(), db, demand_id, include_dismissed=include_dismissed, page=page, page_size=page_size
    )


@router.get("/demands/{demand_id}/matches/funnel", response_model=FunnelResponse)
async def funnel(demand_id: uuid.UUID, _: MatchRead, db: DB) -> Any:
    return await svc.service.get_funnel(get_context(), db, demand_id)


# Epic 38 (FR-194): funnel plus registered rediscovery sections (Talent Pools, ...); demand read as for the funnel.
@router.get("/demands/{demand_id}/rediscovery", response_model=RediscoveryOut)
async def rediscovery(demand_id: uuid.UUID, _: MatchRead, db: DB) -> Any:
    return await svc.rediscovery.rediscovery_summary(get_context(), db, demand_id)


@router.post(
    "/demands/{demand_id}/matches/recompute",
    response_model=RecomputeOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(require_ai_feature("matching"))],  # Epic 12 (FR-62)
)
async def recompute(demand_id: uuid.UUID, _: MatchWrite, db: DB) -> Any:
    return await svc.service.request_recompute(get_context(), db, demand_id)


@router.post("/matches/{match_id}/dismiss", response_model=MatchOut)
async def dismiss(match_id: uuid.UUID, body: DismissIn, _: MatchWrite, db: DB) -> Any:
    return await svc.service.dismiss(get_context(), db, match_id, body)


@router.post("/matches/{match_id}/restore", response_model=MatchOut)
async def restore(match_id: uuid.UUID, _: MatchWrite, db: DB) -> Any:
    return await svc.service.restore(get_context(), db, match_id)


@router.post("/matches/{match_id}/add-to-pipeline", response_model=MatchOut)
async def add_to_pipeline(match_id: uuid.UUID, _: MatchWrite, db: DB) -> Any:
    return await svc.service.add_to_pipeline(get_context(), db, match_id)


@router.get("/settings/matching", response_model=MatchingSettingsOut)
async def get_settings(_: Configure, db: DB) -> Any:
    return await svc.settings.get_settings(get_context(), db)


@router.put("/settings/matching", response_model=MatchingSettingsOut)
async def update_settings(body: MatchingSettingsIn, _: Configure, db: DB) -> Any:
    return await svc.settings.update_settings(get_context(), db, body)
