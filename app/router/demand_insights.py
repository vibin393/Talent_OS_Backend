# ruff: noqa: E501
"""Demand insights endpoints (Epic 55, A6): the "AI assist" tab, suggestions, flags. ``/demands/flags`` is mounted
before the P1 demands router. Mutations are human-only (API keys read only)."""

import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.pagination import Page, PageParams
from app.core.permissions import Principal, require
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.demand_insights import (
    AcceptSuggestionIn,
    DemandInsightsOut,
    FlagOut,
    FlagQuery,
    ResolveFlagIn,
)
from app.services import demand_insights_service as svc

router = APIRouter(prefix="/demands", tags=["demand-insights"])

Read = Annotated[Principal, Depends(require("demand:read"))]
Update = Annotated[Principal, Depends(require("demand:update"))]
Paging = Annotated[PageParams, Depends()]
DB = TenantDBSessionDep


@router.get("/flags", response_model=Page[FlagOut])
async def list_flags(
    _: Read,
    db: DB,
    params: Paging,
    kind: Annotated[str | None, Query(pattern="^(possible_duplicate|sla_risk)$")] = None,
    status: Annotated[str | None, Query(max_length=20)] = "open",
    demand_id: uuid.UUID | None = None,
) -> Any:
    q = FlagQuery(kind=kind, status=status or None, demand_id=demand_id)  # type: ignore[arg-type]
    return await svc.list_flags(get_context(), db, q, params)


@router.post("/flags/{flag_id}/resolve", response_model=FlagOut)
async def resolve_flag(flag_id: uuid.UUID, body: ResolveFlagIn, _: Update, db: DB) -> Any:
    return await svc.resolve_flag(get_context(), db, flag_id, body)


@router.get("/{demand_id}/insights", response_model=DemandInsightsOut)
async def get_insights(demand_id: uuid.UUID, _: Read, db: DB) -> Any:
    return await svc.insights(get_context(), db, demand_id)


@router.post("/{demand_id}/suggestions/{suggestion_id}/accept", response_model=DemandInsightsOut)
async def accept_suggestion(
    demand_id: uuid.UUID, suggestion_id: uuid.UUID, body: AcceptSuggestionIn, _: Update, db: DB
) -> Any:
    return await svc.accept_suggestion(get_context(), db, demand_id, suggestion_id, body)


@router.post("/{demand_id}/suggestions/{suggestion_id}/dismiss", response_model=DemandInsightsOut)
async def dismiss_suggestion(demand_id: uuid.UUID, suggestion_id: uuid.UUID, _: Update, db: DB) -> Any:
    return await svc.dismiss_suggestion(get_context(), db, demand_id, suggestion_id)
