# ruff: noqa: E501
"""Listing drafts and submissions (Epic 74, story 74.5): wrappers over Epic 69's partner functions (which take the
``DeveloperContext``) adding the org role, the org audit log and the conformance-run rule: ``submit`` needs a
``passed`` run of the **same** ``manifest_sha256`` finished within 30 days, whose signed report is attached."""

import uuid
from datetime import timedelta

from sqlalchemy import select

from app.core.database import platform_session
from app.core.developer import DeveloperContext
from app.core.errors import ConflictError, NotFound
from app.core.settings import get_settings
from app.models.developer import ConformanceRun
from app.models.marketplace import MarketplaceListing, MarketplaceListingVersion
from app.models.oauth import App
from app.schemas.marketplace import ListingDraftIn, PartnerListingOut, VersionDraftIn, VersionOut
from app.services import marketplace_service
from app.services.developer_portal import audit_log
from app.services.developer_portal.common import now, require_role


async def listing_draft(dctx: DeveloperContext, app_id: uuid.UUID, data: ListingDraftIn) -> PartnerListingOut:
    org = require_role(dctx, "developer")
    out = await marketplace_service.create_listing_draft(dctx, app_id=app_id, data=data)
    await audit_log.record("listing.saved", partner_org_id=org, account_id=dctx.developer_id, target_type="listing", target_id=out.id, app_id=app_id)
    return out


async def save_version(dctx: DeveloperContext, listing_id: uuid.UUID, data: VersionDraftIn) -> VersionOut:
    org = require_role(dctx, "developer")
    out = await marketplace_service.save_version_draft(dctx, listing_id, data)
    await audit_log.record("listing.saved", partner_org_id=org, account_id=dctx.developer_id, target_type="listing_version", target_id=out.id)
    return out


async def submit(dctx: DeveloperContext, version_id: uuid.UUID, *, conformance_run_id: uuid.UUID) -> VersionOut:
    org = require_role(dctx, "developer")
    async with platform_session() as db:
        found = (
            await db.execute(
                select(MarketplaceListingVersion, MarketplaceListing).join(MarketplaceListing, MarketplaceListing.id == MarketplaceListingVersion.listing_id)
                .where(MarketplaceListingVersion.id == version_id)
            )
        ).first()  # fmt: skip
        if found is None or found[1].partner_org_id != org:
            raise NotFound("Version not found")
        listing = found[1]
        app = await db.get(App, listing.app_id) if listing.app_id else None
        run = await db.get(ConformanceRun, conformance_run_id)
        max_age = timedelta(days=get_settings().conformance_report_max_age_days)
        if (
            app is None or run is None or run.app_id != app.id or run.status != "passed" or not run.report
            or run.manifest_sha256 != app.manifest_sha256 or run.finished_at is None or now() - run.finished_at > max_age
        ):  # fmt: skip
            raise ConflictError("Attach a passed conformance report for this app version (30 days at most)", code="conformance_report_required")
        report = dict(run.report)
    out = await marketplace_service.submit_version(dctx, version_id, conformance_report=report)
    await audit_log.record("listing.submitted", partner_org_id=org, account_id=dctx.developer_id, target_type="listing_version", target_id=version_id, run_id=conformance_run_id)
    return out


async def withdraw(dctx: DeveloperContext, version_id: uuid.UUID) -> VersionOut:
    org = require_role(dctx, "developer")
    out = await marketplace_service.withdraw_version(dctx, version_id)
    await audit_log.record("listing.withdrawn", partner_org_id=org, account_id=dctx.developer_id, target_type="listing_version", target_id=version_id)
    return out


async def submissions(dctx: DeveloperContext, org_id: uuid.UUID) -> list[dict[str, object]]:
    require_role(dctx, "viewer", org_id)
    versions = await marketplace_service.partner_versions(dctx, org_id)
    async with platform_session() as db:
        names = {
            lid: (name, app_id)
            for lid, name, app_id in (await db.execute(select(MarketplaceListing.id, MarketplaceListing.name, MarketplaceListing.app_id).where(MarketplaceListing.partner_org_id == org_id))).all()
        }
    return [{**v.model_dump(mode="json"), "app_name": names.get(v.listing_id, ("", None))[0], "app_id": str(names.get(v.listing_id, ("", None))[1] or "")} for v in versions]


__all__ = ["listing_draft", "save_version", "submissions", "submit", "withdraw"]
