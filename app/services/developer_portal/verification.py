# ruff: file-ignore[line-too-long]
"""Verified Partner applications (Epic 74, story 74.1, spec 0.3).

Owners save a draft and submit it (``partner_orgs.verification_status = 'pending'``). A platform reviewer (human
operator + MFA + platform role ``marketplace_reviewer``, Epic 69's ``review.require_role``) approves ★ (``verified``
for 12 months), requests changes, or rejects ★. Messages are visible to the org. ``expire_verifications`` (daily)
reminds owners at -30 / -7 days and marks ``expired`` 30 days after ``verification_expires_at``.
"""

import uuid
from datetime import timedelta
from typing import Any, Literal
from urllib.parse import urlsplit

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.database import platform_session
from app.core.developer import DeveloperContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.settings import get_settings
from app.models.developer import PartnerVerificationRequest
from app.models.oauth import PartnerOrg
from app.schemas.developer import VerificationIn, VerificationOut
from app.services.developer_portal import audit_log, notifier
from app.services.developer_portal.common import now, require_role
from app.services.legal_entities.gstin import validate_gstin

OPEN = ("draft", "pending", "changes_requested")


def _out(org: PartnerOrg, row: PartnerVerificationRequest | None) -> VerificationOut:
    return VerificationOut(
        id=row.id if row else None, partner_org_id=org.id, org_name=org.name, status=row.status if row else "none",
        payload=dict(row.payload or {}) if row else {}, message=row.message if row else None,
        submitted_at=row.submitted_at if row else None, decided_at=row.decided_at if row else None,
        org_verification_status=org.verification_status, verification_expires_at=org.verification_expires_at,
    )  # fmt: skip


def validate(data: VerificationIn, *, complete: bool) -> dict[str, str]:
    """Field errors (``{}`` = valid). ``complete`` enforces the required fields for submission."""
    errors: dict[str, str] = {}
    if data.tax_id and data.tax_id_kind == "gstin":
        problems = validate_gstin(data.tax_id)
        if problems:
            errors["tax_id"] = problems[0].message
    if data.website:
        parts = urlsplit(data.website)
        if parts.scheme != "https" or not parts.hostname:
            errors["website"] = "Enter an https address"
    if data.support_email and "@" not in data.support_email:
        errors["support_email"] = "Enter an email address"
    if not data.country.isalpha():
        errors["country"] = "Use a 2-letter country code"
    if complete:
        for field in ("legal_name", "tax_id", "website", "registered_address", "support_email"):
            if not str(getattr(data, field) or "").strip():
                errors.setdefault(field, "Required")
        if data.agreement_version != get_settings().developer_terms_version:
            errors["agreement_version"] = "Accept the current Partner Agreement"
        q = data.security_questionnaire
        if not q.data_location.strip() or not q.incident_contact.strip():
            errors.setdefault("security_questionnaire", "Answer every question")
    return errors


async def _open_row(db: AsyncSession, org_id: uuid.UUID) -> PartnerVerificationRequest | None:
    return (
        await db.execute(
            select(PartnerVerificationRequest).where(PartnerVerificationRequest.partner_org_id == org_id, PartnerVerificationRequest.status.in_(OPEN)).with_for_update()
        )
    ).scalar_one_or_none()


async def _latest(db: AsyncSession, org_id: uuid.UUID) -> PartnerVerificationRequest | None:
    return (
        await db.execute(
            select(PartnerVerificationRequest).where(PartnerVerificationRequest.partner_org_id == org_id).order_by(PartnerVerificationRequest.created_at.desc()).limit(1)
        )
    ).scalar_one_or_none()


async def get_verification(dctx: DeveloperContext, org_id: uuid.UUID) -> VerificationOut:
    require_role(dctx, "viewer", org_id)
    async with platform_session() as db:
        org = await db.get(PartnerOrg, org_id)
        if org is None:
            raise NotFound("Organisation not found")
        return _out(org, await _latest(db, org_id))


async def save_verification(dctx: DeveloperContext, org_id: uuid.UUID, data: VerificationIn) -> VerificationOut:
    require_role(dctx, "owner", org_id)
    errors = validate(data, complete=False)
    if errors:
        raise ValidationFailed("Check the highlighted fields", details={"fields": errors})
    async with platform_session() as db:
        org = await db.get(PartnerOrg, org_id)
        if org is None:
            raise NotFound("Organisation not found")
        row = await _open_row(db, org_id)
        if row is not None and row.status == "pending":
            raise ConflictError("Your application is under review", code="verification_pending")
        payload: dict[str, Any] = data.model_dump(mode="json", exclude={"evidence_file_ids"})
        if row is None:
            row = PartnerVerificationRequest(id=uuid.uuid4(), partner_org_id=org_id, status="draft", payload=payload)
            db.add(row)
        row.payload = payload
        row.evidence_file_keys = list(data.evidence_file_ids)
        await db.flush()
        return _out(org, row)


async def submit_verification(dctx: DeveloperContext, org_id: uuid.UUID) -> VerificationOut:
    require_role(dctx, "owner", org_id)
    async with platform_session() as db:
        org = await db.get(PartnerOrg, org_id)
        row = await _open_row(db, org_id)
        if org is None or row is None:
            raise NotFound("Save your application first", code="verification_not_found")
        if row.status == "pending":
            raise ConflictError("Your application is already under review", code="verification_pending")
        errors = validate(VerificationIn.model_validate(row.payload), complete=True)
        if errors:
            raise ValidationFailed("Complete the highlighted fields", details={"fields": errors})
        p = row.payload
        row.status, row.submitted_by, row.submitted_at, row.message = "pending", dctx.developer_id, now(), None
        org.verification_status = "pending" if org.verification_status != "verified" else org.verification_status
        org.legal_name, org.country, org.tax_id_kind, org.tax_id = p.get("legal_name"), (p.get("country") or "")[:2].upper() or None, p.get("tax_id_kind"), p.get("tax_id")
        org.website, org.support_email, org.support_phone = p.get("website") or None, p.get("support_email") or None, p.get("support_phone") or None
        org.agreement_version, org.agreement_accepted_at = p.get("agreement_version"), now()
        audit_log.add(db, "org.verification_submitted", partner_org_id=org_id, account_id=dctx.developer_id, target_type="verification", target_id=row.id)
        await db.flush()
        return _out(org, row)


# ================================================================ platform reviewer
async def review_queue(status: str | None = "pending") -> list[VerificationOut]:
    async with platform_session() as db:
        stmt = select(PartnerVerificationRequest, PartnerOrg).join(PartnerOrg, PartnerOrg.id == PartnerVerificationRequest.partner_org_id)
        if status:
            stmt = stmt.where(PartnerVerificationRequest.status == status)
        rows = await db.execute(stmt.order_by(PartnerVerificationRequest.submitted_at.asc().nulls_last()).limit(500))
        return [_out(org, row) for row, org in rows.all()]


async def get_request(request_id: uuid.UUID) -> VerificationOut:
    async with platform_session() as db:
        row = await db.get(PartnerVerificationRequest, request_id)
        org = await db.get(PartnerOrg, row.partner_org_id) if row else None
        if row is None or org is None:
            raise NotFound("Application not found")
        return _out(org, row)


async def decide_verification(
    operator_ctx: RequestContext, request_id: uuid.UUID, decision: Literal["approve", "request_changes", "reject"], message: str | None
) -> VerificationOut:
    """★ approve / reject: a human platform operator with the ``marketplace_reviewer`` role (Epic 69's guard)."""
    from app.services.marketplace_service import review

    operator_id = await review.require_role(operator_ctx)
    if decision in ("request_changes", "reject") and not (message or "").strip():
        raise ValidationFailed("Write a message to the partner", details={"fields": {"message": "Required"}})
    s = get_settings()
    async with platform_session() as db:
        row = (await db.execute(select(PartnerVerificationRequest).where(PartnerVerificationRequest.id == request_id).with_for_update())).scalar_one_or_none()
        if row is None:
            raise NotFound("Application not found")
        if row.status != "pending":
            raise ConflictError("This application is not waiting for a decision", code="verification_not_pending")
        org = await db.get(PartnerOrg, row.partner_org_id)
        assert org is not None
        at = now()
        row.reviewer_id, row.decided_at, row.message = operator_id, at, (message or "").strip() or None
        if decision == "approve":
            row.status = "approved"
            org.verification_status, org.verified_at, org.verified_by = "verified", at, operator_id
            org.verification_expires_at = at + timedelta(days=round(365 * s.partner_verification_validity_months / 12))
        elif decision == "reject":
            row.status = "rejected"
            org.verification_status = "rejected" if org.verification_status != "verified" else org.verification_status
        else:
            row.status = "changes_requested"
            org.verification_status = "unverified" if org.verification_status == "pending" else org.verification_status
        audit_log.add(db, "org.verification_decided", partner_org_id=org.id, operator_id=operator_id, target_type="verification", target_id=row.id, decision=decision)
        audit.record(f"developer.verification_{decision}", "partner_org", org.id, session=db, request_id=str(row.id))
        await db.flush()
        out = _out(org, row)
        org_id, org_name = org.id, org.name
    await notifier.verification_decided(org_id, org_name, decision, message)
    return out


async def expire_verifications() -> int:
    """Daily: reminders at -30 / -7 days; ``expired`` 30 days after ``verification_expires_at``."""
    at = now()
    changed = 0
    async with platform_session() as db:
        orgs = list((await db.execute(select(PartnerOrg).where(PartnerOrg.verification_status == "verified", PartnerOrg.verification_expires_at.is_not(None)))).scalars())
        reminders: list[tuple[uuid.UUID, str, str]] = []
        for org in orgs:
            assert org.verification_expires_at is not None
            left = (org.verification_expires_at - at).days
            if at >= org.verification_expires_at + timedelta(days=30):
                org.verification_status = "expired"
                changed += 1
                audit_log.add(db, "org.verification_decided", partner_org_id=org.id, decision="expired")
            elif left in (30, 7):
                reminders.append((org.id, org.name, org.verification_expires_at.date().isoformat()))
    for org_id, name, date in reminders:
        await notifier.verification_expiring(org_id, name, date)
    return changed


__all__ = [
    "decide_verification",
    "expire_verifications",
    "get_request",
    "get_verification",
    "review_queue",
    "save_verification",
    "submit_verification",
    "validate",
]
