# ruff: file-ignore[line-too-long]
"""The ``whatsapp`` consent notice (Epic 33; purpose widened by the foundation, R12). Seeded per tenant by
migration ``20261003_220_02`` and lazily here for tenants provisioned later (Epic 35 seeds the other purposes)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.privacy import ConsentNotice

TITLE = "WhatsApp messages"
BODY = (
    "We would like to send you transactional WhatsApp messages about your applications, interviews, joining, "
    "onboarding and timesheets (no marketing). You can stop them at any time by replying STOP, from the link in "
    "any message, or from the Privacy page of the candidate portal."
)


async def ensure_whatsapp_notice(db: AsyncSession) -> ConsentNotice | None:
    stmt = select(ConsentNotice).where(
        ConsentNotice.purpose == "whatsapp", ConsentNotice.locale == "en", ConsentNotice.active.is_(True)
    )
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is not None:
        return row
    exists = (
        await db.execute(
            select(ConsentNotice.id).where(ConsentNotice.purpose == "whatsapp", ConsentNotice.locale == "en")
        )
    ).first()
    if exists is not None:
        return None  # an admin deactivated every version: no notice, no opt-in
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    row = ConsentNotice(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        purpose="whatsapp",
        locale="en",
        version=1,
        title=TITLE,
        body_md=BODY,
        active=True,
    )
    db.add(row)
    await db.flush()
    return row
