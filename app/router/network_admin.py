# ruff: noqa: E501
"""Tenant admin for the Job Network (Epic 75, spec A9; prefix ``/api/v1``). ``join`` / ``leave`` / per-posting opt-in
need ``network:manage`` or ``posting:publish_external`` and are user actions (API keys / Apps are refused by
``require_human`` in the service); reads also allow ``report:read`` for analytics."""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from app.core.context import get_context
from app.core.permissions import Principal, require, require_any
from app.dependencies.auth import TenantDBSessionDep
from app.schemas.job_network import (
    ListingEligibilityOut,
    ListingStatusOut,
    MyListingsPage,
    MyListingsQuery,
    NetworkAnalyticsOut,
    NetworkJoinIn,
    NetworkSettingsOut,
    NetworkSettingsPatch,
    RequestReviewIn,
    SetChoiceIn,
)
from app.services import job_network_service as svc

router = APIRouter(tags=["job-network"])
DB = TenantDBSessionDep
Manage = Annotated[Principal, Depends(require("network:manage"))]
Publish = Annotated[Principal, Depends(require("posting:publish_external"))]
DemandRead = Annotated[Principal, Depends(require("demand:read"))]
# reads: ``network:manage`` or ``posting:publish_external``; analytics: ``network:manage`` or ``report:read``.
SettingsRead = Annotated[Principal, Depends(require_any("network:manage", "posting:publish_external"))]
ListingsRead = Annotated[Principal, Depends(require_any("posting:publish_external", "network:manage"))]
AnalyticsRead = Annotated[Principal, Depends(require_any("network:manage", "report:read"))]


@router.get("/job-network/settings", response_model=NetworkSettingsOut)
async def get_settings(_: SettingsRead, db: DB) -> NetworkSettingsOut:
    return await svc.get_settings(get_context(), db)


@router.post("/job-network/join", response_model=NetworkSettingsOut)
async def join(body: NetworkJoinIn, _: Manage, db: DB) -> NetworkSettingsOut:
    return await svc.join(get_context(), db, body)


@router.patch("/job-network/settings", response_model=NetworkSettingsOut)
async def update_settings(body: NetworkSettingsPatch, _: Manage, db: DB) -> NetworkSettingsOut:
    return await svc.update_settings(get_context(), db, body)


@router.post("/job-network/leave", response_model=NetworkSettingsOut)
async def leave(_: Manage, db: DB, reason: Annotated[str | None, Query(max_length=500)] = None) -> NetworkSettingsOut:
    return await svc.leave(get_context(), db, reason=reason)


@router.get("/job-network/listings", response_model=MyListingsPage)
async def my_listings(
    _: ListingsRead,
    db: DB,
    status_: Annotated[str | None, Query(alias="status")] = None,
    q: Annotated[str | None, Query()] = None,
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=50)] = 20,
) -> MyListingsPage:
    return await svc.my_listings(get_context(), db, MyListingsQuery(status=status_, q=q, page=page, page_size=page_size))


@router.get("/demands/{demand_id}/network", response_model=ListingEligibilityOut)
async def demand_eligibility(demand_id: uuid.UUID, _: DemandRead, db: DB) -> ListingEligibilityOut:
    return await svc.eligibility(get_context(), db, demand_id)


@router.put("/demands/{demand_id}/network", response_model=ListingStatusOut)
async def set_choice(demand_id: uuid.UUID, body: SetChoiceIn, _: Publish, db: DB) -> ListingStatusOut:
    return await svc.set_choice(get_context(), db, demand_id, body.include)


@router.post("/job-network/listings/{listing_id}/request-review", response_model=ListingStatusOut)
async def request_review(listing_id: uuid.UUID, body: RequestReviewIn, _: Publish, db: DB) -> ListingStatusOut:
    from app.services.job_network import membership

    return await membership.request_review(get_context(), db, listing_id, body.note)


@router.get("/job-network/analytics", response_model=NetworkAnalyticsOut)
async def analytics(
    _: AnalyticsRead,
    db: DB,
    date_from: Annotated[date | None, Query(alias="from")] = None,
    date_to: Annotated[date | None, Query(alias="to")] = None,
    demand_id: Annotated[uuid.UUID | None, Query()] = None,
) -> NetworkAnalyticsOut:
    return await svc.analytics(get_context(), db, date_from=date_from, date_to=date_to, demand_id=demand_id)
