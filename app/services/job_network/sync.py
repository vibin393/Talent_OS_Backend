# ruff: file-ignore[line-too-long]
"""Index sync orchestration (Epic 75, spec A3). Opens a ``tenant_session`` to project the careers-public data, then a
``platform_session`` to write the index; runs the automated checks on a changed listing. Idempotent."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import tenancy
from app.core.database import platform_session, tenant_session
from app.core.events import emit
from app.events.job_network import NetworkListingChanged
from app.models.job_network import JobNetworkChoice, JobNetworkSettings, NetworkListing, NetworkMember
from app.services.job_network import checks, index, listings
from app.services.job_network.constants import UNLISTED_RETENTION_DAYS


async def _member(pdb: AsyncSession, tenant_id: uuid.UUID) -> NetworkMember | None:
    return (
        await pdb.execute(select(NetworkMember).where(NetworkMember.tenant_id == tenant_id))
    ).scalar_one_or_none()


async def _live_count(pdb: AsyncSession, tenant_id: uuid.UUID) -> int:
    return int(
        (
            await pdb.execute(
                select(func.count())
                .select_from(NetworkListing)
                .where(NetworkListing.tenant_id == tenant_id, NetworkListing.status == "listed")
            )
        ).scalar_one()
    )


async def _duplicate_spam(pdb: AsyncSession, member_id: uuid.UUID, listing_id: uuid.UUID, content_sha256: str) -> bool:
    """>= 5 listed rows of the member with the same ``content_sha256`` (location-insensitive hash is approximated by the
    full hash here) flags duplicate spam. The near-duplicate MinHash path is a known refinement."""
    count = int(
        (
            await pdb.execute(
                select(func.count())
                .select_from(NetworkListing)
                .where(
                    NetworkListing.member_id == member_id,
                    NetworkListing.status == "listed",
                    NetworkListing.content_sha256 == content_sha256,
                    NetworkListing.id != listing_id,
                )
            )
        ).scalar_one()
    )
    return count >= 4


async def sync_posting(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> str:
    """Re-evaluate one demand's posting against the index. Returns the resulting status (or ``ignored``)."""
    async with platform_session() as pdb:
        member = await _member(pdb, tenant_id)
    if member is None or member.status != "active" or member.verification_status != "verified":
        # not a verified, active member -> ensure nothing of this demand is listed
        await _unlist_demand(tenant_id, demand_id)
        return "unlisted"
    if await tenancy.is_sandbox(tenant_id):
        await _unlist_demand(tenant_id, demand_id)
        return "ignored"

    async with tenant_session(tenant_id) as tdb:
        settings_row = (
            await tdb.execute(select(JobNetworkSettings).where(JobNetworkSettings.tenant_id == tenant_id))
        ).scalar_one_or_none()
        choice = (
            await tdb.execute(
                select(JobNetworkChoice).where(
                    JobNetworkChoice.tenant_id == tenant_id, JobNetworkChoice.demand_id == demand_id
                )
            )
        ).scalar_one_or_none()
        exclude = tuple((settings_row.exclude_client_ids if settings_row else []) or [])
        list_mode = settings_row.list_mode if settings_row else "choose"
        joined = bool(settings_row and settings_row.joined)
        elig_in, _posting = await listings.load_eligibility_input(
            tdb, demand_id=demand_id, exclude_client_ids=exclude, joined=joined, member_status=member.status,
            verification_status=member.verification_status, sandbox=False, list_mode=list_mode,
            include_choice=(choice.include if choice else None), live_count=0, listing_limit=member.listing_limit,
        )
        ok, _reasons = listings.eligible(elig_in)
        data = None
        if ok:
            data = await listings.project(tdb, tenant_id, demand_id, careers_base_url=member.careers_base_url)

    if not ok or data is None:
        await _unlist_demand(tenant_id, demand_id)
        return "unlisted"

    async with platform_session() as pdb:
        # enforce the live-listing limit (oldest-beyond-limit stay unlisted in ``all`` mode).
        if (choice is None or choice.include is not True) and await _live_count(pdb, tenant_id) >= member.listing_limit:
            existing = (
                await pdb.execute(
                    select(NetworkListing).where(
                        NetworkListing.tenant_id == tenant_id, NetworkListing.posting_ref == data.posting_ref
                    )
                )
            ).scalar_one_or_none()
            if existing is None or existing.status != "listed":
                return "limit_reached"
        result = await index.upsert(pdb, member_id=member.id, data=data)
        status = result.status
        if result.changed:
            findings = checks.run(title=data.title, description_text=data.description_text)
            codes = sorted({f.code for f in findings})
            if await _duplicate_spam(pdb, member.id, result.listing_id, data.content_sha256):
                codes = sorted({*codes, "duplicate_spam"})
            status = "held" if codes else "listed"
            await index.set_decision(pdb, result.listing_id, status=status, hold_reasons=codes)
        emit(pdb, NetworkListingChanged(tenant_id=tenant_id, demand_id=demand_id, listing_id=result.listing_id, status=status))
        await pdb.commit()
    return status


async def _unlist_demand(tenant_id: uuid.UUID, demand_id: uuid.UUID) -> None:
    """Unlist every index row of the demand's posting(s). The posting_ref is found via the stored listing's
    ``posting_ref`` (the index does not store demand_id); we match on the demand's choice posting id when present and
    otherwise unlist any listing whose posting belongs to this demand via the tenant session."""
    async with tenant_session(tenant_id) as tdb:
        posting_ids = [
            r[0]
            for r in (
                await tdb.execute(
                    select(JobNetworkChoice.posting_id).where(JobNetworkChoice.demand_id == demand_id)
                )
            ).all()
        ]
        from app.models.demands import DemandPosting

        posting_ids += [
            r[0]
            for r in (
                await tdb.execute(select(DemandPosting.id).where(DemandPosting.demand_id == demand_id))
            ).all()
        ]
    async with platform_session() as pdb:
        for pid in set(posting_ids):
            await index.unlist(pdb, tenant_id=tenant_id, posting_ref=pid, reason="not_eligible")
        await pdb.commit()


async def sync_posting_ref(tenant_id: uuid.UUID, posting_ref: uuid.UUID) -> str:
    """Re-sync by the careers posting id (used by moderator reinstate / release-hold, which only hold the listing)."""
    async with tenant_session(tenant_id) as tdb:
        from app.models.demands import DemandPosting

        demand_id = (
            await tdb.execute(select(DemandPosting.demand_id).where(DemandPosting.id == posting_ref))
        ).scalar_one_or_none()
    if demand_id is None:
        return "ignored"
    return await sync_posting(tenant_id, demand_id)


async def sync_tenant(tenant_id: uuid.UUID) -> int:
    """Re-sync every eligible demand of a tenant (join / settings change). Returns the number of demands processed."""
    async with tenant_session(tenant_id) as tdb:
        from app.models.demands import DemandPosting

        demand_ids = [
            r[0]
            for r in (
                await tdb.execute(select(DemandPosting.demand_id).where(DemandPosting.is_live.is_(True)))
            ).all()
        ]
    for did in set(demand_ids):
        await sync_posting(tenant_id, did)
    return len(set(demand_ids))


async def unlist_tenant(tenant_id: uuid.UUID) -> int:
    """Unlist and blank every listing of a tenant (leave / suspension / offboarding / became sandbox)."""
    async with platform_session() as pdb:
        n = await index.unlist_tenant(pdb, tenant_id, reason="tenant_left")
        await pdb.commit()
    return n


async def reconcile() -> int:
    """Beat safety-net: unlist rows whose ``valid_through`` has passed, and whose member left / is suspended."""
    now = datetime.now(UTC)
    async with platform_session() as pdb:
        expired = list(
            (
                await pdb.execute(
                    select(NetworkListing.tenant_id, NetworkListing.posting_ref).where(
                        NetworkListing.status == "listed",
                        NetworkListing.valid_through.is_not(None),
                        NetworkListing.valid_through < now,
                    )
                )
            ).all()
        )
        for tenant_id, posting_ref in expired:
            await index.unlist(pdb, tenant_id=tenant_id, posting_ref=posting_ref, reason="expired")
        # members that left / are suspended: unlist their still-visible rows.
        gone = list(
            (
                await pdb.execute(
                    select(NetworkMember.tenant_id).where(NetworkMember.status.in_(("left", "suspended")))
                )
            ).scalars()
        )
        for tenant_id in gone:
            await index.unlist_tenant(pdb, tenant_id, reason="member_inactive")
        await pdb.commit()
    return len(expired)


async def purge() -> int:
    """Delete unlisted / removed rows older than the retention window (keep ids only for 30 days for analytics joins)."""
    cutoff = datetime.now(UTC) - timedelta(days=UNLISTED_RETENTION_DAYS)
    from sqlalchemy import delete

    async with platform_session() as pdb:
        result = await pdb.execute(
            delete(NetworkListing).where(
                NetworkListing.status.in_(("unlisted", "removed")), NetworkListing.synced_at < cutoff
            )
        )
        await pdb.commit()
    return int(getattr(result, "rowcount", 0) or 0)


__all__ = ["purge", "reconcile", "sync_posting", "sync_posting_ref", "sync_tenant", "unlist_tenant"]
