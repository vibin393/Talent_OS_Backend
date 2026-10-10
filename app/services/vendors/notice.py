"""DPDP notice to vendor-sourced candidates (Epic 17 A5, FR-95).

After a submission commits, a Secure Link (purpose ``vendor_data_notice``, ttl ``notice_link_days``,
``max_uses=20``, subject ``vendor_submission``) is issued and emailed to the candidate (tenant, vendor, demand
title, rights, link). The public page shows the active ``data_processing`` notice; ``POST .../consent``:

* before acceptance -- the choice is recorded on the submission; a withdrawal auto-rejects it (system,
  ``consent_withdrawn``);
* after acceptance -- the P1 privacy consent grant / withdrawal on the Candidate (source ``vendor_notice``).

Raw tokens are never stored (only the hash, ``core.secure_links``).
"""

import uuid
from datetime import timedelta

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import mailer, secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.models.vendors import Vendor, VendorRelease, VendorSubmission
from app.services.vendors.common import load_settings

PURPOSE = "vendor_data_notice"
SUBJECT = "vendor_submission"
MAX_USES = 20


async def issue_and_email(ctx: RequestContext, db: AsyncSession, submission_id: uuid.UUID) -> bool:  # noqa: ARG001
    from app.services import identity_bridge

    sub = (
        await db.execute(select(VendorSubmission).where(VendorSubmission.id == submission_id))
    ).scalar_one_or_none()
    if sub is None or not sub.email or sub.erased_at is not None:
        return False
    existing = await secure_links.links_for_subject(db, SUBJECT, sub.id)
    if any(link.purpose == PURPOSE for link in existing):
        return False  # idempotent: one notice per submission
    settings = await load_settings(db)
    _link, token = await secure_links.issue(
        db,
        tenant_id=sub.tenant_id,
        purpose=PURPOSE,
        subject_type=SUBJECT,
        subject_id=sub.id,
        ttl=timedelta(days=settings.notice_link_days),
        max_uses=MAX_USES,
    )
    vendor_name = (await db.execute(select(Vendor.legal_name).where(Vendor.id == sub.vendor_id))).scalar_one()
    title = (
        await db.execute(select(VendorRelease.title).where(VendorRelease.id == sub.vendor_release_id))
    ).scalar_one()
    tenant = await identity_bridge.get_tenant(db, sub.tenant_id)
    tenant_name = tenant.name if tenant is not None else "the hiring organisation"
    url = secure_links.link_url(token)
    text = (
        f"Hello {sub.full_name or ''},\n\n"
        f'{vendor_name} has shared your profile with {tenant_name} for the role "{title}".\n'
        f"{tenant_name} will process your personal data to evaluate you for this and similar roles.\n\n"
        "You have the right to access, correct and erase your data and to withdraw your consent at "
        "any time.\n"
        f"Review the notice and record your choice here: {url}\n"
    )
    await mailer.send_email(
        mailer.EmailMessage(
            to=str(sub.email),
            subject=f"{tenant_name}: how we use your data",
            text=text,
            tenant_id=sub.tenant_id,
            kind="vendor_sourced_notice",
        )
    )
    return True


async def view(link: secure_links.ResolvedLink) -> dict[str, object]:
    from app.services import identity_bridge, privacy

    async with tenant_session(link.tenant_id) as db:
        sub = (
            await db.execute(select(VendorSubmission).where(VendorSubmission.id == link.subject_id))
        ).scalar_one_or_none()
        if sub is None or sub.erased_at is not None:
            raise secure_links.LinkGone("This link is no longer valid", code="link_invalid")
        notice = await privacy.consent.active_notice(db, link.tenant_id, "data_processing")
        tenant = await identity_bridge.get_tenant(db, link.tenant_id)
        title = (
            await db.execute(select(VendorRelease.title).where(VendorRelease.id == sub.vendor_release_id))
        ).scalar_one()
        status = sub.notice_consent or "pending"
        first = (sub.full_name or "").split(" ")[0] or None
        return {
            "tenant_name": tenant.name if tenant is not None else None,
            "notice": notice,
            "job_title": title,
            "status": status,
            "candidate_first_name": first,
        }


async def respond(token: str, granted: bool) -> str:
    from app.services import privacy
    from app.services.vendors import review

    link = await secure_links.resolve(token, (PURPOSE,))
    ctx = RequestContext(tenant_id=link.tenant_id, actor_kind="system")
    async with tenant_session(link.tenant_id) as db:
        await secure_links.consume(db, link.id)
        sub = (
            await db.execute(
                select(VendorSubmission).where(VendorSubmission.id == link.subject_id).with_for_update()
            )
        ).scalar_one_or_none()
        if sub is None:
            raise secure_links.LinkGone("This link is no longer valid", code="link_invalid")
        if granted:
            sub.notice_consent = "granted"
            if sub.candidate_id is not None:
                await privacy.consent.grant_consent(
                    ctx,
                    db,
                    sub.candidate_id,
                    "data_processing",
                    source="vendor_notice",
                    evidence={"link_id": str(link.id)},
                )
            await db.flush()
            return "granted"
        if sub.candidate_id is not None and sub.status == "accepted":
            sub.notice_consent = "withdrawn"
            await privacy.consent.withdraw_consent(
                ctx, db, sub.candidate_id, "data_processing", by_kind="candidate"
            )
        else:
            await review.system_reject_consent(ctx, db, sub.id)
        logger.info("Vendor notice consent withdrawn for submission {}", sub.id)
        return "withdrawn"
