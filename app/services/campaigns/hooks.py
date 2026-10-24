# ruff: file-ignore[line-too-long, unused-function-argument]
"""Registrations of the ``campaigns`` domain (autodiscovered by ``core.hooks``), Epic 39.

* Category gates ``marketing`` into P1 ``comms_service.deliver`` and the P2 messaging send gate, and the
  ``List-Unsubscribe`` header provider (comms / messaging never import campaigns).
* Careers apply ``attribution_token`` resolver (``portal.apply.register_attribution_resolver``).
* Approval subject ``campaign`` (``campaign:approve``); workflow action ``campaigns.enrol_in_sequence`` and the
  triggers ``campaigns.candidate_dormant`` / ``silver_medalist_matched`` / ``pool_members_added``; rediscovery
  section ``sequences``.
* Notification types ``campaign_approval_requested``, ``campaign_completed``, ``sequence_reply`` (foundation keys) and
  the domain-local ``campaign_paused`` / ``marketing_tenant_paused``; the ``tracking`` consent notice.
* After-commit handlers (comms, messaging, pipeline, placements, matching, own events) -> ``app.tasks.campaigns``.
* Retention bindings ``campaign_messages`` / ``engagement_events`` -> class ``marketing_logs`` (24 months, seeded by
  Epic 52); audit actions; settings hub entry.
"""

import uuid
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext

TRACKING_TITLE = "Email engagement tracking"
TRACKING_BODY = "We measure whether you open our emails using a small image. You can withdraw at any time."


def _uuid(v: Any) -> uuid.UUID | None:
    try:
        return v if isinstance(v, uuid.UUID) else uuid.UUID(str(v))
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------- gates
def _register_gates() -> None:
    from app.services import comms_service
    from app.services.campaigns import sending
    from app.services.messaging import sending as messaging_sending

    comms_service.register_category_gate("marketing", sending.email_gate)
    comms_service.register_header_provider("marketing", sending.header_provider)
    messaging_sending.register_category_gate("marketing", sending.channel_gate)


def _register_apply() -> None:
    from app.services.campaigns import attribution
    from app.services.portal import apply

    apply.register_attribution_resolver(attribution.resolve_attribution_token)


# ---------------------------------------------------------------- approvals / workflow / rediscovery
def _register_approval() -> None:
    from app.services.approvals.registry import register_approval_subject
    from app.services.campaigns import campaigns

    register_approval_subject(
        campaigns.APPROVAL_SUBJECT,
        on_final_decided=campaigns.on_final_decided,
        describe=campaigns.describe,
        permission="campaign:approve",
    )


def _register_workflow() -> None:
    from app.services.campaigns import triggers

    triggers.register_action()
    triggers.register_triggers()


async def _sequences_section(
    ctx: RequestContext, db: AsyncSession, demand_id: uuid.UUID
) -> dict[str, Any] | None:
    from sqlalchemy import select

    from app.models.campaigns import Sequence

    principal = ctx.principal
    if principal is None or not principal.has("sequence:write"):
        return None
    rows = (
        await db.execute(
            select(Sequence.id, Sequence.name)
            .where(Sequence.status == "active")
            .order_by(Sequence.name)
            .limit(50)
        )
    ).all()
    return {"action": "enrol_in_sequence", "sequences": [{"id": str(i), "name": n} for i, n in rows]}


def _register_rediscovery() -> None:
    from app.services.matching.rediscovery import register_rediscovery_section

    register_rediscovery_section("sequences", _sequences_section)


# ---------------------------------------------------------------- notifications
async def _owner(db: AsyncSession, payload: Any) -> set[uuid.UUID]:  # NOSONAR
    uid = _uuid(payload.get("owner_id"))
    return {uid} if uid else set()


async def _approvers(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.models.approvals import ApprovalRequest
    from app.services import directory_service

    request = await db.get(ApprovalRequest, _uuid(payload.get("request_id")))
    if request is None:
        return set()
    out: set[uuid.UUID] = set()
    if request.current_approver_user_id:
        out.add(request.current_approver_user_id)
    if request.current_approver_role_id:
        out |= await directory_service.user_ids_with_role(db, request.current_approver_role_id)
    return out


async def _tenant_admins(db: AsyncSession, payload: Any) -> set[uuid.UUID]:
    from app.services import entity_refs

    return set(await entity_refs.role_user_ids(db, "tenant_admin"))


async def _campaign_name(db: AsyncSession, payload: Any) -> tuple[uuid.UUID | None, str]:
    from app.models.campaigns import Campaign

    cid = _uuid(payload.get("campaign_id"))
    c = await db.get(Campaign, cid) if cid else None
    return cid, (c.name if c is not None else "Campaign")


async def _render_approval(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    cid, name = await _campaign_name(db, payload)
    return Content(
        title=f"Approve campaign {name}"[:300],
        body="A campaign above the approval threshold is waiting for you.",
        entity_type=None,
        entity_id=None,
        link_path=f"/marketing/campaigns/{cid}",
    )


async def _render_completed(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    cid, name = await _campaign_name(db, payload)
    return Content(
        title=f"Campaign {name} finished sending"[:300],
        body="Open the campaign to see its results.",
        link_path=f"/marketing/campaigns/{cid}",
    )


async def _render_paused(db: AsyncSession, payload: Any) -> Any:
    from app.services.notifications_service import Content

    cid, name = await _campaign_name(db, payload)
    return Content(
        title=f"Campaign {name} was paused"[:300],
        body=f"Reason: {payload.get('reason') or 'paused'}",
        link_path=f"/marketing/campaigns/{cid}",
    )


async def _render_tenant_paused(db: AsyncSession, payload: Any) -> Any:  # NOSONAR
    from app.services.notifications_service import Content

    reason = {"complaint_rate": "the spam complaint rate", "bounce_rate": "the hard bounce rate"}.get(
        str(payload.get("reason")), "a manual pause"
    )
    return Content(
        title="Marketing sends are paused for this workspace",
        body=f"Sending stopped because of {reason}. Review your lists, then resume in Marketing settings.",
        link_path="/marketing/settings",
    )


async def _render_reply(db: AsyncSession, payload: Any) -> Any:
    from sqlalchemy import select

    from app.models.candidates import Candidate
    from app.services.notifications_service import Content

    cid = _uuid(payload.get("candidate_id"))
    name = (
        (await db.execute(select(Candidate.full_name).where(Candidate.id == cid))).scalar_one_or_none()
        if cid
        else None
    )
    channel = {"email": "email", "sms": "SMS", "whatsapp": "WhatsApp"}.get(
        str(payload.get("channel")), "a message"
    )
    return Content(
        title=f"{name or 'A candidate'} replied to your sequence by {channel}"[:300],
        body=None,
        entity_type="candidate" if cid else None,
        entity_id=cid,
        link_path=f"/candidates/{cid}" if cid else "/marketing/sequences",
    )


def _register_notifications() -> None:
    from app.services import notifications_service as ns

    defs = {
        "campaign_approval_requested": ns.NotificationType(
            "Campaign waiting for approval",
            "Marketing",
            "both",
            False,
            _approvers,
            _render_approval,
            internal_only=True,
        ),
        "campaign_completed": ns.NotificationType(
            "Campaign finished", "Marketing", "in_app", False, _owner, _render_completed, internal_only=True
        ),
        "sequence_reply": ns.NotificationType(
            "Reply to a nurture sequence",
            "Marketing",
            "both",
            False,
            _owner,
            _render_reply,
            internal_only=True,
        ),
        "campaign_paused": ns.NotificationType(
            "Campaign paused", "Marketing", "both", False, _owner, _render_paused, internal_only=True
        ),
        "marketing_tenant_paused": ns.NotificationType(
            "Marketing paused for the workspace",
            "Marketing",
            "both",
            True,
            _tenant_admins,
            _render_tenant_paused,
            internal_only=True,
        ),
    }
    for key, definition in defs.items():
        if key not in ns.NOTIFICATION_TYPES:
            ns.register_notification_type(key, definition)


def _register_notice() -> None:
    from app.services.privacy.consent import register_default_notice

    register_default_notice("tracking", TRACKING_TITLE, TRACKING_BODY)


# ---------------------------------------------------------------- events
def _handle(kind: str, tenant_id: uuid.UUID, **payload: Any) -> None:
    from app.tasks import campaigns as tasks
    from app.tasks.runtime import enqueue

    enqueue(
        tasks.handle_event,
        str(tenant_id),
        kind,
        {k: (str(v) if isinstance(v, uuid.UUID) else v) for k, v in payload.items()},
    )


def _subscribe() -> None:
    from app.core.events import subscribe
    from app.events import subscribe_optional
    from app.services.campaigns.events import CampaignStartRequested, MarketingBatchReady

    def on_start(e: CampaignStartRequested) -> None:
        from app.tasks import campaigns as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.start_campaign, str(e.tenant_id), str(e.campaign_id))

    def on_batch(e: MarketingBatchReady) -> None:
        from app.tasks import campaigns as tasks
        from app.tasks.runtime import enqueue

        enqueue(tasks.send_batch, str(e.tenant_id), [str(i) for i in e.message_ids])

    on_start.__name__ = "campaigns_on_start_requested"
    on_batch.__name__ = "campaigns_on_batch_ready"
    subscribe(CampaignStartRequested, on_start)
    subscribe(MarketingBatchReady, on_batch)

    def email_sent(e: Any) -> None:
        if getattr(e, "origin", None) in ("campaign", "user"):
            _handle("email_sent", e.tenant_id, message_id=e.message_id)

    def email_failed(e: Any) -> None:
        _handle("email_failed", e.tenant_id, message_id=e.message_id)

    def email_bounced(e: Any) -> None:
        if e.message_id is not None:
            _handle("email_bounced", e.tenant_id, message_id=e.message_id, bounce_type=e.bounce_type)

    def email_reply(e: Any) -> None:
        _handle("reply", e.tenant_id, candidate_id=e.candidate_id, channel="email", ref=e.inbound_email_id)

    def channel_status(e: Any) -> None:
        _handle("channel_status", e.tenant_id, message_id=e.message_id, status=e.status)

    def channel_opt_out(e: Any) -> None:
        if e.candidate_id is not None:
            _handle("opt_out", e.tenant_id, candidate_id=e.candidate_id, channel=e.channel)

    def channel_inbound(e: Any) -> None:
        if e.matched and e.candidate_id is not None:
            _handle("reply", e.tenant_id, candidate_id=e.candidate_id, channel=e.channel, ref=e.message_id)

    def application_created(e: Any) -> None:
        _handle("application_created", e.tenant_id, application_id=e.application_id)

    def stage_changed(e: Any) -> None:
        if e.to_key == "hired":
            _handle("hired", e.tenant_id, candidate_id=e.candidate_id, application_id=e.application_id)

    def placement_created(e: Any) -> None:
        _handle("hired", e.tenant_id, candidate_id=e.candidate_id)

    def rediscovery_completed(e: Any) -> None:
        _handle("rediscovery", e.tenant_id, demand_id=e.demand_id)

    for module, cls, fn in (
        ("app.events.comms", "EmailSent", email_sent),
        ("app.events.comms", "EmailFailed", email_failed),
        ("app.events.comms", "EmailBounced", email_bounced),
        ("app.events.comms", "EmailReplyReceived", email_reply),
        ("app.events.comms", "ChannelMessageStatusChanged", channel_status),
        ("app.events.comms", "ChannelOptOutRecorded", channel_opt_out),
        ("app.events.comms", "ChannelInboundReceived", channel_inbound),
        ("app.events.pipeline", "ApplicationCreated", application_created),
        ("app.events.pipeline", "ApplicationStageChanged", stage_changed),
        ("app.events.placements", "PlacementCreated", placement_created),
        ("app.events.matching", "RediscoveryCompleted", rediscovery_completed),
    ):
        fn.__name__ = f"campaigns_on_{cls}"
        subscribe_optional(module, cls, fn)


# ---------------------------------------------------------------- retention / audit / settings
def _register_retention() -> None:
    from sqlalchemy import select

    from app.models.campaigns import CampaignMessage, EngagementEvent
    from app.services.privacy.retention_classes import register_retention_binding, retention_bindings

    async def _subject_message(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
        return (
            await db.execute(select(CampaignMessage.candidate_id).where(CampaignMessage.id == row_id))
        ).scalar_one_or_none()

    async def _anchor_message(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(CampaignMessage.created_at).where(CampaignMessage.id == row_id))
        ).scalar_one_or_none()

    async def _subject_event(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> uuid.UUID | None:
        return (
            await db.execute(select(EngagementEvent.candidate_id).where(EngagementEvent.id == row_id))
        ).scalar_one_or_none()

    async def _anchor_event(db: AsyncSession, row_id: uuid.UUID, *_: Any, **__: Any) -> Any:
        return (
            await db.execute(select(EngagementEvent.occurred_at).where(EngagementEvent.id == row_id))
        ).scalar_one_or_none()

    from app.services.campaigns import retention

    bindings = retention_bindings()
    if "campaign_messages" not in bindings:
        register_retention_binding(
            "campaign_messages",
            "marketing_logs",
            subject_resolver=_subject_message,
            anchor_resolver=_anchor_message,
            expire_fn=retention.expire_messages,
        )
    if "engagement_events" not in bindings:
        register_retention_binding(
            "engagement_events",
            "marketing_logs",
            subject_resolver=_subject_event,
            anchor_resolver=_anchor_event,
            expire_fn=retention.expire_events,
        )


def _register_audit() -> None:
    from app.core import audit

    for key, text in (
        ("campaign.transition", "A campaign changed status"),
        ("campaign.ab_decided", "An A/B test winner was chosen"),
        ("sequence.published", "A nurture sequence version was published"),
        ("marketing.paused", "Marketing sends were paused for the workspace"),
        ("marketing.resumed", "Marketing sends were resumed for the workspace"),
    ):
        audit.register_audit_action(key, text)


def _register_settings() -> None:
    from app.services.settings_service import register_settings_entry

    register_settings_entry(
        "recruiting",
        "marketing",
        "Recruitment marketing",
        "Frequency cap, send windows, approval threshold, send rates and the email footer contact line.",
        "/api/v1/marketing/settings",
        "marketing:configure",
        owner_epic="Epic 39",
    )


for _fn in (
    _register_gates,
    _register_apply,
    _register_approval,
    _register_workflow,
    _register_rediscovery,
    _register_notifications,
    _register_notice,
    _subscribe,
    _register_retention,
    _register_audit,
    _register_settings,
):
    try:
        _fn()
    except Exception:  # pragma: no cover - never block start-up on an optional registry
        logger.exception("campaigns: {} failed", _fn.__name__)
