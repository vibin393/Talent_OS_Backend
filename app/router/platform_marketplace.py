# ruff: file-ignore[line-too-long]
"""Platform marketplace review (Epic 69, Part C platform; ``/api/v1/platform/marketplace``). Platform operator + MFA;
role ``marketplace_reviewer`` for every decision (403 ``platform_role_required``). Approve, reject, suspend, reinstate
and delist are ★ (human platform operators only)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Query

from app.core.context import get_context
from app.dependencies.platform import PlatformOperatorMfaDep
from app.schemas.platform_marketplace import (
    ChecklistIn,
    DecisionIn,
    ListingActionIn,
    OperatorRolesIn,
    OperatorRolesOut,
    PlatformListingOut,
    QueueItemOut,
    ReviewVersionOut,
)
from app.services.marketplace_service import review

router = APIRouter(prefix="/platform/marketplace", tags=["platform-marketplace"])


@router.get("/queue", response_model=list[QueueItemOut])
async def queue(
    _: PlatformOperatorMfaDep, status_: Annotated[str | None, Query(alias="status")] = None
) -> list[QueueItemOut]:
    return await review.queue(get_context(), status_)


@router.get("/versions/{version_id}", response_model=ReviewVersionOut)
async def version(version_id: uuid.UUID, _: PlatformOperatorMfaDep) -> ReviewVersionOut:
    return await review.get_version(get_context(), version_id)


@router.post("/versions/{version_id}/claim", response_model=ReviewVersionOut)
async def claim(version_id: uuid.UUID, _: PlatformOperatorMfaDep) -> ReviewVersionOut:
    return await review.claim(get_context(), version_id)


@router.put("/versions/{version_id}/checklist", response_model=ReviewVersionOut)
async def checklist(version_id: uuid.UUID, body: ChecklistIn, _: PlatformOperatorMfaDep) -> ReviewVersionOut:
    return await review.save_checklist(get_context(), version_id, body)


@router.post("/versions/{version_id}/approve", response_model=ReviewVersionOut)
async def approve(version_id: uuid.UUID, body: DecisionIn, _: PlatformOperatorMfaDep) -> ReviewVersionOut:
    return await review.approve(
        get_context(), version_id, message=body.message, internal_note=body.internal_note
    )


@router.post("/versions/{version_id}/request-changes", response_model=ReviewVersionOut)
async def request_changes(
    version_id: uuid.UUID, body: DecisionIn, _: PlatformOperatorMfaDep
) -> ReviewVersionOut:
    return await review.request_changes(
        get_context(), version_id, message=body.message, internal_note=body.internal_note
    )


@router.post("/versions/{version_id}/reject", response_model=ReviewVersionOut)
async def reject(version_id: uuid.UUID, body: DecisionIn, _: PlatformOperatorMfaDep) -> ReviewVersionOut:
    return await review.reject(
        get_context(), version_id, message=body.message, internal_note=body.internal_note
    )


@router.post("/versions/{version_id}/rerun-checks", response_model=ReviewVersionOut)
async def rerun_checks(version_id: uuid.UUID, _: PlatformOperatorMfaDep) -> ReviewVersionOut:
    return await review.rerun_checks(get_context(), version_id)


@router.get("/listings", response_model=list[PlatformListingOut])
async def listings(_: PlatformOperatorMfaDep) -> list[PlatformListingOut]:
    return await review.list_listings(get_context())


@router.post("/listings/{listing_id}/suspend", response_model=PlatformListingOut)
async def suspend(
    listing_id: uuid.UUID, body: ListingActionIn, _: PlatformOperatorMfaDep
) -> PlatformListingOut:
    return await review.suspend_listing(get_context(), listing_id, reason=body.reason)


@router.post("/listings/{listing_id}/reinstate", response_model=PlatformListingOut)
async def reinstate(
    listing_id: uuid.UUID, body: ListingActionIn, _: PlatformOperatorMfaDep
) -> PlatformListingOut:
    return await review.reinstate_listing(get_context(), listing_id, reason=body.reason)


@router.post("/listings/{listing_id}/delist", response_model=PlatformListingOut)
async def delist(
    listing_id: uuid.UUID, body: ListingActionIn, _: PlatformOperatorMfaDep
) -> PlatformListingOut:
    return await review.delist_listing(get_context(), listing_id, reason=body.reason)


@router.put("/operators/{operator_id}/roles", response_model=OperatorRolesOut)
async def operator_roles(
    operator_id: uuid.UUID, body: OperatorRolesIn, _: PlatformOperatorMfaDep
) -> OperatorRolesOut:
    return await review.set_operator_roles(get_context(), operator_id, list(body.roles))
