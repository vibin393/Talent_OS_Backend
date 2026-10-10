# ruff: noqa: E501
"""Partner submissions (Epic 69, spec A7; consumed by Epic 74's developer portal with a ``DeveloperContext``).

draft -> submitted (snapshot of ``apps.manifest`` + scope reasons; the content is immutable from here) ->
automated_checks -> in_review (all checks pass) | changes_requested (any failure; no human review). ``withdraw_version``
returns an open submission to draft. Partners see decision messages, never internal notes or Tenant identities.
Status changes reach partners through ``register_partner_notifier`` (Epic 74 registers its email sender).
"""

import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import platform_session
from app.core.developer import DeveloperContext
from app.core.errors import ConflictError, Forbidden, NotFound, ValidationFailed
from app.core.oauth_scopes import is_sensitive, scopes
from app.models.marketplace import (
    LISTING_CATEGORIES,
    MarketplaceListing,
    MarketplaceListingVersion,
    MarketplaceReview,
)
from app.models.oauth import App, PartnerOrg
from app.schemas.marketplace import (
    CheckResultOut,
    ListingDraftIn,
    PartnerListingOut,
    VersionDraftIn,
    VersionOut,
)
from app.services.marketplace import checks, conformance_report

PartnerNotifier = Callable[[uuid.UUID, uuid.UUID, str], Awaitable[None]]
_notifiers: list[PartnerNotifier] = []
ChecksKicker = Callable[[uuid.UUID], None]
_kicker: ChecksKicker | None = None
EDITABLE = ("draft", "changes_requested")
WITHDRAWABLE = ("submitted", "automated_checks", "in_review", "changes_requested")
CONTENT_FIELDS = (
    "description_md",
    "screenshots",
    "privacy_url",
    "dpa_url",
    "india_transfer_statement",
    "security_questionnaire",
    "changelog",
)


def register_partner_notifier(fn: PartnerNotifier) -> None:
    if fn not in _notifiers:
        _notifiers.append(fn)


def unregister_partner_notifier(fn: PartnerNotifier) -> None:
    if fn in _notifiers:
        _notifiers.remove(fn)


def set_checks_kicker(fn: ChecksKicker | None) -> None:
    global _kicker
    _kicker = fn


async def notify_partner(partner_org_id: uuid.UUID | None, version_id: uuid.UUID, status: str) -> None:
    if partner_org_id is None:
        return
    for fn in list(_notifiers):
        try:
            await fn(partner_org_id, version_id, status)
        except Exception:
            logger.exception("Partner notifier failed for version {}", version_id)


def _now() -> datetime:
    return datetime.now(UTC)


def _listing_out(row: MarketplaceListing) -> PartnerListingOut:
    return PartnerListingOut(
        id=row.id, slug=str(row.slug), name=row.name, tagline=row.tagline, category=row.category, status=row.status,
        app_id=row.app_id, current_version_id=row.current_version_id, install_count=row.install_count,
        last_reviewed_at=row.last_reviewed_at,
    )  # fmt: skip


async def version_out(pdb: AsyncSession, v: MarketplaceListingVersion) -> VersionOut:
    messages = [
        m
        for m in (
            await pdb.execute(
                select(MarketplaceReview.message)
                .where(MarketplaceReview.version_id == v.id, MarketplaceReview.message.is_not(None))
                .order_by(MarketplaceReview.at)
            )
        ).scalars()
        if m
    ]
    return VersionOut(
        id=v.id, listing_id=v.listing_id, version_no=v.version_no, status=v.status, manifest_version=v.manifest_version,
        manifest_sha256=v.manifest_sha256, description_md=v.description_md, screenshots=list(v.screenshots or []),
        privacy_url=v.privacy_url, dpa_url=v.dpa_url, india_transfer_statement=v.india_transfer_statement,
        changelog=v.changelog, check_results=[CheckResultOut(**c) for c in v.check_results or []],
        submitted_at=v.submitted_at, decided_at=v.decided_at, decision_message=v.decision_message,
        published_at=v.published_at, messages=messages,
    )  # fmt: skip


def _require_org(dctx: DeveloperContext) -> uuid.UUID:
    if dctx.partner_org_id is None:
        raise Forbidden("Join or create a partner organisation first", code="partner_org_required")
    return dctx.partner_org_id


async def _owned_listing(
    pdb: AsyncSession, dctx: DeveloperContext, listing_id: uuid.UUID
) -> MarketplaceListing:
    org = _require_org(dctx)
    row = (
        await pdb.execute(
            select(MarketplaceListing).where(MarketplaceListing.id == listing_id).with_for_update()
        )
    ).scalar_one_or_none()
    if row is None or row.partner_org_id != org:
        raise NotFound("Listing not found", code="listing_not_found")
    return row


async def _owned_version(
    pdb: AsyncSession, dctx: DeveloperContext, version_id: uuid.UUID
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
    listing = await _owned_listing(pdb, dctx, v.listing_id)
    return v, listing


async def create_listing_draft(
    dctx: DeveloperContext, *, app_id: uuid.UUID, data: ListingDraftIn
) -> PartnerListingOut:
    org_id = _require_org(dctx)
    if data.category not in LISTING_CATEGORIES:
        raise ValidationFailed("Unknown category", details={"fields": {"category": "Unknown"}})
    async with platform_session() as pdb:
        org = await pdb.get(PartnerOrg, org_id)
        if org is None or org.verification_status != "verified" or not dctx.partner_verified:
            raise ConflictError(
                "Only verified partners can publish to the Marketplace", code="partner_not_verified"
            )
        app = await pdb.get(App, app_id)
        if app is None or app.partner_org_id != org_id or app.publisher_kind != "partner":
            raise NotFound("App not found")
        if (
            await pdb.execute(select(MarketplaceListing.id).where(MarketplaceListing.app_id == app_id))
        ).first():
            raise ConflictError("This app already has a listing", code="listing_exists")
        if (
            await pdb.execute(select(MarketplaceListing.id).where(MarketplaceListing.slug == data.slug))
        ).first():
            raise ConflictError("This listing address is taken", code="slug_taken")
        row = MarketplaceListing(
            id=uuid.uuid4(), slug=data.slug, kind="app", app_id=app_id, publisher_kind="partner", partner_org_id=org_id,
            category=data.category, name=data.name, tagline=data.tagline, status="draft",
            supported_countries=[c.upper()[:2] for c in data.supported_countries] or ["IN"], pricing_note=data.pricing_note,
            support_email=data.support_email, support_url=data.support_url, logo_file_key=data.logo_file_key,
        )  # fmt: skip
        pdb.add(row)
        await pdb.flush()
        return _listing_out(row)


async def save_version_draft(
    dctx: DeveloperContext, listing_id: uuid.UUID, data: VersionDraftIn
) -> VersionOut:
    async with platform_session() as pdb:
        listing = await _owned_listing(pdb, dctx, listing_id)
        v = (
            await pdb.execute(
                select(MarketplaceListingVersion)
                .where(
                    MarketplaceListingVersion.listing_id == listing.id,
                    MarketplaceListingVersion.status.in_(EDITABLE),
                )
                .order_by(MarketplaceListingVersion.version_no.desc())
                .limit(1)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if v is None:
            app = await pdb.get(App, listing.app_id)
            assert app is not None
            next_no = (
                await pdb.execute(
                    select(func.coalesce(func.max(MarketplaceListingVersion.version_no), 0)).where(
                        MarketplaceListingVersion.listing_id == listing.id
                    )
                )
            ).scalar_one() + 1
            v = MarketplaceListingVersion(
                id=uuid.uuid4(), listing_id=listing.id, version_no=next_no, manifest_version=app.manifest_version,
                manifest_sha256=app.manifest_sha256, manifest_snapshot={}, description_md="", status="draft",
            )  # fmt: skip
            pdb.add(v)
        for field in CONTENT_FIELDS:
            setattr(v, field, getattr(data, field))
        v.screenshots = list(data.screenshots)
        await pdb.flush()
        return await version_out(pdb, v)


def _snapshot(app: App) -> dict[str, Any]:
    return {
        "manifest": dict(app.manifest or {}), "requested_scopes": list(app.requested_scopes),
        "scope_reasons": dict(app.scope_reasons or {}), "redirect_uris": list(app.redirect_uris or []),
        "client_type": app.client_type, "grant_types": list(app.grant_types), "resources": list(app.resources),
    }  # fmt: skip


async def submit_version(
    dctx: DeveloperContext, version_id: uuid.UUID, *, conformance_report: dict[str, Any]
) -> VersionOut:
    async with platform_session() as pdb:
        v, listing = await _owned_version(pdb, dctx, version_id)
        if v.status not in EDITABLE:
            raise ConflictError(
                "Only a draft or a version with requested changes can be submitted",
                code="version_not_editable",
            )
        app = await pdb.get(App, listing.app_id)
        assert app is not None
        catalogue = scopes()
        reasons = dict(app.scope_reasons or {})
        v.manifest_version, v.manifest_sha256, v.manifest_snapshot = (
            app.manifest_version,
            app.manifest_sha256,
            _snapshot(app),
        )
        v.scopes = [
            {"key": s, "reason": str(reasons.get(s) or ""), "sensitive": s in catalogue and is_sensitive(s)}
            for s in app.requested_scopes
        ]
        v.data_flows = list((app.manifest or {}).get("data_flows") or [])
        v.egress_hosts = sorted(
            {str(h)[:255] for h in (conformance_report or {}).get("observed_hosts") or []}
        )
        v.conformance_report = dict(conformance_report or {}) or None
        v.status, v.submitted_by_kind, v.submitted_by_id, v.submitted_at = (
            "submitted",
            "developer",
            dctx.developer_id,
            _now(),
        )
        v.check_results = []
        await pdb.flush()
        out = await version_out(pdb, v)
        org_id = listing.partner_org_id
    await notify_partner(org_id, version_id, "submitted")
    if _kicker is not None:
        _kicker(version_id)
    return out


async def withdraw_version(dctx: DeveloperContext, version_id: uuid.UUID) -> VersionOut:
    async with platform_session() as pdb:
        v, _ = await _owned_version(pdb, dctx, version_id)
        if v.status not in WITHDRAWABLE:
            raise ConflictError("This version can no longer be withdrawn", code="version_not_withdrawable")
        v.status, v.claimed_by, v.claimed_at = "draft", None, None
        await pdb.flush()
        return await version_out(pdb, v)


async def partner_versions(dctx: DeveloperContext, partner_org_id: uuid.UUID) -> list[VersionOut]:
    if dctx.partner_org_id != partner_org_id:
        raise Forbidden("Not a member of this organisation", code="partner_org_forbidden")
    async with platform_session() as pdb:
        rows = (
            await pdb.execute(
                select(MarketplaceListingVersion)
                .join(MarketplaceListing, MarketplaceListing.id == MarketplaceListingVersion.listing_id)
                .where(MarketplaceListing.partner_org_id == partner_org_id)
                .order_by(MarketplaceListingVersion.created_at.desc())
            )
        ).scalars()
        return [await version_out(pdb, v) for v in rows]


# ---------------------------------------------------------------- automated checks (task marketplace.run_checks)
async def run_checks(version_id: uuid.UUID, *, transport: Any = None) -> list[dict[str, Any]]:
    """Run the automated checks of a submitted version: ``automated_checks`` -> ``in_review`` | ``changes_requested``."""
    async with platform_session() as pdb:
        v = (
            await pdb.execute(
                select(MarketplaceListingVersion)
                .where(MarketplaceListingVersion.id == version_id)
                .with_for_update()
            )
        ).scalar_one_or_none()
        if v is None or v.status not in ("submitted", "automated_checks"):
            return []
        listing = await pdb.get(MarketplaceListing, v.listing_id)
        app = await pdb.get(App, listing.app_id) if listing is not None and listing.app_id else None
        if listing is None or app is None:
            return []
        v.status = "automated_checks"
        await pdb.flush()
        snap = dict(v.manifest_snapshot or {})

        class _Frozen:  # the submitted snapshot, not the live App row
            manifest = snap.get("manifest") or {}
            requested_scopes = snap.get("requested_scopes") or []
            scope_reasons = snap.get("scope_reasons") or {}
            redirect_uris = snap.get("redirect_uris") or []
            client_type = snap.get("client_type") or "confidential"

        results = list(checks.run_manifest_checks(_Frozen, kind="partner"))
        results.append(await checks.check_urls_reachable(
            {"privacy_url": v.privacy_url, "dpa_url": v.dpa_url, "support_url": listing.support_url}, transport=transport,
        ))  # fmt: skip
        problems = await conformance_report.verify(
            v.conformance_report, app_id=app.id, manifest_sha256=v.manifest_sha256
        )
        results.append(checks.CheckResult(
            "conformance_report", not problems,
            None if not problems else ("conformance_report_missing" if problems == ["conformance_report_missing"] else ", ".join(problems)),
        ))  # fmt: skip
        results.append(checks.check_listing_text(listing.name, listing.tagline, v.description_md or ""))
        results.append(await checks.check_assets(listing.logo_file_key, list(v.screenshots or [])))
        results.append(checks.check_transfer_statement(list(v.data_flows or []), v.india_transfer_statement))
        v.check_results = [r.as_dict() for r in results]
        v.status = "in_review" if all(r.passed for r in results) else "changes_requested"
        await pdb.flush()
        status, org_id = v.status, listing.partner_org_id
        out = list(v.check_results)
    await notify_partner(org_id, version_id, status)
    return out


__all__ = [
    "create_listing_draft",
    "notify_partner",
    "partner_versions",
    "register_partner_notifier",
    "run_checks",
    "save_version_draft",
    "set_checks_kicker",
    "submit_version",
    "unregister_partner_notifier",
    "version_out",
    "withdraw_version",
]
