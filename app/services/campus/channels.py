# ruff: file-ignore[line-too-long]
"""SMS / WhatsApp notices through P2 messaging (Epic 43 A5 / A6): campus slot reminders and queue turn notices.

Uses the tenant's registered transactional template with purpose ``interview_reminder`` for the channel. P2
``messaging.sending.send`` applies opt-outs (SMS) and WhatsApp consent; a refusal or any failure is returned as a
short result code and never blocks the caller (the send runs in a SAVEPOINT).
"""

import uuid

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError

PURPOSE = "interview_reminder"


async def send(
    c: RequestContext, db: AsyncSession, channel: str, candidate_id: uuid.UUID, *, demand_id: uuid.UUID | None = None
) -> str:
    """Queue one message; returns ``queued`` or the refusal / failure code (``no_template``, ``opted_out``,
    ``no_whatsapp_consent``, ``failed:<code>`` ...)."""
    from app.models.comms import ChannelTemplate
    from app.schemas.messaging import ContextRefsIn, RecipientRef
    from app.services.messaging import sending

    if channel not in ("sms", "whatsapp"):
        return "none"
    template = (
        await db.execute(
            select(ChannelTemplate)
            .where(
                ChannelTemplate.channel == channel,
                ChannelTemplate.purpose == PURPOSE,
                ChannelTemplate.status == "registered",
                ChannelTemplate.provider_template_ref.is_not(None),
            )
            .order_by(ChannelTemplate.updated_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if template is None:
        return "no_template"
    try:
        async with db.begin_nested():
            out = await sending.send(
                c,
                db,
                channel,
                template.id,
                [RecipientRef(entity_type="candidate", entity_id=candidate_id)],
                ContextRefsIn(demand_id=demand_id) if demand_id else None,
                origin="system",
            )
    except DomainError as exc:
        return f"failed:{exc.code}"[:40]
    except Exception:
        logger.exception("campus: channel notice failed")
        return "failed"
    result = out.results[0] if out.results else None
    if result is None:
        return "failed"
    return "queued" if result.status == "queued" else (result.reason or "refused")[:40]
