# ruff: noqa: E501
"""Membership, settings and per-demand opt-in (Epic 75, stories 75.1 / 75.5; FR-386).

Tenant settings / choices live in a ``tenant_session`` (RLS); the network's view of the agency (``network_members``)
and the public index live in a ``platform_session``. The tenant id always comes from ``ctx``, never a request field.
"""

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tenancy
from app.core.actors import acting_user_id, actor_from_ctx, require_human
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.core.settings import get_settings
from app.events.job_network import NetworkMembershipChanged
from app.models.job_network import JobNetworkChoice, JobNetworkSettings, NetworkListing, NetworkMember
from app.models.legal_entities import GstRegistration, LegalEntity
from app.models.portal import CareerSite
from app.schemas.job_network import (
    ListingEligibilityOut,
    ListingStatusOut,
    MyListingOut,
    MyListingsPage,
    MyListingsQuery,
    NetworkJoinIn,
    NetworkSettingsOut,
    NetworkSettingsPatch,
)
from app.services.job_network import listings
from app.services.job_network.constants import NETWORK_TERMS_VERSION
from app.services.portal import careers, projection
from app.services.portal.common import plain_text


def _tenant_id(ctx: RequestContext) -> uuid.UUID:
    assert ctx.tenant_id is not None
    return ctx.tenant_id


def _careers_base_url(slug: str) -> str:
    s = get_settings()
    if s.careers_root_domain:
        return f"https://{slug}.{s.careers_root_domain}"
    return f"{s.frontend_url.rstrip('/')}/careers/{slug}"


async def get_or_create_settings(db: AsyncSession, tenant_id: uuid.UUID) -> JobNetworkSettings:
    row = (
        await db.execute(select(JobNetworkSettings).where(JobNetworkSettings.tenant_id == tenant_id))
    ).scalar_one_or_none()
    if row is None:
        row = JobNetworkSettings(id=uuid.uuid4(), tenant_id=tenant_id)
        db.add(row)
        await db.flush()
    return row


async def _member(tenant_id: uuid.UUID) -> NetworkMember | None:
    async with platform_session() as pdb:
        return (
            await pdb.execute(select(NetworkMember).where(NetworkMember.tenant_id == tenant_id))
        ).scalar_one_or_none()


async def _live_count(tenant_id: uuid.UUID) -> int:
    async with platform_session() as pdb:
        return int(
            (
                await pdb.execute(
                    select(func.count())
                    .select_from(NetworkListing)
                    .where(NetworkListing.tenant_id == tenant_id, NetworkListing.status == "listed")
                )
            ).scalar_one()
        )


async def _verification_missing(db: AsyncSession, tenant_id: uuid.UUID) -> list[str]:
    missing: list[str] = []
    has_entity = (
        await db.execute(
            select(LegalEntity.id).where(LegalEntity.status == "active").limit(1)
        )
    ).first() is not None
    if not has_entity:
        missing.append("legal_entity")
    has_gstin = (
        await db.execute(
            select(GstRegistration.id).where(GstRegistration.status == "active").limit(1)
        )
    ).first() is not None
    if not has_gstin:
        missing.append("gstin")
    site = await db.get(CareerSite, tenant_id)
    if site is None or not site.enabled:
        missing.append("career_site")
    return missing


async def _settings_out(
    settings_row: JobNetworkSettings, member: NetworkMember | None, sandbox: bool
) -> NetworkSettingsOut:
    live_count = 0 if member is None else await _live_count(settings_row.tenant_id)
    return NetworkSettingsOut(
        joined=bool(settings_row.joined),
        member_status=(member.status if member else None),
        verification_status=(member.verification_status if member else None),
        list_mode=settings_row.list_mode,
        show_logo=bool(settings_row.show_logo),
        exclude_client_ids=list(settings_row.exclude_client_ids or []),
        terms_version_current=NETWORK_TERMS_VERSION,
        terms_accepted_at=settings_row.terms_accepted_at,
        listing_limit=(member.listing_limit if member else get_settings().network_listing_limit_default),
        live_count=live_count,
        suspended_reason=(member.suspended_reason if member else None),
        sandbox=sandbox,
    )


# ================================================================ reads
async def get_settings_view(ctx: RequestContext, db: AsyncSession) -> NetworkSettingsOut:
    tenant_id = _tenant_id(ctx)
    row = await get_or_create_settings(db, tenant_id)
    member = await _member(tenant_id)
    return await _settings_out(row, member, await tenancy.is_sandbox(tenant_id))


# ================================================================ join / leave / settings
async def join(ctx: RequestContext, db: AsyncSession, data: NetworkJoinIn) -> NetworkSettingsOut:
    require_human(actor_from_ctx(ctx))
    tenant_id = _tenant_id(ctx)
    if data.terms_version != NETWORK_TERMS_VERSION:
        raise ConflictError("The listing terms have changed; review and accept the current version", code="terms_outdated")
    if data.list_mode not in ("all", "choose"):
        raise ValidationFailed("Invalid listing mode", code="invalid_list_mode")
    missing = await _verification_missing(db, tenant_id)
    if missing:
        raise ValidationFailed(
            "Your tenant must be verified before joining the Job Network",
            code="network_verification_required",
            details={"missing": missing},
        )
    row = await get_or_create_settings(db, tenant_id)
    row.joined = True
    row.list_mode = data.list_mode
    row.show_logo = data.show_logo
    row.terms_version = data.terms_version
    row.terms_accepted_at = datetime.now(UTC)
    row.terms_accepted_by = acting_user_id(ctx.principal)
    row.updated_by = acting_user_id(ctx.principal)

    sandbox = await tenancy.is_sandbox(tenant_id)
    member: NetworkMember | None = None
    if not sandbox:
        site = await careers.site_for_tenant(db, tenant_id)
        slug = site.slug if site else str(tenant_id)
        member = await _upsert_member(tenant_id, site, slug)
        row.member_id = member.id
        emit(db, NetworkMembershipChanged(tenant_id=tenant_id, status=member.status, verification_status=member.verification_status, actor_kind=ctx.actor_kind))
        _enqueue_sync_tenant(tenant_id)
        _notify_platform_verification(tenant_id)
    await db.flush()
    return await _settings_out(row, member, sandbox)


async def _upsert_member(tenant_id: uuid.UUID, site: object, slug: str) -> NetworkMember:
    display_name = getattr(site, "display_name", None) or getattr(site, "tenant_name", None) or slug
    about_html = getattr(site, "about_html", None)
    logo_file_id = getattr(site, "logo_file_id", None)
    logo_url = f"{get_settings().frontend_url.rstrip('/')}{projection.asset_url(slug, 'logo')}" if logo_file_id else None
    async with platform_session() as pdb:
        member = (
            await pdb.execute(select(NetworkMember).where(NetworkMember.tenant_id == tenant_id).with_for_update())
        ).scalar_one_or_none()
        now = datetime.now(UTC)
        if member is None:
            member = NetworkMember(
                id=uuid.uuid4(), tenant_id=tenant_id, public_slug=slug, display_name=display_name[:200],
                logo_url=logo_url, about_text=(plain_text(about_html)[:2000] if about_html else None),
                careers_base_url=_careers_base_url(slug), status="pending", verification_status="pending",
                listing_limit=get_settings().network_listing_limit_default, joined_at=now,
            )
            pdb.add(member)
        else:
            member.display_name = display_name[:200]
            member.logo_url = logo_url
            member.about_text = plain_text(about_html)[:2000] if about_html else None
            member.careers_base_url = _careers_base_url(slug)
            if member.status in ("left",):
                member.status = "active" if member.verification_status == "verified" else "pending"
            elif member.status in ("suspended",) and member.verification_status == "rejected":
                member.verification_status = "pending"
            member.left_at = None
            member.joined_at = member.joined_at or now
        await pdb.flush()
        pdb.expunge(member)
        return member


async def update_settings(ctx: RequestContext, db: AsyncSession, data: NetworkSettingsPatch) -> NetworkSettingsOut:
    tenant_id = _tenant_id(ctx)
    row = await get_or_create_settings(db, tenant_id)
    if data.list_mode is not None:
        if data.list_mode not in ("all", "choose"):
            raise ValidationFailed("Invalid listing mode", code="invalid_list_mode")
        row.list_mode = data.list_mode
    if data.show_logo is not None:
        row.show_logo = data.show_logo
    if data.exclude_client_ids is not None:
        row.exclude_client_ids = list(data.exclude_client_ids)
    row.updated_by = acting_user_id(ctx.principal)
    await db.flush()
    member = await _member(tenant_id)
    if member is not None:
        _enqueue_sync_tenant(tenant_id)
    return await _settings_out(row, member, await tenancy.is_sandbox(tenant_id))


async def leave(ctx: RequestContext, db: AsyncSession, *, reason: str | None) -> NetworkSettingsOut:  # noqa: ARG001 - reason reserved for an audit note
    require_human(actor_from_ctx(ctx))
    tenant_id = _tenant_id(ctx)
    row = await get_or_create_settings(db, tenant_id)
    row.joined = False
    row.updated_by = acting_user_id(ctx.principal)
    await db.flush()
    member: NetworkMember | None = None
    async with platform_session() as pdb:
        member = (
            await pdb.execute(select(NetworkMember).where(NetworkMember.tenant_id == tenant_id).with_for_update())
        ).scalar_one_or_none()
        if member is not None:
            member.status = "left"
            member.left_at = datetime.now(UTC)
            await pdb.flush()
            emit(pdb, NetworkMembershipChanged(tenant_id=tenant_id, status="left", verification_status=member.verification_status, actor_kind=ctx.actor_kind))
            pdb.expunge(member)
    if member is not None:
        _enqueue_unlist_tenant(tenant_id)
    return await _settings_out(row, member, await tenancy.is_sandbox(tenant_id))


# ================================================================ eligibility / per-demand opt-in
async def _choice(db: AsyncSession, tenant_id: uuid.UUID, demand_id: uuid.UUID) -> JobNetworkChoice | None:
    return (
        await db.execute(
            select(JobNetworkChoice).where(
                JobNetworkChoice.tenant_id == tenant_id, JobNetworkChoice.demand_id == demand_id
            )
        )
    ).scalar_one_or_none()


async def eligibility(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID) -> ListingEligibilityOut:
    tenant_id = _tenant_id(ctx)
    row = await get_or_create_settings(db, tenant_id)
    member = await _member(tenant_id)
    choice = await _choice(db, tenant_id, demand_id)
    data, posting = await listings.load_eligibility_input(
        db, demand_id=demand_id, exclude_client_ids=tuple(row.exclude_client_ids or []),
        joined=bool(row.joined), member_status=(member.status if member else None),
        verification_status=(member.verification_status if member else None),
        sandbox=await tenancy.is_sandbox(tenant_id), list_mode=row.list_mode,
        include_choice=(choice.include if choice else None),
        live_count=(await _live_count(tenant_id) if member else 0),
        listing_limit=(member.listing_limit if member else get_settings().network_listing_limit_default),
    )
    ok, reasons = listings.eligible(data)
    status_out = await _listing_status(tenant_id, posting.id) if posting is not None else None
    return ListingEligibilityOut(eligible=ok, reasons=reasons, listing=status_out)


async def _listing_status(tenant_id: uuid.UUID, posting_ref: uuid.UUID) -> ListingStatusOut | None:
    async with platform_session() as pdb:
        listing = (
            await pdb.execute(
                select(NetworkListing).where(
                    NetworkListing.tenant_id == tenant_id, NetworkListing.posting_ref == posting_ref
                )
            )
        ).scalar_one_or_none()
    if listing is None:
        return None
    return ListingStatusOut(
        listing_id=listing.id, status=listing.status, hold_reasons=list(listing.hold_reasons or []),
        listed_at=listing.listed_at,
    )


async def set_choice(ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID, include: bool) -> ListingStatusOut:
    tenant_id = _tenant_id(ctx)
    row = await get_or_create_settings(db, tenant_id)
    member = await _member(tenant_id)
    # enforce the per-member live-listing limit on an explicit opt-in
    if include and member is not None and await _live_count(tenant_id) >= member.listing_limit:
        raise ValidationFailed("You have reached your network listing limit", code="network_listing_limit")
    _, posting = await listings.load_eligibility_input(
        db, demand_id=demand_id, exclude_client_ids=tuple(row.exclude_client_ids or []),
        joined=bool(row.joined), member_status=(member.status if member else None),
        verification_status=(member.verification_status if member else None),
        sandbox=await tenancy.is_sandbox(tenant_id), list_mode=row.list_mode, include_choice=include,
        live_count=0, listing_limit=(member.listing_limit if member else 500),
    )
    choice = await _choice(db, tenant_id, demand_id)
    if choice is None:
        choice = JobNetworkChoice(
            id=uuid.uuid4(), tenant_id=tenant_id, demand_id=demand_id,
            posting_id=(posting.id if posting else uuid.uuid4()), include=include,
            chosen_by=acting_user_id(ctx.principal), chosen_at=datetime.now(UTC),
        )
        db.add(choice)
    else:
        choice.include = include
        choice.chosen_by = acting_user_id(ctx.principal)
        choice.chosen_at = datetime.now(UTC)
        if posting is not None:
            choice.posting_id = posting.id
    await db.flush()
    if member is not None:
        _enqueue_sync_posting(tenant_id, demand_id)
    return ListingStatusOut(status=("pending_checks" if include else "unlisted"))


async def my_listings(ctx: RequestContext, db: AsyncSession, q: MyListingsQuery) -> MyListingsPage:  # noqa: ARG001 - listings live on the platform index (own session)
    tenant_id = _tenant_id(ctx)
    async with platform_session() as pdb:
        stmt = select(NetworkListing).where(NetworkListing.tenant_id == tenant_id)
        if q.status:
            stmt = stmt.where(NetworkListing.status == q.status)
        if q.q and q.q.strip():
            stmt = stmt.where(NetworkListing.title.ilike(f"%{q.q.strip()[:100]}%"))
        total = int((await pdb.execute(select(func.count()).select_from(stmt.subquery()))).scalar_one())
        rows = list(
            (
                await pdb.execute(
                    stmt.order_by(NetworkListing.published_at.desc().nulls_last())
                    .offset((q.page - 1) * q.page_size)
                    .limit(q.page_size)
                )
            ).scalars()
        )
        from app.services.job_network import stats as stats_svc

        items = []
        for r in rows:
            agg = await stats_svc.listing_totals(pdb, r.id)
            items.append(
                MyListingOut(
                    listing_id=r.id, posting_ref=r.posting_ref, title=r.title, status=r.status,
                    hold_reasons=list(r.hold_reasons or []), views=agg["views"], apply_clicks=agg["apply_clicks"],
                    impressions=agg["impressions"], listed_at=r.listed_at,
                )
            )
    return MyListingsPage(items=items, total=total, page=q.page, page_size=q.page_size)


async def request_review(ctx: RequestContext, db: AsyncSession, listing_id: uuid.UUID, note: str) -> ListingStatusOut:  # noqa: ARG001 - db kept for signature symmetry
    """Request a moderator re-check of a held listing. The listing surfaces in the moderator held-listings queue and
    the note is recorded; returns the current listing status (404 if it is not this Tenant's listing)."""
    tenant_id = _tenant_id(ctx)
    async with platform_session() as pdb:
        listing = (
            await pdb.execute(
                select(NetworkListing).where(
                    NetworkListing.id == listing_id, NetworkListing.tenant_id == tenant_id
                )
            )
        ).scalar_one_or_none()
        if listing is None:
            from app.core.errors import NotFound

            raise NotFound("Listing not found", code="listing_not_found")
        from loguru import logger

        logger.info("job_network: review requested for listing {} ({})", listing_id, (note or "")[:80])
        return ListingStatusOut(
            listing_id=listing.id, status=listing.status, hold_reasons=list(listing.hold_reasons or []),
            listed_at=listing.listed_at,
        )


# ================================================================ task enqueue helpers
def _enqueue_sync_tenant(tenant_id: uuid.UUID) -> None:
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.sync_tenant, str(tenant_id))


def _enqueue_unlist_tenant(tenant_id: uuid.UUID) -> None:
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.unlist_tenant, str(tenant_id))


def _enqueue_sync_posting(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> None:
    from app.tasks import job_network as tasks
    from app.tasks.runtime import enqueue

    enqueue(tasks.sync_posting, str(tenant_id), str(demand_id))


def _notify_platform_verification(tenant_id: uuid.UUID) -> None:
    # Platform operators are alerted to a new verification request (platform channel). Kept best-effort so a
    # notification problem never blocks the join.
    from loguru import logger

    logger.info("job_network: verification requested for tenant {}", tenant_id)


__all__ = [
    "eligibility",
    "get_or_create_settings",
    "get_settings_view",
    "join",
    "leave",
    "my_listings",
    "set_choice",
    "update_settings",
]
