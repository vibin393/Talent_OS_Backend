"""Vendors contract (Epic 13 AD-36 signatures; filled by Epic 17).

* ``get_vendor_summary`` -- id, human id, legal name, status (404 when not visible).
* ``assert_vendor_can_submit`` -- 409 ``vendor_not_active`` unless the vendor is ``active``.
* ``sourcing_for_application`` -- the accepted vendor submission linked to the Application (Epic 19 uses it to
  default a Placement's vendor and vendor rate), ``None`` for non-vendor applications.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.vendors import VendorSubmission
from app.schemas.vendors import VendorSourcing, VendorSummary
from app.services.vendors.common import conflict, get_vendor


async def get_vendor_summary(ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID) -> VendorSummary:  # ruff: ignore[unused-function-argument]
    v = await get_vendor(db, vendor_id)
    return VendorSummary(id=v.id, human_id=v.human_id, legal_name=v.legal_name, status=v.status)


async def assert_vendor_can_submit(ctx: RequestContext, db: AsyncSession, vendor_id: uuid.UUID) -> None:  # ruff: ignore[unused-function-argument]
    v = await get_vendor(db, vendor_id)
    if v.status != "active":
        raise conflict("vendor_not_active", "The vendor is not active", status=v.status)


async def sourcing_for_application(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    application_id: uuid.UUID,
) -> VendorSourcing | None:
    sub = (
        await db.execute(
            select(VendorSubmission)
            .where(VendorSubmission.application_id == application_id, VendorSubmission.status == "accepted")
            .order_by(VendorSubmission.reviewed_at.desc().nulls_last())
            .limit(1)
        )
    ).scalar_one_or_none()
    if sub is None:
        return None
    return VendorSourcing(
        vendor_id=sub.vendor_id,
        vendor_submission_id=sub.id,
        vendor_rate=sub.vendor_rate,
        rate_unit=sub.rate_unit,
        currency=sub.currency,
    )


async def candidate_vendor_owner(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    candidate_id: uuid.UUID,
    client_id: uuid.UUID,
) -> dict[str, object] | None:
    """Epic 61 (additive read): the vendor owning ``candidate_id`` for ``client_id`` now.

    An active, unexpired ``vendor_candidate_ownerships`` row as
    ``{vendor_id, vendor_name, vendor_submission_id, expires_at}``, else None (internal only).
    """
    from datetime import UTC, datetime

    from app.models.vendors import Vendor, VendorCandidateOwnership

    row = (
        await db.execute(
            select(VendorCandidateOwnership, Vendor.legal_name)
            .join(Vendor, Vendor.id == VendorCandidateOwnership.vendor_id)
            .where(
                VendorCandidateOwnership.candidate_id == candidate_id,
                VendorCandidateOwnership.client_id == client_id,
                VendorCandidateOwnership.ended_at.is_(None),
                VendorCandidateOwnership.expires_at > datetime.now(UTC),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return None
    own, name = row
    return {
        "vendor_id": own.vendor_id,
        "vendor_name": name,
        "vendor_submission_id": own.vendor_submission_id,
        "expires_at": own.expires_at,
    }
