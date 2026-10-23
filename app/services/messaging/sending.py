# ruff: file-ignore[line-too-long]
"""SMS / WhatsApp sending (Epic 33, FR-172/173, AD-29).

``send`` (API ``POST /comms/messages/send``, ``messaging:send``; and the workflow actions) checks each
recipient -- phone (``no_phone``), opt-out (``opted_out``), WhatsApp consent (``no_whatsapp_consent``), merge
variables (``missing_variable``) -- inserts a ``channel_messages`` row and enqueues an idempotent connector job
(``<channel>:send:channel_message:<id>:<dispatch_version>``, 3 attempts). The response is per recipient.

The job's adapter calls back through ``MessagingGate`` (``connectors.sms.channel``): ``prepare`` re-checks the
template, opt-out and consent (an opt-out recorded after queueing is honoured) and defers SMS inside quiet hours;
``complete`` records the outcome (``sent`` / ``manual_pending`` / ``failed``) with an Activity on the recipient
(template name and status only -- never the phone number). The Manual connector's queue is completed with
``mark_sent`` / ``mark_failed``.
"""

import dataclasses
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import secure_links
from app.core.context import RequestContext
from app.core.database import tenant_session
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.core.permissions import Principal
from app.events.comms import ChannelMessageQueued, ChannelMessageStatusChanged
from app.models.candidates import CandidatePhone
from app.models.clients import Contact
from app.models.comms import ChannelMessage, ChannelTemplate
from app.models.connectors import ConnectorJob
from app.schemas.messaging import (
    ChannelMessageOut,
    EligibilityOut,
    ManualQueueItemOut,
    MessagingStateOut,
    NoticeBrief,
    RecipientRef,
    SendMessagesOut,
    SendResultOut,
)
from app.services import entity_refs
from app.services.comms import channel_settings
from app.services.connectors import jobs
from app.services.connectors.sms.channel import Outcome, Prepared, SendSpec, mask_phone
from app.services.messaging import opt_outs, templates

MAX_ATTEMPTS = 3
OPT_OUT_PURPOSE = "channel_opt_out"
OPT_OUT_TTL = timedelta(days=180)
OPT_OUT_MAX_USES = 5
WHATSAPP_PURPOSE = "whatsapp"
STATUS_RANK = {
    "queued": 0,
    "deferred_quiet_hours": 0,
    "manual_pending": 1,
    "sent": 2,
    "delivered": 3,
    "read": 4,
}
LABEL = {"sms": "SMS", "whatsapp": "WhatsApp"}


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(db: AsyncSession) -> uuid.UUID:
    return uuid.UUID(str(db.sync_session.info["tenant_id"]))


def _user_id(ctx: RequestContext) -> uuid.UUID | None:
    p = ctx.principal
    return p.id if isinstance(p, Principal) and p.type == "user" else None


# ================================================================ recipients
async def resolve_phone(db: AsyncSession, entity_type: str, entity_id: uuid.UUID) -> str | None:
    if entity_type == "candidate":
        return (
            await db.execute(
                select(CandidatePhone.phone_e164)
                .where(CandidatePhone.candidate_id == entity_id)
                .order_by(CandidatePhone.is_primary.desc(), CandidatePhone.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
    if entity_type == "contact":
        return (await db.execute(select(Contact.phone).where(Contact.id == entity_id))).scalar_one_or_none()
    return None


async def has_whatsapp_consent(
    db: AsyncSession, entity_type: str | None, entity_id: uuid.UUID | None
) -> bool:
    """WhatsApp opt-in is a P1 consent (purpose ``whatsapp``) -- candidates (incl. Workers) only."""
    if entity_type != "candidate" or entity_id is None:
        return False
    from app.services.privacy import consent

    return await consent.has_active_consent(db, entity_id, WHATSAPP_PURPOSE)


@dataclasses.dataclass
class Evaluation:
    ref: RecipientRef
    reason: str | None
    phone: str | None = None
    body: str | None = None
    values: list[str] = dataclasses.field(default_factory=list)


async def _evaluate(
    ctx: RequestContext,
    db: AsyncSession,
    channel: str,
    template: ChannelTemplate | None,
    ref: RecipientRef,
    context_refs: dict[str, Any],
    *,
    check_access: bool,
) -> Evaluation:
    if check_access:
        await entity_refs.require_readable(ctx, db, ref.entity_type, ref.entity_id)
    who = await entity_refs.addressee(db, ref.entity_type, ref.entity_id)
    if who is None:
        raise NotFound("Recipient not found")
    phone = await resolve_phone(db, ref.entity_type, ref.entity_id)
    if not phone:
        return Evaluation(ref, "no_phone")
    if await opt_outs.is_opted_out(db, channel, phone):
        return Evaluation(ref, "opted_out", phone)
    if channel == "whatsapp" and not await has_whatsapp_consent(db, ref.entity_type, ref.entity_id):
        return Evaluation(ref, "no_whatsapp_consent", phone)
    if template is None:
        return Evaluation(ref, None, phone)
    if not templates.sendable(template):
        return Evaluation(ref, "template_not_registered", phone)
    data = await entity_refs.merge_data(
        db, _tenant(db), context_refs, sender_user_id=_user_id(ctx), recipient=who
    )
    body, values, missing = templates.render(template, data)
    if missing:
        return Evaluation(ref, "missing_variable", phone, body, values)
    if channel == "sms" and len(body) > templates.SMS_MAX_CHARS:
        return Evaluation(ref, "body_too_long", phone, body, values)
    return Evaluation(ref, None, phone, body, values)


def _refs(context_refs: Any) -> dict[str, Any]:
    if context_refs is None:
        return {}
    raw = (
        context_refs.model_dump(exclude_none=True)
        if hasattr(context_refs, "model_dump")
        else dict(context_refs)
    )
    return {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in raw.items()}


async def _template_for(db: AsyncSession, channel: str, template_id: uuid.UUID) -> ChannelTemplate:
    template = await db.get(ChannelTemplate, template_id)
    if template is None:
        raise NotFound("Template not found")
    if template.channel != channel:
        raise ValidationFailed("The template belongs to another channel", code="template_channel_mismatch")
    return template


async def eligibility(
    ctx: RequestContext,
    db: AsyncSession,
    channel: str,
    template_id: uuid.UUID | None,
    recipients: Sequence[RecipientRef],
    context_refs: Any = None,
) -> list[EligibilityOut]:
    template = await _template_for(db, channel, template_id) if template_id else None
    out: list[EligibilityOut] = []
    for ref in recipients:
        ev = await _evaluate(ctx, db, channel, template, ref, _refs(context_refs), check_access=True)
        out.append(
            EligibilityOut(
                entity_type=ref.entity_type,
                entity_id=ref.entity_id,
                eligible=ev.reason is None,
                reason=ev.reason,
                preview=ev.body,
            )
        )
    return out


async def _channel_enabled(db: AsyncSession, channel: str) -> None:
    settings = await channel_settings.get_row(db)
    if not (settings.sms_enabled if channel == "sms" else settings.whatsapp_enabled):
        raise ConflictError(
            f"{LABEL[channel]} messaging is turned off for this workspace", code="channel_disabled"
        )


async def _opt_out_link(db: AsyncSession, message: ChannelMessage) -> str:
    _, token = await secure_links.issue(
        db,
        tenant_id=message.tenant_id,
        purpose=OPT_OUT_PURPOSE,
        subject_type="channel_message",
        subject_id=message.id,
        ttl=OPT_OUT_TTL,
        max_uses=OPT_OUT_MAX_USES,
        metadata={"channel": message.channel},
    )
    return secure_links.link_url(token)


async def enqueue_dispatch(ctx: RequestContext, db: AsyncSession, message: ChannelMessage) -> None:
    job = await jobs.enqueue(
        ctx,
        db,
        message.channel,
        "send",
        "channel_message",
        message.id,
        message.dispatch_version,
        {"tenant_id": str(message.tenant_id), "channel_message_id": str(message.id)},
    )
    await db.execute(
        update(ConnectorJob)
        .where(ConnectorJob.id == job.id, ConnectorJob.attempts == 0)
        .values(max_attempts=MAX_ATTEMPTS)
    )
    message.connector_job_id = job.id


# Epic 39 (additive registry; ``messaging`` never imports ``campaigns``): a gate per message category, called by the
# connector-job send gate immediately before the provider call (``gate(db, message) -> obj`` with ``.action`` in
# send | suppress | defer, ``.reason`` and ``.until``). Marketing messages carry ``variables.category``.
_category_gates: dict[str, Any] = {}


def register_category_gate(category: str, gate: Any) -> None:
    _category_gates[category] = gate


def message_category(message: ChannelMessage) -> str:
    return str((message.variables or {}).get("category") or "transactional")


async def send(
    ctx: RequestContext,
    db: AsyncSession,
    channel: str,
    template_id: uuid.UUID,
    recipients: Sequence[RecipientRef],
    context_refs: Any = None,
    *,
    origin: str = "user",
    workflow_execution_id: uuid.UUID | None = None,
    related: Sequence[dict[str, Any]] = (),
    category: str = "transactional",
) -> SendMessagesOut:
    template = await _template_for(db, channel, template_id)
    if not templates.sendable(template):
        raise templates.not_registered()
    if category == "marketing" and template.purpose != "marketing":
        raise ConflictError("Marketing messages need a registered marketing template", code="template_not_marketing")
    await _channel_enabled(db, channel)
    refs = _refs(context_refs)
    results: list[SendResultOut] = []
    sender = _user_id(ctx)
    for ref in recipients:
        ev = await _evaluate(ctx, db, channel, template, ref, refs, check_access=origin == "user")
        if ev.reason is not None:
            results.append(
                SendResultOut(
                    entity_type=ref.entity_type, entity_id=ref.entity_id, status="refused", reason=ev.reason
                )
            )
            continue
        message = ChannelMessage(
            id=uuid.uuid4(),
            tenant_id=_tenant(db),
            channel=channel,
            direction="outbound",
            template_id=template.id,
            template_version=template.version,
            recipient_kind=ref.entity_type,
            recipient_id=ref.entity_id,
            candidate_id=ref.entity_id if ref.entity_type == "candidate" else None,
            to_e164=ev.phone,
            body_rendered=ev.body or "",
            variables={
                "values": ev.values,
                "paths": list(template.variables or []),
                **({"category": category} if category != "transactional" else {}),
            },
            status="queued",
            related=[{"entity_type": ref.entity_type, "entity_id": str(ref.entity_id)}, *related],
            origin=origin,
            workflow_execution_id=workflow_execution_id,
            sent_by=sender,
            dispatch_version=1,
        )
        db.add(message)
        await db.flush()
        message.body_rendered = f"{message.body_rendered}\nStop messages: {await _opt_out_link(db, message)}"
        await enqueue_dispatch(ctx, db, message)
        await db.flush()
        emit(
            db,
            ChannelMessageQueued(
                tenant_id=message.tenant_id, message_id=message.id, channel=channel, origin=origin
            ),
        )
        results.append(
            SendResultOut(
                entity_type=ref.entity_type, entity_id=ref.entity_id, status="queued", message_id=message.id
            )
        )
    return SendMessagesOut(results=results)


# ================================================================ activities / status
async def _activity(db: AsyncSession, message: ChannelMessage, template_name: str | None, text: str) -> None:
    from app.services import activity_service

    if message.recipient_kind not in ("candidate", "contact") or message.recipient_id is None:
        return
    author = (
        Principal(type="user", id=message.sent_by, tenant_id=message.tenant_id)
        if message.sent_by is not None
        else None
    )
    actx = RequestContext(tenant_id=message.tenant_id, principal=author)
    await activity_service.add_system_entry(
        actx,
        db,
        message.recipient_kind,
        message.recipient_id,
        text,
        {
            "channel_message_id": str(message.id),
            "template_name": template_name,
            "status": message.status,
            "direction": message.direction,
            "channel": message.channel,
        },
        kind=message.channel,
    )


async def _template_name(db: AsyncSession, message: ChannelMessage) -> str | None:
    if message.template_id is None:
        return None
    t = await db.get(ChannelTemplate, message.template_id)
    return t.name if t is not None else None


async def set_status(
    db: AsyncSession,
    message: ChannelMessage,
    status: str,
    *,
    reason: str | None = None,
    error_code: str | None = None,
    provider_message_id: str | None = None,
    activity: bool = True,
) -> None:
    message.status = status
    if reason is not None:
        message.refusal_reason = reason
    if error_code is not None:
        message.error_code = error_code[:64]
    if provider_message_id:
        message.provider_message_id = provider_message_id[:255]
    if status in ("sent", "delivered") and message.sent_at is None:
        message.sent_at = _now()
    await db.flush()
    if activity:
        name = await _template_name(db, message)
        label = LABEL.get(message.channel, message.channel)
        detail = {
            "refused": f"refused ({reason})",
            "failed": "failed",
            "manual_pending": "queued for manual send",
        }.get(status, status)
        await _activity(db, message, name, f"{label} {detail}: {name or 'message'}")
    emit(
        db,
        ChannelMessageStatusChanged(
            tenant_id=message.tenant_id,
            message_id=message.id,
            channel=message.channel,
            status=status,
            error_code=error_code,
        ),
    )


# ================================================================ send gate (job callbacks)
class MessagingGate:
    """Installed into ``connectors.sms.channel`` by ``messaging.hooks``."""

    async def prepare(self, tenant_id: uuid.UUID, message_id: uuid.UUID) -> Prepared:
        async with tenant_session(tenant_id) as db:
            message = (
                await db.execute(
                    select(ChannelMessage).where(ChannelMessage.id == message_id).with_for_update()
                )
            ).scalar_one_or_none()
            if message is None or message.status != "queued" or message.direction != "outbound":
                return Prepared(None, f"status_{message.status if message else 'missing'}")
            template = await db.get(ChannelTemplate, message.template_id) if message.template_id else None
            gate = _category_gates.get(message_category(message))
            if gate is not None:  # Epic 39: marketing governance immediately before the provider call
                decision = await gate(db, message)
                action = getattr(decision, "action", "send")
                if action == "suppress":
                    reason_code = f"marketing_{getattr(decision, 'reason', None) or 'suppressed'}"[:40]
                    await set_status(db, message, "refused", reason=reason_code)
                    return Prepared(None, reason_code)
                if action == "defer":
                    until = getattr(decision, "until", None) or _now() + timedelta(minutes=15)
                    message.status, message.deferred_until = "deferred_quiet_hours", until
                    await db.flush()
                    return Prepared(None, "deferred_quiet_hours")
            reason: str | None = None
            if template is None or not templates.sendable(template):
                reason = "template_not_registered"
            elif await opt_outs.is_opted_out(db, message.channel, message.to_e164):
                reason = "opted_out"
            elif message.channel == "whatsapp" and not await has_whatsapp_consent(
                db, message.recipient_kind, message.recipient_id
            ):
                reason = "no_whatsapp_consent"
            if reason is not None:
                await set_status(db, message, "refused", reason=reason)
                return Prepared(None, reason)
            settings = await channel_settings.get_row(db)
            if message.channel == "sms":
                until = channel_settings.quiet_until(settings.quiet_hours, _now())
                if until is not None:
                    message.status, message.deferred_until = "deferred_quiet_hours", until
                    await db.flush()
                    return Prepared(None, "deferred_quiet_hours")
            assert template is not None
            return Prepared(
                SendSpec(
                    channel=message.channel,
                    to_e164=message.to_e164 or "",
                    body=message.body_rendered,
                    template_ref=template.provider_template_ref or "",
                    variables=list((message.variables or {}).get("values") or []),
                    language=template.language,
                    sender_id=template.sender_id or settings.default_sms_sender_id,
                    dlt_entity_id=settings.dlt_entity_id,
                )
            )

    async def complete(self, tenant_id: uuid.UUID, message_id: uuid.UUID, outcome: Outcome) -> None:
        async with tenant_session(tenant_id) as db:
            message = (
                await db.execute(
                    select(ChannelMessage).where(ChannelMessage.id == message_id).with_for_update()
                )
            ).scalar_one_or_none()
            if message is None or message.status not in ("queued",):
                return
            await set_status(
                db,
                message,
                outcome.status,
                error_code=outcome.error_code,
                provider_message_id=outcome.provider_message_id,
            )


async def job_failed(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> bool:  # ruff: ignore[unused-function-argument]
    """``ConnectorJobFailed`` (all attempts used): the queued message becomes ``failed``."""
    message = (
        await db.execute(select(ChannelMessage).where(ChannelMessage.id == message_id).with_for_update())
    ).scalar_one_or_none()
    if message is None or message.status != "queued":
        return False
    await set_status(db, message, "failed", error_code="provider_unavailable")
    return True


async def release_deferred(ctx: RequestContext, db: AsyncSession, now: datetime | None = None) -> int:
    """Re-dispatch SMS deferred by quiet hours once the window has ended (new job version)."""
    now = now or _now()
    rows = list(
        (
            await db.execute(
                select(ChannelMessage)
                .where(ChannelMessage.status == "deferred_quiet_hours", ChannelMessage.deferred_until <= now)
                .with_for_update(skip_locked=True)
                .limit(500)
            )
        ).scalars()
    )
    for message in rows:
        message.status = "queued"
        message.dispatch_version += 1
        message.deferred_until = None
        await enqueue_dispatch(ctx, db, message)
    await db.flush()
    return len(rows)


# ================================================================ manual queue
def _age(message: ChannelMessage) -> int:
    return max(int((_now() - (message.created_at or _now())).total_seconds()), 0)


async def manual_queue(ctx: RequestContext, db: AsyncSession) -> list[ManualQueueItemOut]:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(
                select(ChannelMessage)
                .where(ChannelMessage.status == "manual_pending")
                .order_by(ChannelMessage.created_at)
                .limit(500)
            )
        ).scalars()
    )
    names = {t.id: t for t in (await db.execute(select(ChannelTemplate))).scalars()}
    return [
        ManualQueueItemOut(
            id=m.id,
            channel=m.channel,  # type: ignore[arg-type]
            to_e164=m.to_e164,
            body_rendered=m.body_rendered,
            template_name=names[m.template_id].name if m.template_id in names else None,
            provider_template_ref=names[m.template_id].provider_template_ref
            if m.template_id in names
            else None,
            variables=dict(m.variables or {}),
            created_at=m.created_at,
            age_seconds=_age(m),
        )
        for m in rows
    ]


async def _manual(db: AsyncSession, message_id: uuid.UUID) -> ChannelMessage:
    message = (
        await db.execute(select(ChannelMessage).where(ChannelMessage.id == message_id).with_for_update())
    ).scalar_one_or_none()
    if message is None:
        raise NotFound("Message not found")
    if message.status != "manual_pending":
        raise ConflictError("This message is not waiting for a manual send", code="message_not_manual")
    return message


async def mark_sent(ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID) -> ChannelMessageOut:
    message = await _manual(db, message_id)
    if await opt_outs.is_opted_out(db, message.channel, message.to_e164):
        await set_status(db, message, "refused", reason="opted_out")
    else:
        message.sent_by = message.sent_by or _user_id(ctx)
        await set_status(db, message, "sent")
    return (await _messages_out(db, [message]))[0]


async def mark_failed(
    ctx: RequestContext, db: AsyncSession, message_id: uuid.UUID, error_code: str | None
) -> ChannelMessageOut:
    message = await _manual(db, message_id)
    await set_status(db, message, "failed", error_code=error_code or "manual_failed")
    return (await _messages_out(db, [message]))[0]


# ================================================================ reads
async def _messages_out(db: AsyncSession, rows: Sequence[ChannelMessage]) -> list[ChannelMessageOut]:
    ids = {m.template_id for m in rows if m.template_id}
    names = (
        {
            t.id: t.name
            for t in (await db.execute(select(ChannelTemplate).where(ChannelTemplate.id.in_(ids)))).scalars()
        }
        if ids
        else {}
    )
    return [
        ChannelMessageOut(
            id=m.id,
            channel=m.channel,  # type: ignore[arg-type]
            direction=m.direction,  # type: ignore[arg-type]
            template_id=m.template_id,
            template_name=names.get(m.template_id) if m.template_id else None,
            status=m.status,
            refusal_reason=m.refusal_reason,
            error_code=m.error_code,
            recipient_kind=m.recipient_kind,
            recipient_id=m.recipient_id,
            to_masked=mask_phone(m.to_e164),
            body_rendered=m.body_rendered,
            origin=m.origin,
            sent_at=m.sent_at,
            created_at=m.created_at,
            age_seconds=_age(m),
        )
        for m in rows
    ]


async def list_messages(
    ctx: RequestContext,
    db: AsyncSession,
    entity_type: str | None = None,
    entity_id: uuid.UUID | None = None,
    channel: str | None = None,
    status: str | None = None,
) -> list[ChannelMessageOut]:
    stmt = select(ChannelMessage)
    if entity_type and entity_id:
        await entity_refs.require_readable(ctx, db, entity_type, entity_id)
        stmt = stmt.where(
            ChannelMessage.recipient_kind == entity_type, ChannelMessage.recipient_id == entity_id
        )
    if channel:
        stmt = stmt.where(ChannelMessage.channel == channel)
    if status:
        stmt = stmt.where(ChannelMessage.status == status)
    rows = list((await db.execute(stmt.order_by(ChannelMessage.created_at.desc()).limit(200))).scalars())
    return await _messages_out(db, rows)


async def unmatched_inbound(ctx: RequestContext, db: AsyncSession) -> list[ChannelMessageOut]:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(
                select(ChannelMessage)
                .where(ChannelMessage.direction == "inbound", ChannelMessage.recipient_id.is_(None))
                .order_by(ChannelMessage.created_at.desc())
                .limit(200)
            )
        ).scalars()
    )
    return await _messages_out(db, rows)


async def whatsapp_notice(db: AsyncSession) -> NoticeBrief | None:
    from app.services.messaging.notices import ensure_whatsapp_notice

    notice = await ensure_whatsapp_notice(db)
    return (
        NoticeBrief(id=notice.id, version=notice.version, title=notice.title, body_md=notice.body_md)
        if notice
        else None
    )


async def messaging_state(
    ctx: RequestContext, db: AsyncSession, entity_type: str, entity_id: uuid.UUID
) -> MessagingStateOut:
    if entity_type not in ("candidate", "contact"):
        raise NotFound("Resource not found")
    await entity_refs.require_readable(ctx, db, entity_type, entity_id)
    phone = await resolve_phone(db, entity_type, entity_id)
    return MessagingStateOut(
        has_phone=bool(phone),
        phone_masked=mask_phone(phone),
        sms_opted_out=await opt_outs.is_opted_out(db, "sms", phone),
        whatsapp_consent=await has_whatsapp_consent(db, entity_type, entity_id),
        whatsapp_notice=await whatsapp_notice(db) if entity_type == "candidate" else None,
    )


async def attest_whatsapp(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, notice_id: uuid.UUID
) -> MessagingStateOut:
    """A recruiter records the candidate's WhatsApp opt-in (``recruiter_attestation``) against the shown notice."""
    from app.services.messaging.notices import ensure_whatsapp_notice
    from app.services.privacy import consent

    await entity_refs.require_readable(ctx, db, "candidate", candidate_id)
    notice = await ensure_whatsapp_notice(db)
    if notice is None or notice.id != notice_id:
        raise ConflictError("The WhatsApp notice changed; review it again", code="notice_outdated")
    if not await has_whatsapp_consent(db, "candidate", candidate_id):
        await consent.grant_consent(
            ctx,
            db,
            candidate_id,
            WHATSAPP_PURPOSE,
            source="recruiter_attestation",
            notice_id=notice.id,
            evidence={"attested_by": str(_user_id(ctx)) if _user_id(ctx) else None},
        )
    return await messaging_state(ctx, db, "candidate", candidate_id)


async def find_latest_outbound(
    db: AsyncSession, channel: str, phone: str, since: datetime
) -> ChannelMessage | None:
    return (
        await db.execute(
            select(ChannelMessage)
            .where(
                ChannelMessage.channel == channel,
                ChannelMessage.direction == "outbound",
                ChannelMessage.to_e164 == phone,
                ChannelMessage.created_at >= since,
                or_(ChannelMessage.status != "refused", ChannelMessage.status.is_(None)),
            )
            .order_by(ChannelMessage.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def log_safe(message_id: uuid.UUID, status: str) -> None:
    logger.info("Channel message {} -> {}", message_id, status)


# Epic 33: the job adapters call back through this gate (also installed by ``messaging.hooks``).
from app.services.connectors.sms.channel import register_send_gate as _register_gate

_register_gate(MessagingGate())
