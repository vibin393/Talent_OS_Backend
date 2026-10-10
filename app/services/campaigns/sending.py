# ruff: noqa: E501, ARG001
"""Marketing delivery (Epic 39 A4/A5/A7): claim -> throttle -> governance -> render -> hand to P1 comms / P2 messaging.

``send_batch(db, message_ids)`` claims each ``campaign_messages`` row with ``SELECT ... FOR UPDATE SKIP LOCKED`` where
``status IN ('pending','deferred')`` (so a retried task never hands a message to delivery twice -- the unique keys plus
this status guard), takes a token (``throttle``), runs ``governance.check`` and then:

* ``Send``: renders the per-recipient body and queues it -- email via ``comms_service.queue_email(origin='campaign',
  category='marketing', raw=True)`` (sequence steps use ``origin='user'`` when the owner has a connected mailbox,
  coordinator decision 14), SMS / WhatsApp via ``messaging.sending.send(origin='campaign', category='marketing')``
  with the registered marketing template; status ``queued``, ``sent_at`` = hand-off time (counts toward the cap).
* ``Defer``: ``deferred`` + ``defer_until`` (None = held until the campaign / tenant resumes).
* ``Suppress``: ``suppressed`` + reason (a sequence enrolment ends ``suppressed`` on consent / opt-out reasons).

Each message runs in a SAVEPOINT; the delivery rows commit atomically with the status change. Bodies, addresses and
tokens are never logged.
"""

import dataclasses
import html
import re
import uuid
from datetime import datetime, timedelta
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.errors import DomainError
from app.models.campaigns import Campaign, CampaignMessage, Enrollment, SequenceStep
from app.models.campaigns import Sequence as SequenceModel
from app.models.candidates import Candidate
from app.services.campaigns import governance, throttle, tokens
from app.services.campaigns import settings as mkt_settings
from app.services.campaigns.common import now as _now
from app.services.campaigns.common import system_ctx, tenant_of

ENDING_REASONS = ("no_marketing_consent", "channel_opt_out", "no_whatsapp_optin", "erased")
MESSAGING_REASON_MAP = {
    "opted_out": "channel_opt_out",
    "no_phone": "no_address",
    "no_whatsapp_consent": "no_whatsapp_optin",
}


@dataclasses.dataclass
class BatchResult:
    sent: int = 0
    deferred: int = 0
    suppressed: int = 0
    skipped: int = 0
    failed: int = 0
    throttled: list[uuid.UUID] = dataclasses.field(default_factory=list)
    retry_after_ms: int = 0


@dataclasses.dataclass(frozen=True, slots=True)
class Content:
    channel: str
    subject: str | None
    body_html: str | None
    channel_template_id: uuid.UUID | None
    job_links: list[dict[str, Any]]
    owner_id: uuid.UUID


# ================================================================ content
def content_for_campaign(campaign: Campaign, variant_key: str | None) -> Content:
    subject = campaign.email_subject
    if variant_key and campaign.ab_test:
        for v in campaign.ab_test.get("variants") or []:
            if v.get("key") == variant_key:
                subject = v.get("subject") or subject
    return Content(
        campaign.channel,
        subject,
        campaign.email_body_html,
        campaign.channel_template_id,
        list(campaign.job_links or []),
        campaign.owner_id,
    )


def content_for_step(step: SequenceStep, owner_id: uuid.UUID) -> Content:
    cfg = step.config or {}
    tpl = cfg.get("channel_template_id")
    return Content(
        str(cfg.get("channel") or "email"),
        cfg.get("subject"),
        cfg.get("body_html"),
        uuid.UUID(str(tpl)) if tpl else None,
        list(cfg.get("job_links") or []),
        owner_id,
    )


def job_links_html(token: str, links: list[dict[str, Any]]) -> str:
    if not links:
        return ""
    items = "".join(
        f'<li><a href="{html.escape(tokens.api_url(token, f"c/{int(link.get('index', i))}"))}">'
        f"{html.escape(str(link.get('label') or 'View job'))}</a></li>"
        for i, link in enumerate(links)
    )
    return f"<p><strong>Open roles</strong></p><ul>{items}</ul>"


JOB_LINK_TOKEN = re.compile(r"\{\{\s*job_link\.(\d+)\s*\}\}")


def replace_job_link_tokens(body: str, token: str, links: list[dict[str, Any]]) -> tuple[str, bool]:
    """``{{job_link.N}}`` (1-based, as the editor inserts it) -> the signed click URL of job link N-1."""
    found = False

    def _sub(match: re.Match[str]) -> str:
        nonlocal found
        n = int(match.group(1)) - 1
        if 0 <= n < len(links):
            found = True
            return html.escape(tokens.api_url(token, f"c/{int(links[n].get('index', n))}"))
        return "#"

    return JOB_LINK_TOKEN.sub(_sub, body), found


def footer_html(token: str, contact: str | None) -> str:
    unsubscribe = html.escape(tokens.page_url(token, "u"))
    contact_line = f"<p>{html.escape(contact)}</p>" if contact else ""
    return f'<hr>{contact_line}<p><a href="{unsubscribe}">Unsubscribe</a> from these emails.</p>'


async def _render_email(
    db: AsyncSession,
    cm: CampaignMessage,
    content: Content,
    address: str,
    full_name: str | None,
    footer: str | None,
) -> tuple[str, str]:
    from app.services import entity_refs
    from app.services.comms import merge_fields
    from app.services.privacy import consent

    tenant_id = tenant_of(db)
    data = await entity_refs.merge_data(
        db,
        tenant_id,
        {"candidate_id": str(cm.candidate_id)},
        recipient=entity_refs.Addressee("candidate", cm.candidate_id, address, full_name),
    )
    token = tokens.make(tenant_id, cm.id, cm.token_nonce)
    raw_body, inline = replace_job_link_tokens(content.body_html or "", token, content.job_links)
    subject, _ = merge_fields.render(content.subject or "", data, escape=False)
    body, _ = merge_fields.render(raw_body, data, escape=True)
    links_block = "" if inline else job_links_html(token, content.job_links)
    body = f"{body}{links_block}{footer_html(token, footer)}"
    if await consent.has_active_consent(db, cm.candidate_id, "tracking"):
        body += f'<img src="{html.escape(tokens.api_url(token, "o.gif"))}" width="1" height="1" alt="">'
        cm.pixel = True
    return subject.strip() or "(no subject)", body


async def _sequence_origin(db: AsyncSession, owner_id: uuid.UUID) -> str:
    """Decision 14: ``user`` when the owner has a connected mailbox (reply capture), else ``campaign``."""
    from app.services import comms_service

    provider = comms_service.mailbox_provider()
    if provider is None:
        return "campaign"
    try:
        conn = await provider.get_send_connection(system_ctx(tenant_of(db)), db, owner_id)
    except Exception:  # a broken mailbox never blocks marketing: platform transport
        return "campaign"
    return "user" if conn is not None else "campaign"


async def deliver(
    db: AsyncSession, cm: CampaignMessage, content: Content, address: str, *, sequence: bool
) -> str | None:
    """Hand one governed message to its channel. Returns None on success, else a suppression reason."""
    from app.services import comms_service

    ctx = system_ctx(tenant_of(db))
    if content.channel == "email":
        row = await mkt_settings.get_row(db)
        full_name = (
            await db.execute(select(Candidate.full_name).where(Candidate.id == cm.candidate_id))
        ).scalar_one_or_none()
        subject, body = await _render_email(db, cm, content, address, full_name, row.footer_contact)
        origin = await _sequence_origin(db, content.owner_id) if sequence else "campaign"
        spec = comms_service.RecipientSpec("to", address, "candidate", cm.candidate_id, full_name)
        try:
            result = await comms_service.queue_email(
                ctx,
                db,
                origin=origin,
                to=[spec],
                subject=subject,
                body_html=body,
                sender_user_id=content.owner_id if origin == "user" else None,
                reply_to_user_id=content.owner_id,
                category="marketing",
                related=[("candidate", cm.candidate_id)],
                raw=True,
            )
        except DomainError as exc:
            if origin != "user":
                raise
            logger.info("Sequence mailbox send refused ({}); platform transport", exc.code)
            result = await comms_service.queue_email(
                ctx,
                db,
                origin="campaign",
                to=[spec],
                subject=subject,
                body_html=body,
                reply_to_user_id=content.owner_id,
                category="marketing",
                related=[("candidate", cm.candidate_id)],
                raw=True,
            )
        cm.email_message_id = result.message_ids[0] if result.message_ids else None
        return None
    from app.schemas.messaging import RecipientRef
    from app.services.messaging import sending as messaging

    if content.channel_template_id is None:
        return "template_not_registered"
    out = await messaging.send(
        ctx,
        db,
        content.channel,
        content.channel_template_id,
        [RecipientRef(entity_type="candidate", entity_id=cm.candidate_id)],
        {"candidate_id": str(cm.candidate_id)},
        origin="campaign",
        related=[{"entity_type": "campaign_message", "entity_id": str(cm.id)}],
        category="marketing",
    )
    first = out.results[0]
    if first.status != "queued":
        return MESSAGING_REASON_MAP.get(first.reason or "", first.reason or "refused")
    cm.channel_message_id = first.message_id
    return None


# ================================================================ batch
async def _owner_content(db: AsyncSession, cm: CampaignMessage) -> tuple[Content | None, Enrollment | None]:
    if cm.campaign_id is not None:
        campaign = await db.get(Campaign, cm.campaign_id)
        return (content_for_campaign(campaign, cm.variant_key) if campaign else None), None
    enrollment = await db.get(Enrollment, cm.enrollment_id)
    step = await db.get(SequenceStep, cm.step_id)
    sequence = await db.get(SequenceModel, enrollment.sequence_id) if enrollment else None
    if step is None or sequence is None:
        return None, enrollment
    return content_for_step(step, sequence.owner_id), enrollment


def next_local_midnight(now: datetime, tz: Any) -> datetime:
    local = now.astimezone(tz)
    return datetime.combine(local.date() + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(
        now.tzinfo
    )


async def _end_enrollment(db: AsyncSession, enrollment: Enrollment | None, reason: str) -> None:
    if enrollment is not None and reason in ENDING_REASONS and enrollment.status in ("active", "paused"):
        enrollment.status, enrollment.ended_at, enrollment.next_run_at = "suppressed", _now(), None


async def process_one(db: AsyncSession, message_id: uuid.UUID, result: BatchResult, now: datetime) -> None:
    cm = (
        await db.execute(
            select(CampaignMessage)
            .where(CampaignMessage.id == message_id, CampaignMessage.status.in_(("pending", "deferred")))
            .with_for_update(skip_locked=True)
        )
    ).scalar_one_or_none()
    if cm is None:
        result.skipped += 1
        return
    if cm.status == "deferred" and cm.defer_until is not None and cm.defer_until > now:
        result.skipped += 1
        return
    row = await mkt_settings.get_row(db)
    taken = await throttle.take(
        row.tenant_id,
        cm.channel,
        rate=mkt_settings.effective_rate(row, cm.channel),
        daily_cap=mkt_settings.effective_daily_cap(row, cm.channel),
    )
    if not taken.allowed:
        if taken.daily_exhausted:
            cm.status = "deferred"
            cm.defer_until = next_local_midnight(now, mkt_settings.zone(row.default_timezone))
            result.deferred += 1
        else:
            result.throttled.append(cm.id)
            result.retry_after_ms = max(result.retry_after_ms, taken.retry_after_ms)
        return
    address = await governance.address_for(db, cm.candidate_id, cm.channel)
    decision = await governance.check(
        db, candidate_id=cm.candidate_id, channel=cm.channel, address=address, now=now, message=cm
    )
    content, enrollment = await _owner_content(db, cm)
    if isinstance(decision, governance.Suppress):
        cm.status, cm.suppression_reason, cm.defer_until = "suppressed", decision.reason, None
        await _end_enrollment(db, enrollment, decision.reason)
        result.suppressed += 1
        return
    if isinstance(decision, governance.Defer):
        cm.status, cm.defer_until = "deferred", decision.until
        result.deferred += 1
        return
    if content is None:
        cm.status, cm.suppression_reason = "suppressed", "content_missing"
        result.suppressed += 1
        return
    assert address is not None
    try:
        async with db.begin_nested():
            refused = await deliver(db, cm, content, address, sequence=cm.enrollment_id is not None)
    except DomainError as exc:
        cm.status, cm.suppression_reason = "failed", exc.code[:40]
        result.failed += 1
        return
    if refused is not None:
        cm.status, cm.suppression_reason = "suppressed", refused[:40]
        await _end_enrollment(db, enrollment, refused)
        result.suppressed += 1
        return
    cm.status, cm.sent_at, cm.defer_until, cm.suppression_reason = "queued", now, None, None
    result.sent += 1


async def send_batch(
    db: AsyncSession, message_ids: list[uuid.UUID], now: datetime | None = None
) -> BatchResult:
    result = BatchResult()
    for mid in message_ids:
        when = now or _now()
        try:
            async with db.begin_nested():
                await process_one(db, mid, result, when)
        except Exception:
            logger.exception("Marketing message {} could not be processed", mid)
            result.failed += 1
    await db.flush()
    return result


# ================================================================ comms / messaging gates (registered from hooks)
@dataclasses.dataclass(frozen=True, slots=True)
class GateDecision:
    action: str
    reason: str | None = None
    until: datetime | None = None


async def email_gate(db: AsyncSession, message: Any) -> GateDecision:
    """P1 ``comms_service.deliver`` gate for ``category='marketing'`` emails."""
    cm = (
        await db.execute(
            select(CampaignMessage).where(CampaignMessage.email_message_id == message.id).with_for_update()
        )
    ).scalar_one_or_none()
    return await _gate(db, cm, "email")


async def channel_gate(db: AsyncSession, message: Any) -> GateDecision:
    """P2 messaging send gate for ``category='marketing'`` SMS / WhatsApp."""
    cm = (
        await db.execute(
            select(CampaignMessage).where(CampaignMessage.channel_message_id == message.id).with_for_update()
        )
    ).scalar_one_or_none()
    return await _gate(db, cm, message.channel)


async def _gate(db: AsyncSession, cm: CampaignMessage | None, channel: str) -> GateDecision:
    if cm is None:  # a marketing message with no campaign row is never ours to send
        return GateDecision("suppress", "unknown_marketing_message")
    if cm.status not in ("queued",):
        return GateDecision("suppress", cm.suppression_reason or "superseded")
    address = await governance.address_for(db, cm.candidate_id, channel)
    decision = await governance.check(
        db,
        candidate_id=cm.candidate_id,
        channel=channel,
        address=address,
        now=_now(),
        message=cm,
        respect_window=False,
    )
    if isinstance(decision, governance.Send):
        return GateDecision("send")
    if isinstance(decision, governance.Suppress):
        cm.status, cm.suppression_reason = "suppressed", decision.reason
        _, enrollment = await _owner_content(db, cm)
        await _end_enrollment(db, enrollment, decision.reason)
        await db.flush()
        return GateDecision("suppress", decision.reason)
    # Deferred after hand-off (tenant / campaign paused): the delivery row is dropped and the message re-queued.
    cm.status, cm.defer_until, cm.email_message_id, cm.channel_message_id = (
        "deferred",
        decision.until,
        None,
        None,
    )
    await db.flush()
    return GateDecision("defer", "deferred", decision.until)


async def header_provider(db: AsyncSession, message: Any) -> dict[str, str]:
    """``List-Unsubscribe`` (RFC 2369 / RFC 8058 one-click) and the idempotency key of a marketing email."""
    cm = (
        await db.execute(select(CampaignMessage).where(CampaignMessage.email_message_id == message.id))
    ).scalar_one_or_none()
    if cm is None:
        return {}
    token = tokens.make(cm.tenant_id, cm.id, cm.token_nonce)
    return {
        "List-Unsubscribe": f"<{tokens.api_url(token, 'u')}>",
        "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        "X-TOS-Idempotency-Key": f"campaign_message:{cm.id}",
    }
