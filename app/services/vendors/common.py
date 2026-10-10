"""Shared helpers of the vendors domain (Epic 17): tenant / clock / errors / settings / lookups."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.vendors import Vendor, VendorSettings

DEFAULT_LABELS: dict[str, str] = {
    "submitted_to_vendor": "Received",
    "under_review": "Received",
    "accepted": "Shortlisted",
    "applied": "Shortlisted",
    "screening": "Shortlisted",
    "shortlisted": "Shortlisted",
    "submitted": "Submitted to client",
    "interviewing": "Interviewing",
    "selected": "Offered",
    "offered": "Offered",
    "hired": "Placed",
    "placement": "Placed",
    "rejected": "Not selected",
    "withdrawn": "Not selected",
}
# Vendor-visible status labels (category label only; never reason text).
STATUS_LABELS: dict[str, str] = {
    "submitted": "Received",
    "under_review": "Received",
    "accepted": "Shortlisted",
    "rejected": "Not selected",
    "duplicate": "Duplicate",
    "withdrawn": "Withdrawn",
}
CATEGORY_LABELS: dict[str, str] = {
    "owned_by_other_vendor": "Already represented",
    "existing_candidate": "Already in our database",
    "submitted_by_other_vendor": "Already submitted",
    "other": "Duplicate",
    "consent_withdrawn": "Candidate withdrew consent",
}
ATTESTATION_TEXT = (
    "I confirm that the candidate has authorised us to represent them for this role, that the "
    "information and "
    "resume are accurate, and that the candidate has been informed that their data will be shared "
    "with the hiring "
    "organisation."
)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "vendors needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def now() -> datetime:
    return datetime.now(UTC)


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    if getattr(principal, "type", None) == "user":
        return getattr(principal, "id", None)
    return None


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


def not_found(what: str = "Vendor") -> NotFound:
    return NotFound(f"{what} not found")


async def get_vendor(db: AsyncSession, vendor_id: uuid.UUID, *, lock: bool = False) -> Vendor:
    stmt = select(Vendor).where(Vendor.id == vendor_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found()
    return row


async def load_settings(db: AsyncSession) -> VendorSettings:
    """Return the tenant's vendor settings row (created lazily with the defaults)."""
    row = (await db.execute(select(VendorSettings))).scalar_one_or_none()
    if row is not None:
        return row
    await db.execute(
        pg_insert(VendorSettings)
        .values(id=uuid.uuid4(), tenant_id=tenant_of(db))
        .on_conflict_do_nothing(index_elements=["tenant_id"])
    )
    return (await db.execute(select(VendorSettings))).scalar_one()


def labels_of(settings: VendorSettings) -> dict[str, str]:
    return {**DEFAULT_LABELS, **(settings.vendor_labels or {})}
