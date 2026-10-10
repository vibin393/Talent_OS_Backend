# ruff: noqa: E501, RUF028, D401
"""Platform review of partner versions and listing operations (Epic 69, spec A7). Every action needs a human platform
operator (automated actors -> 403 ``human_only_action``) holding the ``marketplace_reviewer`` role (403
``platform_role_required``); approve, reject, suspend, reinstate and delist are ★. Every action writes a
``marketplace_reviews`` row (when a version is involved) and a platform audit entry.

Suspending a listing suspends the App platform-wide (``oauth_service.set_app_status`` +
``ecosystem_service.platform_set(scope="app")``: tokens fail everywhere within the 60 s caches) and marks every Tenant's
Installation ``suspended`` with an ``InstallationChanged(actor_kind="platform")`` (notification
``app_suspended_by_platform``).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.actors import AUTOMATED_KINDS, human_only_action
from app.core.context import RequestContext
from app.core.database import platform_session, tenant_session
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.events import emit
from app.events.ecosystem import InstallationChanged
from app.models.identity import PlatformOperator, Tenant
from app.models.marketplace import MarketplaceListing, MarketplaceListingVersion, MarketplaceReview
from app.models.oauth import App, AppInstallation, PartnerOrg
from app.schemas.marketplace import CheckResultOut
from app.schemas.platform_marketplace import (
    CHECKLIST_KEYS,
    ChecklistIn,
    OperatorRolesOut,
    PlatformListingOut,
    QueueItemOut,
    ReviewEntryOut,
    ReviewVersionOut,
)
from app.services import ecosystem_service, oauth_service
from app.services.marketplace import submissions

ROLE = "marketplace_reviewer"
PLATFORM_SUSPENSION_PREFIX = "Suspended by Talent OS"


def _now() -> datetime:
    return datetime.now(UTC)


def _operator_id(ctx: RequestContext) -> uuid.UUID:
    """A human platform operator (★ guard for the platform side)."""
    principal = ctx.principal
    if ctx.actor_kind in AUTOMATED_KINDS or getattr(principal, "type", None) != "platform":
        raise human_only_action()
    pid = getattr(principal, "id", None)
    assert pid is not None
    return pid


async def require_role(ctx: RequestContext, role: str = ROLE) -> uuid.UUID:
    operator_id = _operator_id(ctx)
    async with platform_session() as pdb:
        op = await pdb.get(PlatformOperator, operator_id)
        roles = list(op.roles or []) if op is not None else []
    if role not in roles:
        raise Forbidden("You need the Marketplace Reviewer role", code="platform_role_required")
    return operator_id


def _audit(
    pdb: AsyncSession, action: str, entity: str, entity_id: uuid.UUID, operator_id: uuid.UUID, **extra: Any
) -> None:
    audit.record(
        f"marketplace.{action}",
        entity,
        entity_id,
        session=pdb,
        actor_type="platform",
        actor_id=operator_id,
        **extra,
    )


def _review(pdb: AsyncSession, version_id: uuid.UUID, operator_id: uuid.UUID, action: str, *, checklist: dict[str, Any] | None = None,
            message: str | None = None, internal_note: str | None = None) -> None:  # fmt: skip
    pdb.add(MarketplaceReview(
        id=uuid.uuid4(), version_id=version_id, operator_id=operator_id, action=action, checklist=checklist,
        message=(message or None), internal_note=(internal_note or None), at=_now(),
    ))  # fmt: skip


async def _version(
    pdb: AsyncSession, version_id: uuid.UUID
) -> tuple[MarketplaceListingVersion, MarketplaceListing]:
    v = (
        await pdb.execute(
            select(MarketplaceListingVersion)
            .where(MarketplaceListingVersion.id == version_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if v is None:
        raise NotFound("Version not found")
    listing = (
        await pdb.execute(
            select(MarketplaceListing).where(MarketplaceListing.id == v.listing_id).with_for_update()
        )
    ).scalar_one()
    return v, listing


async def _current_checklist(pdb: AsyncSession, version_id: uuid.UUID) -> dict[str, Any]:
    row = (
        await pdb.execute(
            select(MarketplaceReview.checklist)
            .where(MarketplaceReview.version_id == version_id, MarketplaceReview.action == "checklist")
            .order_by(MarketplaceReview.at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return dict(row or {})


# ---------------------------------------------------------------- reads
async def queue(ctx: RequestContext, status: str | None = None) -> list[QueueItemOut]:
    _operator_id(ctx)
    statuses = (status,) if status else ("submitted", "automated_checks", "in_review", "changes_requested")
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(
                select(MarketplaceListingVersion, MarketplaceListing, PartnerOrg.name)
                .join(MarketplaceListing, MarketplaceListing.id == MarketplaceListingVersion.listing_id)
                .outerjoin(PartnerOrg, PartnerOrg.id == MarketplaceListing.partner_org_id)
                .where(MarketplaceListingVersion.status.in_(statuses), MarketplaceListing.kind == "app")
                .order_by(MarketplaceListingVersion.submitted_at)
            )
        ).all()
        return [
            QueueItemOut(
                version_id=v.id,
                listing_id=listing.id,
                app_name=listing.name,
                partner_name=org_name,
                version_no=v.version_no,
                submitted_at=v.submitted_at,
                status=v.status,
                checks_failed=sum(1 for c in v.check_results or [] if not c.get("passed")),
                reviewer_id=v.claimed_by,
            )  # fmt: skip
            for v, listing, org_name in rows
        ]


async def get_version(ctx: RequestContext, version_id: uuid.UUID) -> ReviewVersionOut:
    _operator_id(ctx)
    async with platform_session() as pdb:
        v = await pdb.get(MarketplaceListingVersion, version_id)
        if v is None:
            raise NotFound("Version not found")
        listing = await pdb.get(MarketplaceListing, v.listing_id)
        assert listing is not None
        org = await pdb.get(PartnerOrg, listing.partner_org_id) if listing.partner_org_id else None
        history = (
            await pdb.execute(
                select(MarketplaceReview)
                .where(MarketplaceReview.version_id == v.id)
                .order_by(MarketplaceReview.at)
            )
        ).scalars()
        return ReviewVersionOut(
            version_id=v.id, listing_id=listing.id, listing_slug=str(listing.slug), listing_status=listing.status,
            app_id=listing.app_id, app_name=listing.name, partner_name=org.name if org else None, version_no=v.version_no,
            status=v.status, manifest_snapshot=dict(v.manifest_snapshot or {}), scopes=list(v.scopes or []),
            data_flows=list(v.data_flows or []), egress_hosts=list(v.egress_hosts or []), description_md=v.description_md,
            privacy_url=v.privacy_url, dpa_url=v.dpa_url, india_transfer_statement=v.india_transfer_statement,
            security_questionnaire=v.security_questionnaire, changelog=v.changelog,
            check_results=[CheckResultOut(**c) for c in v.check_results or []], conformance_report=v.conformance_report,
            checklist=await _current_checklist(pdb, v.id), claimed_by=v.claimed_by,
            history=[ReviewEntryOut(action=h.action, operator_id=h.operator_id, checklist=h.checklist, message=h.message,
                                    internal_note=h.internal_note, at=h.at) for h in history],
        )  # fmt: skip


async def list_listings(ctx: RequestContext) -> list[PlatformListingOut]:
    _operator_id(ctx)
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(
                select(MarketplaceListing, PartnerOrg.name)
                .outerjoin(PartnerOrg, PartnerOrg.id == MarketplaceListing.partner_org_id)
                .where(MarketplaceListing.publisher_kind != "tenant")
                .order_by(MarketplaceListing.name)
            )
        ).all()
        return [
            PlatformListingOut(
                id=r.id,
                slug=str(r.slug),
                kind=r.kind,
                name=r.name,
                publisher_kind=r.publisher_kind,
                partner_name=org_name,
                category=r.category,
                status=r.status,
                install_count=r.install_count,
                last_reviewed_at=r.last_reviewed_at,
                suspended_reason=r.suspended_reason,
            )  # fmt: skip
            for r, org_name in rows
        ]


# ---------------------------------------------------------------- reviewer actions
async def claim(ctx: RequestContext, version_id: uuid.UUID) -> ReviewVersionOut:
    operator_id = await require_role(ctx)
    async with platform_session() as pdb:
        v, _ = await _version(pdb, version_id)
        if v.status != "in_review":
            raise ConflictError("Only versions in review can be claimed", code="version_not_in_review")
        previous = v.claimed_by
        v.claimed_by, v.claimed_at = operator_id, _now()
        _review(
            pdb,
            v.id,
            operator_id,
            "claim",
            internal_note=f"Taken over from {previous}" if previous and previous != operator_id else None,
        )
        _audit(pdb, "version_claim", "marketplace_listing_version", v.id, operator_id)
    return await get_version(ctx, version_id)


async def save_checklist(ctx: RequestContext, version_id: uuid.UUID, data: ChecklistIn) -> ReviewVersionOut:
    operator_id = await require_role(ctx)
    unknown = sorted(set(data.items) - set(CHECKLIST_KEYS))
    if unknown:
        raise ValidationFailed(
            "Unknown checklist items", details={"fields": dict.fromkeys(unknown, "Unknown")}
        )
    async with platform_session() as pdb:
        v, _ = await _version(pdb, version_id)
        if v.status != "in_review":
            raise ConflictError("Only versions in review have a checklist", code="version_not_in_review")
        merged = {**await _current_checklist(pdb, v.id), **{k: i.model_dump() for k, i in data.items.items()}}
        _review(pdb, v.id, operator_id, "checklist", checklist=merged, internal_note=data.internal_note)
        _audit(pdb, "version_checklist", "marketplace_listing_version", v.id, operator_id)
    return await get_version(ctx, version_id)


async def _decide(
    ctx: RequestContext, version_id: uuid.UUID, action: str, message: str, internal_note: str | None
) -> tuple[uuid.UUID, MarketplaceListingVersion, MarketplaceListing]:
    operator_id = await require_role(ctx)
    async with platform_session() as pdb:
        v, listing = await _version(pdb, version_id)
        if v.status != "in_review":
            raise ConflictError("Only versions in review can be decided", code="version_not_in_review")
        now = _now()
        if action == "approve":
            checklist = await _current_checklist(pdb, v.id)
            if not all(bool((checklist.get(k) or {}).get("ok")) for k in CHECKLIST_KEYS):
                raise ConflictError("Complete every checklist item first", code="checklist_incomplete")
            await pdb.execute(
                update(MarketplaceListingVersion)
                .where(
                    MarketplaceListingVersion.listing_id == listing.id,
                    MarketplaceListingVersion.status == "published",
                    MarketplaceListingVersion.id != v.id,
                )
                .values(status="superseded")
            )
            v.status = "approved"
            await pdb.flush()
            v.status, v.published_at = "published", now
            listing.status, listing.current_version_id, listing.last_reviewed_at = "published", v.id, now
        elif action == "request_changes":
            if not message.strip():
                raise ValidationFailed(
                    "A message to the partner is required", details={"fields": {"message": "Required"}}
                )
            v.status = "changes_requested"
        else:
            if not message.strip():
                raise ValidationFailed(
                    "A message to the partner is required", details={"fields": {"message": "Required"}}
                )
            v.status = "rejected"
            listing.last_reviewed_at = now
        v.decided_by, v.decided_at, v.decision_message = operator_id, now, message.strip() or None
        _review(pdb, v.id, operator_id, action, message=message.strip() or None, internal_note=internal_note)
        _audit(pdb, f"version_{action}", "marketplace_listing_version", v.id, operator_id, status=v.status)
        await pdb.flush()
        pdb.expunge(v)
        pdb.expunge(listing)
    return operator_id, v, listing


async def approve(
    ctx: RequestContext, version_id: uuid.UUID, *, message: str = "", internal_note: str | None = None
) -> ReviewVersionOut:
    """★ approve -> published (same transaction), previous published version superseded, App published."""
    _, v, listing = await _decide(ctx, version_id, "approve", message, internal_note)
    if listing.app_id is not None:
        async with platform_session() as pdb:
            await oauth_service.set_app_status(ctx, pdb, listing.app_id, "published", None)
        await _mark_app_reconsent(listing.app_id)
    await submissions.notify_partner(listing.partner_org_id, v.id, "published")
    return await get_version(ctx, version_id)


async def request_changes(
    ctx: RequestContext, version_id: uuid.UUID, *, message: str, internal_note: str | None = None
) -> ReviewVersionOut:
    _, v, listing = await _decide(ctx, version_id, "request_changes", message, internal_note)
    await submissions.notify_partner(listing.partner_org_id, v.id, "changes_requested")
    return await get_version(ctx, version_id)


async def reject(
    ctx: RequestContext, version_id: uuid.UUID, *, message: str, internal_note: str | None = None
) -> ReviewVersionOut:
    """★ reject -> ``rejected`` (terminal for that version)."""
    _, v, listing = await _decide(ctx, version_id, "reject", message, internal_note)
    await submissions.notify_partner(listing.partner_org_id, v.id, "rejected")
    return await get_version(ctx, version_id)


async def rerun_checks(ctx: RequestContext, version_id: uuid.UUID) -> ReviewVersionOut:
    operator_id = await require_role(ctx)
    async with platform_session() as pdb:
        v, _ = await _version(pdb, version_id)
        if v.status not in ("changes_requested", "in_review", "automated_checks", "submitted"):
            raise ConflictError("Checks can be rerun only for open submissions", code="version_not_open")
        if v.status == "changes_requested" and v.decided_by is not None:
            raise ConflictError(
                "A reviewer asked for changes; the partner must resubmit", code="version_not_open"
            )
        v.status = "submitted"
        _review(pdb, v.id, operator_id, "comment", internal_note="Automated checks rerun")
        _audit(pdb, "version_rerun_checks", "marketplace_listing_version", v.id, operator_id)
    await submissions.run_checks(version_id)
    return await get_version(ctx, version_id)


# ---------------------------------------------------------------- tenant fan-out
async def _for_installations(
    app_id: uuid.UUID, fn: Callable[[AsyncSession, list[AppInstallation]], Awaitable[None]]
) -> int:
    async with platform_session() as pdb:
        tenant_ids = list(
            (await pdb.execute(select(Tenant.id).where(Tenant.status.in_(("active", "suspended"))))).scalars()
        )
    touched = 0
    for tid in tenant_ids:
        async with tenant_session(tid) as db:
            rows = list(
                (
                    await db.execute(
                        select(AppInstallation).where(
                            AppInstallation.app_id == app_id, AppInstallation.status != "uninstalled"
                        )
                    )
                ).scalars()
            )
            if rows:
                await fn(db, rows)
                touched += len(rows)
    return touched


async def _mark_app_reconsent(app_id: uuid.UUID) -> int:
    async with platform_session() as pdb:
        app = await pdb.get(App, app_id)
        if app is None:
            return 0
        digest, version = app.manifest_sha256, app.manifest_version

    async def mark(db: AsyncSession, rows: list[AppInstallation]) -> None:
        for inst in rows:
            if inst.manifest_sha256 == digest or inst.pending_manifest_version == version:
                continue
            inst.pending_manifest_version = version
            if inst.status in ("active", "degraded"):
                inst.status = "reconsent_required"
            emit(db, InstallationChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, kind="app", app_id=app_id, connector_key=None,
                change="reconsent_required", status=inst.status, actor_kind="system",
            ))  # fmt: skip

    return await _for_installations(app_id, mark)


async def _listing_for_action(pdb: AsyncSession, listing_id: uuid.UUID) -> MarketplaceListing:
    listing = (
        await pdb.execute(
            select(MarketplaceListing).where(MarketplaceListing.id == listing_id).with_for_update()
        )
    ).scalar_one_or_none()
    if listing is None:
        raise NotFound("Listing not found")
    if listing.app_id is None:
        raise ConflictError(
            "Only app listings can be suspended, reinstated or delisted", code="listing_not_app"
        )
    return listing


async def _set_version_status(
    pdb: AsyncSession,
    listing: MarketplaceListing,
    status: str,
    operator_id: uuid.UUID,
    action: str,
    reason: str,
) -> None:
    if listing.current_version_id is not None:
        v = await pdb.get(MarketplaceListingVersion, listing.current_version_id)
        if v is not None:
            v.status = status
            _review(pdb, v.id, operator_id, action, message=reason)


async def suspend_listing(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    """★ suspend everywhere (FR-340: tokens fail within the 60 s caches; Tenants notified)."""
    operator_id = await require_role(ctx)
    async with platform_session() as pdb:
        listing = await _listing_for_action(pdb, listing_id)
        listing.status, listing.suspended_reason = "suspended", reason[:500]
        await _set_version_status(pdb, listing, "suspended", operator_id, "suspend", reason)
        _audit(pdb, "listing_suspend", "marketplace_listing", listing.id, operator_id, reason=reason[:500])
        app_id = listing.app_id
    assert app_id is not None
    await ecosystem_service.platform_set(
        "app", app_id=app_id, on=True, reason=reason, operator_id=operator_id
    )
    async with platform_session() as pdb:
        await oauth_service.set_app_status(ctx, pdb, app_id, "suspended", reason)

    async def suspend(db: AsyncSession, rows: list[AppInstallation]) -> None:
        for inst in rows:
            if inst.status == "suspended":
                continue
            inst.status, inst.suspended_reason = "suspended", f"{PLATFORM_SUSPENSION_PREFIX}: {reason}"[:500]
            await oauth_service.revoke_for(db, installation_id=inst.id, reason="suspended")
            emit(db, InstallationChanged(
                tenant_id=inst.tenant_id, installation_id=inst.id, kind="app", app_id=app_id, connector_key=None,
                change="suspended", status="suspended", actor_kind="platform",
            ))  # fmt: skip

    await _for_installations(app_id, suspend)
    oauth_service.clear_cache()
    return await _listing_out(listing_id)


async def reinstate_listing(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    """★ reverse a suspension: listing + version published, App published, platform-suspended Installations resume."""
    operator_id = await require_role(ctx)
    async with platform_session() as pdb:
        listing = await _listing_for_action(pdb, listing_id)
        if listing.status != "suspended":
            raise ConflictError("Only suspended listings can be reinstated", code="listing_not_suspended")
        listing.status, listing.suspended_reason = "published", None
        await _set_version_status(pdb, listing, "published", operator_id, "reinstate", reason)
        _audit(pdb, "listing_reinstate", "marketplace_listing", listing.id, operator_id, reason=reason[:500])
        app_id = listing.app_id
    assert app_id is not None
    await ecosystem_service.platform_set(
        "app", app_id=app_id, on=False, reason=reason, operator_id=operator_id
    )
    async with platform_session() as pdb:
        await oauth_service.set_app_status(ctx, pdb, app_id, "published", None)

    async def resume(db: AsyncSession, rows: list[AppInstallation]) -> None:
        for inst in rows:
            if inst.status == "suspended" and (inst.suspended_reason or "").startswith(
                PLATFORM_SUSPENSION_PREFIX
            ):
                inst.status, inst.suspended_reason = "active", None
                emit(db, InstallationChanged(
                    tenant_id=inst.tenant_id, installation_id=inst.id, kind="app", app_id=app_id, connector_key=None,
                    change="resumed", status="active", actor_kind="platform",
                ))  # fmt: skip

    await _for_installations(app_id, resume)
    oauth_service.clear_cache()
    return await _listing_out(listing_id)


async def delist_listing(ctx: RequestContext, listing_id: uuid.UUID, *, reason: str) -> PlatformListingOut:
    """★ delist: listing + version delisted, App retired; Installations stay suspended (Tenants can only uninstall)."""
    operator_id = await require_role(ctx)
    async with platform_session() as pdb:
        listing = await _listing_for_action(pdb, listing_id)
        listing.status, listing.suspended_reason = "delisted", reason[:500]
        await _set_version_status(pdb, listing, "delisted", operator_id, "delist", reason)
        _audit(pdb, "listing_delist", "marketplace_listing", listing.id, operator_id, reason=reason[:500])
        app_id = listing.app_id
    assert app_id is not None
    async with platform_session() as pdb:
        await oauth_service.set_app_status(ctx, pdb, app_id, "retired", reason)

    async def suspend(db: AsyncSession, rows: list[AppInstallation]) -> None:
        for inst in rows:
            if inst.status != "suspended":
                inst.status, inst.suspended_reason = "suspended", f"{PLATFORM_SUSPENSION_PREFIX}: delisted"
                await oauth_service.revoke_for(db, installation_id=inst.id, reason="suspended")
                emit(db, InstallationChanged(
                    tenant_id=inst.tenant_id, installation_id=inst.id, kind="app", app_id=app_id, connector_key=None,
                    change="suspended", status="suspended", actor_kind="platform",
                ))  # fmt: skip

    await _for_installations(app_id, suspend)
    oauth_service.clear_cache()
    return await _listing_out(listing_id)


async def _listing_out(listing_id: uuid.UUID) -> PlatformListingOut:
    async with platform_session() as pdb:
        r = await pdb.get(MarketplaceListing, listing_id)
        assert r is not None
        org = await pdb.get(PartnerOrg, r.partner_org_id) if r.partner_org_id else None
        return PlatformListingOut(
            id=r.id, slug=str(r.slug), kind=r.kind, name=r.name, publisher_kind=r.publisher_kind,
            partner_name=org.name if org else None, category=r.category, status=r.status, install_count=r.install_count,
            last_reviewed_at=r.last_reviewed_at, suspended_reason=r.suspended_reason,
        )  # fmt: skip


async def set_operator_roles(
    ctx: RequestContext, operator_id: uuid.UUID, roles: list[str]
) -> OperatorRolesOut:
    actor = _operator_id(ctx)
    if actor == operator_id:
        raise Forbidden("You cannot change your own roles", code="own_roles_forbidden")
    async with platform_session() as pdb:
        op = (
            await pdb.execute(
                select(PlatformOperator).where(PlatformOperator.id == operator_id).with_for_update()
            )
        ).scalar_one_or_none()
        if op is None:
            raise NotFound("Operator not found")
        before = list(op.roles or [])
        op.roles = sorted(set(roles))
        audit.record("platform.operator_roles", "platform_operator", op.id, session=pdb, actor_type="platform", actor_id=actor,
                     before=before, after=list(op.roles))  # fmt: skip
        return OperatorRolesOut(operator_id=op.id, roles=list(op.roles))


async def refresh_install_counts() -> int:
    """Daily aggregate (no tenant ids): live Installations per App / connector key across Tenants."""
    counts: dict[str, int] = {}
    async with platform_session() as pdb:
        tenant_ids = list((await pdb.execute(select(Tenant.id).where(Tenant.status == "active"))).scalars())
    for tid in tenant_ids:
        async with tenant_session(tid) as db:
            for kind, key in (await db.execute(
                select(AppInstallation.kind, AppInstallation.connector_key).where(AppInstallation.status != "uninstalled", AppInstallation.kind == "connector")
            )).all():  # fmt: skip
                counts[f"c:{key}"] = counts.get(f"c:{key}", 0) + 1
                del kind
            for (app_id,) in (
                await db.execute(
                    select(AppInstallation.app_id).where(
                        AppInstallation.status != "uninstalled", AppInstallation.kind == "app"
                    )
                )
            ).all():
                counts[f"a:{app_id}"] = counts.get(f"a:{app_id}", 0) + 1
    async with platform_session() as pdb:
        for row in (await pdb.execute(select(MarketplaceListing))).scalars():
            key = f"c:{row.connector_key}" if row.connector_key else f"a:{row.app_id}"
            row.install_count = counts.get(key, 0)
    return len(counts)


__all__ = [
    "ROLE",
    "approve",
    "claim",
    "delist_listing",
    "get_version",
    "list_listings",
    "queue",
    "refresh_install_counts",
    "reinstate_listing",
    "reject",
    "request_changes",
    "require_role",
    "rerun_checks",
    "save_checklist",
    "set_operator_roles",
    "suspend_listing",
]
