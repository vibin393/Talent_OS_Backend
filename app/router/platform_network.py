# ruff: file-ignore[line-too-long]
"""Platform Network Moderator console (Epic 75, spec A5; ``/api/v1/platform/network``). Platform operator + MFA; the
``network_moderator`` role is required for every action (403 ``platform_role_required``). Verify, reject, suspend,
reinstate, remove, release-hold and uphold / dismiss report are ★ (human platform operators only). Each action needs a
reason of >= 10 characters and is recorded in ``network_moderation_actions`` and the platform audit log."""

import uuid

from fastapi import APIRouter

from app.core.context import get_context
from app.dependencies.platform import PlatformOperatorMfaDep
from app.schemas.job_network import (
    OperatorRolesIn,
    PlatformListingOut,
    PlatformMemberOut,
    PlatformNetworkAnalyticsOut,
    PlatformReportOut,
    ReasonIn,
)
from app.services.job_network import moderation

router = APIRouter(prefix="/platform/network", tags=["platform-network"])


@router.get("/verification-queue", response_model=list[PlatformMemberOut])
async def verification_queue(_: PlatformOperatorMfaDep) -> list[PlatformMemberOut]:
    return await moderation.verification_queue(get_context())


@router.get("/members", response_model=list[PlatformMemberOut])
async def members(_: PlatformOperatorMfaDep) -> list[PlatformMemberOut]:
    return await moderation.members(get_context())


@router.get("/held-listings", response_model=list[PlatformListingOut])
async def held_listings(_: PlatformOperatorMfaDep) -> list[PlatformListingOut]:
    return await moderation.held_listings(get_context())


@router.get("/reports", response_model=list[PlatformReportOut])
async def open_reports(_: PlatformOperatorMfaDep) -> list[PlatformReportOut]:
    return await moderation.open_reports(get_context())


@router.get("/analytics", response_model=PlatformNetworkAnalyticsOut)
async def analytics(_: PlatformOperatorMfaDep) -> PlatformNetworkAnalyticsOut:
    return await moderation.analytics(get_context())


# ---------------------------------------------------------------- members (★)
@router.post("/members/{member_id}/verify", response_model=PlatformMemberOut)
async def verify_member(member_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformMemberOut:
    return await moderation.verify_member(get_context(), member_id, reason=body.reason, approve=True)


@router.post("/members/{member_id}/reject", response_model=PlatformMemberOut)
async def reject_member(member_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformMemberOut:
    return await moderation.verify_member(get_context(), member_id, reason=body.reason, approve=False)


@router.post("/members/{member_id}/suspend", response_model=PlatformMemberOut)
async def suspend_member(member_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformMemberOut:
    return await moderation.suspend_member(get_context(), member_id, reason=body.reason)


@router.post("/members/{member_id}/reinstate", response_model=PlatformMemberOut)
async def reinstate_member(member_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformMemberOut:
    return await moderation.reinstate_member(get_context(), member_id, reason=body.reason)


# ---------------------------------------------------------------- listings (★)
@router.post("/listings/{listing_id}/suspend", response_model=PlatformListingOut)
async def suspend_listing(listing_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformListingOut:
    return await moderation.suspend_listing(get_context(), listing_id, reason=body.reason)


@router.post("/listings/{listing_id}/reinstate", response_model=PlatformListingOut)
async def reinstate_listing(listing_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformListingOut:
    return await moderation.reinstate_listing(get_context(), listing_id, reason=body.reason)


@router.post("/listings/{listing_id}/remove", response_model=PlatformListingOut)
async def remove_listing(listing_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformListingOut:
    return await moderation.remove_listing(get_context(), listing_id, reason=body.reason)


@router.post("/listings/{listing_id}/release-hold", response_model=PlatformListingOut)
async def release_hold(listing_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> PlatformListingOut:
    return await moderation.release_hold(get_context(), listing_id, reason=body.reason)


# ---------------------------------------------------------------- reports (★)
@router.post("/reports/{report_id}/uphold", status_code=204)
async def uphold_report(report_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> None:
    await moderation.decide_report(get_context(), report_id, reason=body.reason, uphold=True)


@router.post("/reports/{report_id}/dismiss", status_code=204)
async def dismiss_report(report_id: uuid.UUID, body: ReasonIn, _: PlatformOperatorMfaDep) -> None:
    await moderation.decide_report(get_context(), report_id, reason=body.reason, uphold=False)


# ---------------------------------------------------------------- operator roles
@router.put("/operators/{operator_id}/roles", response_model=list[str])
async def set_roles(operator_id: uuid.UUID, body: OperatorRolesIn, _: PlatformOperatorMfaDep) -> list[str]:
    return await moderation.set_operator_roles(get_context(), operator_id, body.roles)
