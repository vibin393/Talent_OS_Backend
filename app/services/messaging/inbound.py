# ruff: file-ignore[line-too-long]
"""Inbound SMS / WhatsApp webhooks (Epic 33, FR-172/173), run by ``connectors.process_inbound`` in the tenant of
the verified config (``/api/v1/hooks/{sms|whatsapp}/<config_id>``) -- a payload can only touch that tenant's rows.

* Delivery receipts move the message forward (``sent`` < ``delivered`` < ``read``; ``failed``).
* Inbound text is matched to the latest outbound message to that number on the channel within 24 h: matched
  -> stored ``direction=inbound`` with an Activity on the same recipient; unmatched -> stored with
  ``related=[]`` (admin "Unmatched inbound" list), no Activity.
* ``STOP`` / ``UNSUBSCRIBE`` (and similar) record an opt-out; on WhatsApp they also withdraw the ``whatsapp``
  consent of the matched candidate.
"""

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.events.comms import ChannelInboundReceived
from app.models.comms import ChannelMessage
from app.services.connectors.base import ConnectorConfig
from app.services.connectors.sms.port import ChannelEvent, events_from_payload
from app.services.messaging import opt_outs, sending

MATCH_WINDOW = timedelta(hours=24)


async def _delivery(db: AsyncSession, channel: str, event: ChannelEvent) -> None:
    if not event.provider_message_id or not event.status:
        return
    message = (
        await db.execute(
            select(ChannelMessage)
            .where(
                ChannelMessage.channel == channel,
                ChannelMessage.direction == "outbound",
                ChannelMessage.provider_message_id == event.provider_message_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if message is None:
        return
    if event.status == "failed":
        if message.status in ("sent", "queued", "delivered"):
            await sending.set_status(db, message, "failed", error_code=event.error_code or "provider_failed")
        return
    current = sending.STATUS_RANK.get(message.status, -1)
    if sending.STATUS_RANK.get(event.status, -1) > current >= sending.STATUS_RANK["sent"]:
        await sending.set_status(db, message, event.status, activity=False)


async def _inbound(ctx: RequestContext, db: AsyncSession, channel: str, event: ChannelEvent) -> None:
    phone = event.from_e164
    if not phone:
        return
    received = event.received_at or datetime.now(UTC)
    if event.provider_message_id:
        dup = (
            await db.execute(
                select(ChannelMessage.id).where(
                    ChannelMessage.direction == "inbound",
                    ChannelMessage.provider_message_id == event.provider_message_id,
                )
            )
        ).first()
        if dup is not None:
            return
    origin = await sending.find_latest_outbound(db, channel, phone, received - MATCH_WINDOW)
    tenant_id = uuid.UUID(str(db.sync_session.info["tenant_id"]))
    message = ChannelMessage(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        channel=channel,
        direction="inbound",
        recipient_kind=origin.recipient_kind if origin else None,
        recipient_id=origin.recipient_id if origin else None,
        candidate_id=origin.candidate_id if origin else None,
        to_e164=phone,
        body_rendered=(event.text or "")[:2000],
        status="received",
        provider_message_id=event.provider_message_id,
        related=list(origin.related or []) if origin else [],
        origin="system",
        sent_at=received,
        in_reply_to_id=origin.id if origin else None,
    )
    db.add(message)
    await db.flush()
    if origin is not None:
        await sending._activity(
            db, message, await sending._template_name(db, origin), f"{sending.LABEL[channel]} reply received"
        )
    if opt_outs.is_stop(event.text):
        await opt_outs.record(
            db,
            channel,
            phone,
            "stop_keyword",
            recipient_kind=origin.recipient_kind if origin else None,
            recipient_id=origin.recipient_id if origin else None,
        )
        if (
            channel == "whatsapp"
            and origin is not None
            and origin.recipient_kind == "candidate"
            and origin.recipient_id
        ):
            from app.services.privacy import consent

            sctx = RequestContext(tenant_id=tenant_id, actor_kind="system")
            await consent.withdraw_consent(
                sctx, db, origin.recipient_id, sending.WHATSAPP_PURPOSE, by_kind="candidate"
            )
    emit(
        db,
        ChannelInboundReceived(
            tenant_id=tenant_id,
            message_id=message.id,
            channel=channel,
            matched=origin is not None,
            candidate_id=origin.candidate_id if origin else None,
            contact_id=origin.recipient_id if origin and origin.recipient_kind == "contact" else None,
        ),
    )


async def handle_webhook(
    ctx: RequestContext, db: AsyncSession, payload: Mapping[str, Any], config: ConnectorConfig
) -> None:
    channel = config.category
    for event in events_from_payload(payload):
        if event.kind == "delivery":
            await _delivery(db, channel, event)
        elif event.kind == "inbound":
            await _inbound(ctx, db, channel, event)
    await db.flush()
