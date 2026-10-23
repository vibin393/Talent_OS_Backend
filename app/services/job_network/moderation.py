# ruff: file-ignore[line-too-long]
"""Platform moderation (Epic 75, spec A5; FR-389). Every action requires a human platform operator (``require_human``;
automated actors -> 403 ``human_only_action``) holding the ``network_moderator`` role (else 403
``platform_role_required``). Verify, reject, suspend, reinstate, remove, release-hold, uphold / dismiss report are ★.
Every action writes ``network_moderation_actions`` and a platform audit entry; reasons are >= 10 chars."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import AUTOMATED_KINDS, human_only_action
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import Forbidden, NotFound, ValidationFailed
from app.models.identity import PlatformOperator
from app.models.job_network import NetworkListing, NetworkMember, NetworkModerationAction, NetworkReport
from app.schemas.job_network import (
    PlatformListingOut,
    PlatformMemberOut,
    PlatformNetworkAnalyticsOut,
    PlatformReportOut,
)
from app.services.job_network.constants import AUTO_SUSPEND_VIOLATIONS, VIOLATION_WINDOW_DAYS

ROLE = "network_moderator"


def _operator_id(ctx: RequestContext) -> uuid.UUID:
    principal = ctx.principal
    if ctx.actor_kind in AUTOMATED_KINDS or getattr(principal, "type", None) != "platform":
        raise human_only_action()
    pid = getattr(principal, "id", None)
    assert pid is not None
    return pid


async def require_role(ctx: RequestContext) -> uuid.UUID:
    operator_id = _operator_id(ctx)
    async with platform_session() as pdb:
        op = await pdb.get(PlatformOperator, operator_id)
        roles = list(op.roles or []) if op is not None else []
    if ROLE not in roles:
        raise Forbidden("You need the Network Moderator role", code="platform_role_required")
    return operator_id


def _reason(reason: str) -> str:
    if not reason or len(reason.strip()) < 10:
        raise ValidationFailed("A reason of at least 10 characters is required", code="reason_required")
    return reason.strip()


def _action(pdb: AsyncSession, *, target_type: str, target_id: uuid.UUID, action: str, reason: str, operator_id: uuid.UUID) -> None:
    pdb.add(
        NetworkModerationAction(
            id=uuid.uuid4(), target_type=target_type, target_id=target_id, action=action, reason=reason,
            operator_id=operator_id, created_at=datetime.now(UTC),
        )
    )
    audit.record(
        f"job_network.{action}", target_type, target_id, session=pdb, actor_type="platform", actor_id=operator_id,
    )


# ---------------------------------------------------------------- reads (queues)
async def verification_queue(ctx: RequestContext) -> list[PlatformMemberOut]:
    await require_role(ctx)
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(NetworkMember)
                    .where(NetworkMember.verification_status == "pending")
                    .order_by(NetworkMember.joined_at)
                )
            ).scalars()
        )
    return [_member_out(m) for m in rows]


async def members(ctx: RequestContext) -> list[PlatformMemberOut]:
    await require_role(ctx)
    async with platform_session() as pdb:
        rows = list((await pdb.execute(select(NetworkMember).order_by(NetworkMember.display_name))).scalars())
    return [_member_out(m) for m in rows]


async def held_listings(ctx: RequestContext) -> list[PlatformListingOut]:
    await require_role(ctx)
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(NetworkListing, NetworkMember)
                    .join(NetworkMember, NetworkMember.id == NetworkListing.member_id)
                    .where(NetworkListing.status.in_(("held", "pending_checks")))
                    .order_by(NetworkListing.synced_at.desc())
                )
            ).all()
        )
    return [
        PlatformListingOut(id=row.id, title=row.title, agency=mem.display_name, status=row.status, hold_reasons=list(row.hold_reasons or []))
        for row, mem in rows
    ]


async def open_reports(ctx: RequestContext) -> list[PlatformReportOut]:
    await require_role(ctx)
    async with platform_session() as pdb:
        rows = list(
            (
                await pdb.execute(
                    select(NetworkReport, NetworkListing.title)
                    .join(NetworkListing, NetworkListing.id == NetworkReport.listing_id)
                    .where(NetworkReport.status == "open")
                    .order_by(NetworkReport.created_at)
                )
            ).all()
        )
    return [
        PlatformReportOut(id=r.id, listing_id=r.listing_id, listing_title=title, reason=r.reason, details=r.details, status=r.status, created_at=r.created_at)
        for r, title in rows
    ]


def _member_out(m: NetworkMember) -> PlatformMemberOut:
    return PlatformMemberOut(
        id=m.id, display_name=m.display_name, public_slug=m.public_slug, status=m.status,
        verification_status=m.verification_status, violation_count=m.violation_count,
        suspended_reason=m.suspended_reason, requested_at=m.joined_at,
    )


# ---------------------------------------------------------------- member actions (★)
async def verify_member(ctx: RequestContext, member_id: uuid.UUID, *, reason: str, approve: bool) -> PlatformMemberOut:
    operator_id = await require_role(ctx)
    reason = _reason(reason)
    async with platform_session() as pdb:
        member = (await pdb.execute(select(NetworkMember).where(NetworkMember.id == member_id).with_for_update())).scalar_one_or_none()
        if member is None:
            raise NotFound("Member not found")
        if approve:
            member.verification_status = "verified"
            member.verified_at = datetime.now(UTC)
            member.verified_by = operator_id
            if member.status == "pending":
                member.status = "active"
            action = "verify"
        else:
            member.verification_status = "rejected"
            action = "reject_verification"
        _action(pdb, target_type="member", target_id=member.id, action=action, reason=reason, operator_id=operator_id)
        tenant_id = member.tenant_id
        out = _member_out(member)
        await pdb.commit()
    if approve:
        _enqueue_sync_tenant(tenant_id)
    _notify_membership(tenant_id)
    return out


async def suspend_member(ctx: RequestContext, member_id: uuid.UUID, *, reason: str) -> PlatformMemberOut:
    return await _member_status(ctx, member_id, reason=reason, suspend=True)


async def reinstate_member(ctx: RequestContext, member_id: uuid.UUID, *, reason: str) -> PlatformMemberOut:
    return await _member_status(ctx, member_id, reason=reason, suspend=False)


async def _member_status(ctx: RequestContext, member_id: uuid.UUID, *, reason: str, suspend: bool) -> PlatformMemberOut:
    operator_id = await require_role(ctx)
    reason = _reason(reason)
    async with platform_session() as pdb:
        member = (await pdb.execute(select(NetworkMember).where(NetworkMember.id == member_id).with_for_update())).scalar_one_or_none()
        if member is None:
            raise NotFound("Member not found")
        if suspend:
            member.status = "suspended"
            member.suspended_at = datetime.now(UTC)
            member.suspended_reason = reason
        else:
            member.status = "active" if member.verification_status == "verified" else "pending"
            member.suspended_reason = None
        _action(pdb, target_type="member", target_id=member.id, action=("suspend" if suspend else "reinstate"), reason=reason, operator_id=operator_id)
        tenant_id = member.tenant_id
        out = _member_out(member)
        await pdb.commit()
    if suspend:
        _enqueue_unlist_tenant(tenant_id)
    else:
        _enqueue_sync_tenant(tenant_id)
    _notify_membership(tenant_id)
    return out


# ---------------------------------------------------------------- listing actions (★)
async def suspend_listing(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    return await _listing_action(ctx, listing_id, reason=reason, action="suspend")


async def reinstate_listing(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    return await _listing_action(ctx, listing_id, reason=reason, action="reinstate")


async def remove_listing(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    return await _listing_action(ctx, listing_id, reason=reason, action="remove")


async def release_hold(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    return await _listing_action(ctx, listing_id, reason=reason, action="release_hold")


async def _listing_action(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str, action: str) -> PlatformListingOut:
    operator_id = await require_role(ctx)
    reason = _reason(reason)
    async with platform_session() as pdb:
        row = (
            await pdb.execute(
                select(NetworkListing, NetworkMember)
                .join(NetworkMember, NetworkMember.id == NetworkListing.member_id)
                .where(NetworkListing.id == listing_id)
                .with_for_update(of=NetworkListing)
            )
        ).first()
        if row is None:
            raise NotFound("Listing not found")
        listing, member = row
        if action == "suspend":
            listing.status = "suspended"
            listing.description_html = ""
            listing.description_text = ""
        elif action == "remove":
            listing.status = "removed"
            listing.description_html = ""
            listing.description_text = ""
        elif action in ("reinstate", "release_hold"):
            listing.status = "listed"
            listing.hold_reasons = []
            listing.listed_at = listing.listed_at or datetime.now(UTC)
        _action(pdb, target_type="listing", target_id=listing.id, action=action, reason=reason, operator_id=operator_id)
        out = PlatformListingOut(id=listing.id, title=listing.title, agency=member.display_name, status=listing.status, hold_reasons=list(listing.hold_reasons or []))
        tenant_id = listing.tenant_id
        posting_ref = listing.posting_ref
        await pdb.commit()
    if action in ("suspend", "remove"):
        _notify_listing_suspended(tenant_id)
    if action in ("reinstate", "release_hold"):
        _enqueue_sync_posting_by_posting(tenant_id, posting_ref)
    return out


# ---------------------------------------------------------------- reports (★)
async def decide_report(ctx: RequestContext, report_id: uuid.UUID, *, reason: str, uphold: bool) -> None:
    operator_id = await require_role(ctx)
    reason = _reason(reason)
    auto_suspend_tenant: uuid.UUID | None = None
    async with platform_session() as pdb:
        report = (await pdb.execute(select(NetworkReport).where(NetworkReport.id == report_id).with_for_update())).scalar_one_or_none()
        if report is None:
            raise NotFound("Report not found")
        report.status = "upheld" if uphold else "dismissed"
        report.decided_by = operator_id
        report.decided_at = datetime.now(UTC)
        _action(pdb, target_type="listing", target_id=report.listing_id, action=("uphold_report" if uphold else "dismiss_report"), reason=reason, operator_id=operator_id)
        if uphold:
            member = (
                await pdb.execute(
                    select(NetworkMember)
                    .join(NetworkListing, NetworkListing.member_id == NetworkMember.id)
                    .where(NetworkListing.id == report.listing_id)
                    .with_for_update(of=NetworkMember)
                )
            ).scalar_one_or_none()
            if member is not None:
                member.violation_count = (member.violation_count or 0) + 1
                recent = await _recent_upheld(pdb, member.id)
                if recent >= AUTO_SUSPEND_VIOLATIONS and member.status != "suspended":
                    member.status = "suspended"
                    member.suspended_at = datetime.now(UTC)
                    member.suspended_reason = "Automatically suspended: repeated upheld violations"
                    _action(pdb, target_type="member", target_id=member.id, action="suspend", reason="Auto-suspended after 3 upheld violations in 180 days", operator_id=operator_id)
                    auto_suspend_tenant = member.tenant_id
        await pdb.commit()
    if auto_suspend_tenant is not None:
        _enqueue_unlist_tenant(auto_suspend_tenant)
        _notify_membership(auto_suspend_tenant)


async def _recent_upheld(pdb: AsyncSession, member_id: uuid.UUID) -> int:
    cutoff = datetime.now(UTC) - timedelta(days=VIOLATION_WINDOW_DAYS)
    return int(
        (
            await pdb.execute(
                select(func.count())
                .select_from(NetworkModerationAction)
                .where(
                    NetworkModerationAction.target_type == "listing",
                    NetworkModerationAction.action == "uphold_report",
                    NetworkModerationAction.created_at >= cutoff,
                    NetworkModerationAction.target_id.in_(
                        select(NetworkListing.id).where(NetworkListing.member_id == member_id)
                    ),
                )
            )
        ).scalar_one()
    )


# ---------------------------------------------------------------- analytics (platform aggregates only)
async def analytics(ctx: RequestContext) -> PlatformNetworkAnalyticsOut:
    await require_role(ctx)
    async with platform_session() as pdb:
        by_status = {
            str(k): int(v)
            for k, v in (await pdb.execute(select(NetworkListing.status, func.count()).group_by(NetworkListing.status))).all()
        }
        by_category = {
            str(k): int(v)
            for k, v in (
                await pdb.execute(
                    select(NetworkListing.category, func.count())
                    .where(NetworkListing.category.is_not(None))
                    .group_by(NetworkListing.category)
                )
            ).all()
        }
        by_country = {
            str(k): int(v)
            for k, v in (
                await pdb.execute(
                    select(NetworkListing.country, func.count())
                    .where(NetworkListing.country.is_not(None))
                    .group_by(NetworkListing.country)
                )
            ).all()
        }
    return PlatformNetworkAnalyticsOut(by_status=by_status, by_category=by_category, by_country=by_country)


# ---------------------------------------------------------------- roles (operator console)
async def set_operator_roles(ctx: RequestContext, operator_id: uuid.UUID, roles: list[str]) -> list[str]:
    _operator_id(ctx)  # human platform operator
    allowed = {"marketplace_reviewer", "network_moderator"}
    bad = [r for r in roles if r not in allowed]
    if bad:
        raise ValidationFailed("Unknown platform role", code="unknown_role", details={"roles": bad})
    async with platform_session() as pdb:
        op = await pdb.get(PlatformOperator, operator_id)
        if op is None:
            raise NotFound("Operator not found")
        op.roles = sorted(set(roles))
        await pdb.commit()
        return list(op.roles)


# ---------------------------------------------------------------- enqueue / notify helpers
def _enqueue_sync_tenant(tenant_id: uuid.UUID) -> None:
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.sync_tenant, str(tenant_id))


def _enqueue_unlist_tenant(tenant_id: uuid.UUID) -> None:
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.unlist_tenant, str(tenant_id))


def _enqueue_sync_posting_by_posting(tenant_id: uuid.UUID, posting_ref: uuid.UUID) -> None:
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.sync_posting_ref, str(tenant_id), str(posting_ref))


def _notify_membership(tenant_id: uuid.UUID) -> None:
    from loguru import logger

    logger.info("job_network: membership changed for tenant {}", tenant_id)


def _notify_listing_suspended(tenant_id: uuid.UUID) -> None:
    from loguru import logger

    logger.info("job_network: listing suspended for tenant {}", tenant_id)


__all__ = [
    "analytics",
    "decide_report",
    "held_listings",
    "members",
    "open_reports",
    "reinstate_listing",
    "reinstate_member",
    "release_hold",
    "remove_listing",
    "require_role",
    "set_operator_roles",
    "suspend_listing",
    "suspend_member",
    "verification_queue",
    "verify_member",
]
