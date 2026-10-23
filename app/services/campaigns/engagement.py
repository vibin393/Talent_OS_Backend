# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Engagement capture (Epic 39 A6, FR-198, story 39.4).

Sources: P1 comms events (``EmailSent`` / ``EmailFailed`` / ``EmailBounced`` / ``EmailReplyReceived``), P2 messaging
events (``ChannelMessageStatusChanged`` / ``ChannelOptOutRecorded`` / ``ChannelInboundReceived``), the signed public
links (open pixel, click, unsubscribe) and pipeline / placement events (applied, hired -- see ``attribution``). Every
event row is deduplicated (``dedupe_key``); the seven contract kinds emit ``CampaignMessageEngaged``.
"""

import uuid
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.events import emit
from app.events.campaigns import CampaignMessageEngaged
from app.models.campaigns import CampaignMessage, EngagementEvent, Enrollment
from app.models.campaigns import Sequence as SequenceModel
from app.services.campaigns.common import now, tenant_of

CONTRACT_KINDS = ("delivered", "opened", "clicked", "replied", "unsubscribed", "bounced", "complained")
REPLY_WINDOW = timedelta(days=30)


async def record(
    db: AsyncSession,
    cm: CampaignMessage,
    kind: str,
    *,
    dedupe: str | None = None,
    meta: dict[str, Any] | None = None,
    at: datetime | None = None,
) -> bool:
    """Insert one engagement event (idempotent by ``dedupe_key``); True when it is new."""
    when = at or now()
    new_id = (
        await db.execute(
            pg_insert(EngagementEvent)
            .values(
                id=uuid.uuid4(),
                tenant_id=cm.tenant_id,
                campaign_message_id=cm.id,
                candidate_id=cm.candidate_id,
                kind=kind,
                occurred_at=when,
                meta=meta or {},
                dedupe_key=(dedupe or f"{kind}:{cm.id}")[:200],
            )
            .on_conflict_do_nothing(index_elements=["tenant_id", "dedupe_key"])
            .returning(EngagementEvent.id)
        )
    ).scalar_one_or_none()
    if new_id is None:
        return False
    if kind in CONTRACT_KINDS:
        emit(
            db,
            CampaignMessageEngaged(
                tenant_id=cm.tenant_id,
                campaign_message_id=cm.id,
                campaign_id=cm.campaign_id,
                enrollment_id=cm.enrollment_id,
                candidate_id=cm.candidate_id,
                kind=kind,
                occurred_at=when,
            ),
        )
    return True


async def _by_email(db: AsyncSession, email_message_id: uuid.UUID) -> CampaignMessage | None:
    return (
        await db.execute(
            select(CampaignMessage)
            .where(CampaignMessage.email_message_id == email_message_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def _by_channel(db: AsyncSession, channel_message_id: uuid.UUID) -> CampaignMessage | None:
    return (
        await db.execute(
            select(CampaignMessage)
            .where(CampaignMessage.channel_message_id == channel_message_id)
            .with_for_update()
        )
    ).scalar_one_or_none()


# ================================================================ comms (email)
async def on_email_sent(db: AsyncSession, email_message_id: uuid.UUID) -> bool:
    cm = await _by_email(db, email_message_id)
    if cm is None:
        return False
    if cm.status == "queued":
        cm.status = "sent"
    await record(db, cm, "delivered")
    return True


async def on_email_failed(db: AsyncSession, email_message_id: uuid.UUID) -> bool:
    cm = await _by_email(db, email_message_id)
    if cm is None or cm.status != "queued":
        return False
    cm.status, cm.suppression_reason = "failed", "delivery_failed"
    return True


async def on_email_bounced(db: AsyncSession, email_message_id: uuid.UUID, bounce_type: str) -> bool:
    from app.services import comms_service
    from app.services.campaigns import governance

    cm = await _by_email(db, email_message_id)
    if cm is None:
        return False
    if bounce_type == "complaint":
        await record(db, cm, "complained")
        address = await governance.email_for(db, cm.candidate_id)
        if address:
            await comms_service.set_marketing_opt_out(db, str(address))
        await suppress_open(db, cm.candidate_id, None, "complaint")
        return True
    cm.status = "bounced"
    await record(db, cm, "bounced", meta={"bounce_type": bounce_type})
    return True


# ================================================================ messaging (sms / whatsapp)
async def on_channel_status(db: AsyncSession, channel_message_id: uuid.UUID, status: str) -> bool:
    cm = await _by_channel(db, channel_message_id)
    if cm is None:
        return False
    if status in ("sent", "manual_pending") and cm.status == "queued":
        cm.status = "sent"
    elif status in ("delivered", "read"):
        cm.status = "delivered"
        await record(db, cm, "delivered")
    elif status == "failed" and cm.status in ("queued", "sent"):
        cm.status = "failed"
    elif status == "refused" and cm.status == "queued":
        cm.status, cm.suppression_reason = "suppressed", cm.suppression_reason or "refused"
    return True


async def suppress_open(db: AsyncSession, candidate_id: uuid.UUID, channel: str | None, reason: str) -> int:
    """Every not-yet-delivered marketing message of the candidate (one channel or all) -> ``suppressed(reason)``."""
    stmt = (
        update(CampaignMessage)
        .where(
            CampaignMessage.candidate_id == candidate_id, CampaignMessage.status.in_(("pending", "deferred"))
        )
        .values(status="suppressed", suppression_reason=reason, defer_until=None, updated_at=now())
    )
    if channel is not None:
        stmt = stmt.where(CampaignMessage.channel == channel)
    result = await db.execute(stmt)
    return int(getattr(result, "rowcount", 0) or 0)


async def on_opt_out_recorded(db: AsyncSession, candidate_id: uuid.UUID | None, channel: str) -> int:
    if candidate_id is None:
        return 0
    return await suppress_open(db, candidate_id, channel, "channel_opt_out")


# ================================================================ replies / exits
async def exit_enrollments(
    db: AsyncSession, candidate_id: uuid.UUID, reason: str, *, only: uuid.UUID | None = None
) -> list[Enrollment]:
    """Active / paused enrolments of the candidate whose sequence exits on ``reason`` -> ``exited(reason)``."""
    stmt = (
        select(Enrollment, SequenceModel)
        .join(SequenceModel, SequenceModel.id == Enrollment.sequence_id)
        .where(Enrollment.candidate_id == candidate_id, Enrollment.status.in_(("active", "paused")))
        .with_for_update(of=Enrollment)
    )
    if only is not None:
        stmt = stmt.where(Enrollment.id == only)
    ended: list[Enrollment] = []
    for enrollment, sequence in (await db.execute(stmt)).tuples().all():
        if reason != "manual" and not bool((sequence.exit_on or {}).get(reason, True)):
            continue
        enrollment.status, enrollment.exit_reason = "exited", reason
        enrollment.ended_at, enrollment.next_run_at = now(), None
        ended.append(enrollment)
    if ended:
        await db.execute(
            update(CampaignMessage)
            .where(
                CampaignMessage.enrollment_id.in_([e.id for e in ended]),
                CampaignMessage.status.in_(("pending", "deferred")),
            )
            .values(status="cancelled", defer_until=None, updated_at=now())
        )
    await db.flush()
    return ended


async def on_reply(db: AsyncSession, candidate_id: uuid.UUID | None, channel: str | None, ref: str) -> bool:
    """A reply from the candidate within 30 days of their latest marketing message -> ``replied`` + sequence exit."""
    if candidate_id is None:
        return False
    stmt = (
        select(CampaignMessage)
        .where(
            CampaignMessage.candidate_id == candidate_id,
            CampaignMessage.status.in_(("queued", "sent", "delivered")),
            CampaignMessage.sent_at >= now() - REPLY_WINDOW,
        )
        .order_by(CampaignMessage.sent_at.desc())
        .limit(1)
    )
    if channel is not None:
        stmt = stmt.where(CampaignMessage.channel == channel)
    cm = (await db.execute(stmt)).scalar_one_or_none()
    if cm is None:
        return False
    await record(db, cm, "replied", dedupe=f"replied:{cm.id}:{ref}"[:200])
    if cm.enrollment_id is not None:
        ended = await exit_enrollments(db, candidate_id, "replied")
        if ended:
            from app.services import notifications_service

            enrollment = ended[0]
            sequence = await db.get(SequenceModel, enrollment.sequence_id)
            if sequence is not None:
                await notifications_service.fan_out(
                    db,
                    "sequence_reply",
                    {
                        "sequence_id": str(sequence.id),
                        "owner_id": str(sequence.owner_id),
                        "candidate_id": str(candidate_id),
                        "channel": cm.channel,
                    },
                )
    return True


# ================================================================ public links
async def message_for_token(db: AsyncSession, message_id: uuid.UUID) -> CampaignMessage | None:
    return await db.get(CampaignMessage, message_id)


async def opened(db: AsyncSession, cm: CampaignMessage) -> bool:
    if not cm.pixel:
        return False
    return await record(db, cm, "opened")


async def clicked(db: AsyncSession, cm: CampaignMessage, index: int) -> str | None:
    """The stored job-link URL (+ ``tos_ct``) for ``index`` (None = 404: never an open redirect)."""
    from app.models.campaigns import Campaign, SequenceStep
    from app.services.campaigns import tokens

    links: list[dict[str, Any]] = []
    if cm.campaign_id is not None:
        campaign = await db.get(Campaign, cm.campaign_id)
        links = list(campaign.job_links or []) if campaign else []
    elif cm.step_id is not None:
        step = await db.get(SequenceStep, cm.step_id)
        links = list((step.config or {}).get("job_links") or []) if step else []
    link = next((x for x in links if int(x.get("index", -1)) == index), None)
    if link is None or not str(link.get("url") or "").startswith(("https://", "http://")):
        return None
    await record(db, cm, "clicked", dedupe=f"clicked:{cm.id}:{index}", meta={"index": index})
    token = tokens.make(cm.tenant_id, cm.id, cm.token_nonce)
    sep = "&" if "?" in str(link["url"]) else "?"
    return f"{link['url']}{sep}tos_ct={token}"


async def unsubscribe_state(db: AsyncSession, cm: CampaignMessage) -> bool:
    from app.models.comms import EmailAddressStatus
    from app.services.campaigns import governance
    from app.services.messaging import opt_outs

    if cm.channel == "email":
        address = await governance.email_for(db, cm.candidate_id)
        if not address:
            return False
        status = await db.get(EmailAddressStatus, (tenant_of(db), str(address).lower()))
        return status is not None and status.marketing_opt_out_at is not None
    phone = await governance.phone_for(db, cm.candidate_id)
    return bool(phone) and await opt_outs.is_opted_out(db, cm.channel, phone)


async def unsubscribe(db: AsyncSession, cm: CampaignMessage, *, all_marketing: bool) -> None:
    """Synchronous opt-out (within the request): channel opt-out (+ ``marketing`` consent withdrawal for ``all``),
    pending / deferred messages suppressed, active enrolments exited, ``unsubscribed`` recorded."""
    from app.services import comms_service
    from app.services.campaigns import governance
    from app.services.campaigns.common import system_ctx
    from app.services.messaging import opt_outs
    from app.services.privacy import consent

    if cm.channel == "email":
        address = await governance.email_for(db, cm.candidate_id)
        if address:
            await comms_service.set_marketing_opt_out(db, str(address))
    else:
        phone = await governance.phone_for(db, cm.candidate_id)
        if phone:
            await opt_outs.record(
                db,
                cm.channel,
                phone,
                "recipient_link",
                recipient_kind="candidate",
                recipient_id=cm.candidate_id,
            )
    if all_marketing:
        await consent.withdraw_consent(
            system_ctx(cm.tenant_id), db, cm.candidate_id, "marketing", by_kind="candidate"
        )
        await suppress_open(db, cm.candidate_id, None, "no_marketing_consent")
    else:
        await suppress_open(db, cm.candidate_id, cm.channel, "channel_opt_out")
    await record(db, cm, "unsubscribed", meta={"all": all_marketing})
    await exit_enrollments(db, cm.candidate_id, "unsubscribed")
