# ruff: file-ignore[line-too-long, non-imperative-mood, unused-function-argument]
"""One-off Campaigns (Epic 39 A5, FR-197, story 39.3; PRD 5.2 state machine).

``draft -> pending_approval`` (recipients after the suppression preview >= ``approval_threshold``) or ``draft ->
scheduled`` (below, needs ``campaign:send``); ``pending_approval -> scheduled`` (approval engine, subject
``campaign``) / ``-> draft`` (returned); ``scheduled -> sending`` (``scheduled_at`` reached); ``scheduled|sending ->
paused -> sending``; ``sending -> completed`` (every message terminal); any non-terminal -> ``cancelled``. Every
other transition is 409 ``invalid_transition``. Scheduling freezes the Audience snapshot; consent is re-checked at
every send by ``governance``.

Dispatch inserts one ``campaign_messages`` row per snapshot member (``ON CONFLICT DO NOTHING`` on
``(campaign_id, candidate_id)``) and hands batches of <= 500 ids to ``campaigns.send_batch``. A/B (email only): the
deterministic ``sample_pct`` sample is split A/B first; ``decide_ab`` picks the winner after the window (ties -> A)
and dispatches the remainder with it.
"""

import hashlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import Forbidden, ValidationFailed
from app.core.events import emit
from app.models.campaigns import Audience, AudienceMember, Campaign, CampaignMessage, EngagementEvent
from app.models.candidates import Candidate
from app.models.comms import ChannelTemplate
from app.schemas.campaigns import (
    AbTestOut,
    CampaignCreate,
    CampaignMessageOut,
    CampaignMessagesPage,
    CampaignOut,
    CampaignSubmitIn,
    CampaignUpdate,
    JobLinkIn,
    JobLinkOut,
    TestSendOut,
)
from app.services.campaigns import audiences, tokens
from app.services.campaigns import settings as mkt_settings
from app.services.campaigns.common import (
    APPROVE,
    SEND,
    conflict,
    not_found,
    now,
    principal_of,
    system_ctx,
    tenant_of,
    user_id_of,
)
from app.services.campaigns.events import CampaignStartRequested, MarketingBatchReady

APPROVAL_SUBJECT = "campaign"
DEFAULT_APPROVER_ROLE = "recruitment_manager"
BATCH = 500
TERMINAL = ("completed", "cancelled")
OPEN_MESSAGE_STATUSES = ("pending", "deferred", "queued")
TRANSITIONS: dict[str, tuple[str, ...]] = {
    "draft": ("pending_approval", "scheduled", "cancelled"),
    "pending_approval": ("scheduled", "draft", "cancelled"),
    "scheduled": ("sending", "paused", "cancelled"),
    "sending": ("paused", "completed", "cancelled"),
    "paused": ("sending", "scheduled", "cancelled"),
    "completed": (),
    "cancelled": (),
}


# Epic 78 (Ask First, additive): guards run first in ``submit`` (P3 never imports P5; owners register here).
SubmitGuard = Callable[[RequestContext, AsyncSession, Campaign], Awaitable[None]]
_SUBMIT_GUARDS: list[SubmitGuard] = []


def register_submit_guard(fn: SubmitGuard) -> None:
    """Register a guard called first in ``submit`` (raises to refuse); idempotent per function."""
    if fn not in _SUBMIT_GUARDS:
        _SUBMIT_GUARDS.append(fn)


# ================================================================ helpers
async def _get(db: AsyncSession, campaign_id: uuid.UUID, *, lock: bool = False) -> Campaign:
    stmt = select(Campaign).where(Campaign.id == campaign_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found("Campaign")
    return row


async def to_out(db: AsyncSession, c: Campaign) -> CampaignOut:
    await db.flush()
    await db.refresh(c)
    name = (await db.execute(select(Audience.name).where(Audience.id == c.audience_id))).scalar_one_or_none()
    ab = None
    if c.ab_test:
        ab = AbTestOut(
            variants=c.ab_test.get("variants") or [],
            sample_pct=int(c.ab_test.get("sample_pct") or 0),
            metric=str(c.ab_test.get("metric") or "open"),
            window_hours=int(c.ab_test.get("window_hours") or 4),
            winner_key=c.ab_test.get("winner_key"),
            decided_at=datetime.fromisoformat(c.ab_test["decided_at"])
            if c.ab_test.get("decided_at")
            else None,
        )
    return CampaignOut(
        id=c.id,
        name=c.name,
        channel=c.channel,
        audience_id=c.audience_id,
        audience_name=name,
        snapshot_id=c.snapshot_id,
        owner_id=c.owner_id,
        status=c.status,
        scheduled_at=c.scheduled_at,
        started_at=c.started_at,
        completed_at=c.completed_at,
        cancelled_at=c.cancelled_at,
        cancel_reason=c.cancel_reason,
        approval_request_id=c.approval_request_id,
        email_subject=c.email_subject,
        email_body_html=c.email_body_html,
        email_template_id=c.email_template_id,
        channel_template_id=c.channel_template_id,
        job_links=[JobLinkOut(**link) for link in (c.job_links or [])],
        ab_test=ab,
        counts=dict(c.counts or {}),
        version=c.version,
        created_at=c.created_at,
        updated_at=c.updated_at,
    )


async def transition(
    ctx: RequestContext, db: AsyncSession, c: Campaign, to: str, actor: str | None = None
) -> None:
    """The only way a campaign changes status (AD-4); audited."""
    if to not in TRANSITIONS.get(c.status, ()):
        raise conflict(
            "invalid_transition", f"A {c.status} campaign cannot become {to}", status=c.status, to=to
        )
    before = c.status
    c.status = to
    c.updated_at = now()
    if to == "sending" and c.started_at is None:
        c.started_at = now()
    if to == "completed":
        c.completed_at = now()
    if to == "cancelled":
        c.cancelled_at = now()
    await db.flush()
    audit.record(
        "campaign.transition",
        "campaign",
        c.id,
        session=db,
        **{"from": before, "to": to, "by": actor or str(user_id_of(ctx) or "system")},
    )


async def _job_links(db: AsyncSession, links: list[JobLinkIn]) -> list[dict[str, Any]]:
    from app.models.demands import DemandPosting

    out: list[dict[str, Any]] = []
    if not links:
        return out
    slug = await _tenant_slug(tenant_of(db))
    base = tokens.base_url()
    for i, link in enumerate(links):
        posting = await db.get(DemandPosting, link.posting_id)
        if posting is None:
            raise ValidationFailed(
                "Unknown job posting",
                code="unknown_posting",
                details={"fields": {f"job_links[{i}]": "Unknown posting"}},
            )
        from app.models.demands import Demand

        demand = await db.get(Demand, posting.demand_id)
        label = link.label or (demand.title if demand is not None else "View job")
        url = (
            f"{base}/careers/{slug}/jobs/{posting.id}/{posting.slug or 'job'}"  # canonical slug keeps ?tos_ct
        )
        out.append(
            {
                "index": i,
                "posting_id": str(posting.id),
                "demand_id": str(posting.demand_id),
                "label": label,
                "url": url,
            }
        )
    return out


async def _tenant_slug(tenant_id: uuid.UUID) -> str:
    from app.core.database import platform_session
    from app.models.identity import Tenant

    async with platform_session() as pdb:
        tenant = await pdb.get(Tenant, tenant_id)
    return str(tenant.slug) if tenant is not None else ""


def _ab_json(data: Any, row: Any) -> dict[str, Any] | None:
    if data is None:
        return None
    return {
        "variants": [v.model_dump() for v in data.variants],
        "sample_pct": data.sample_pct,
        "metric": data.metric,
        "window_hours": data.window_hours or row.ab_window_hours,
        "winner_key": None,
        "decided_at": None,
    }


def _validate_channel_fields(channel: str, ab: Any) -> None:
    if ab is not None and channel != "email":
        raise ValidationFailed(
            "A/B tests are available for email only",
            code="ab_email_only",
            details={"fields": {"ab_test": "Email only"}},
        )
    if ab is not None and {v.key for v in ab.variants} != {"A", "B"}:
        raise ValidationFailed(
            "A/B tests need variants A and B",
            code="ab_variants",
            details={"fields": {"ab_test": "Variants A and B"}},
        )


# ================================================================ CRUD
async def list_campaigns(
    ctx: RequestContext, db: AsyncSession, status: str | None = None
) -> list[CampaignOut]:
    stmt = select(Campaign).order_by(Campaign.created_at.desc()).limit(500)
    if status:
        stmt = stmt.where(Campaign.status == status)
    return [await to_out(db, c) for c in (await db.execute(stmt)).scalars()]


async def create_campaign(ctx: RequestContext, db: AsyncSession, data: CampaignCreate) -> CampaignOut:
    principal = principal_of(ctx)
    await audiences.get_audience(ctx, db, data.audience_id)
    _validate_channel_fields(data.channel, data.ab_test)
    row = await mkt_settings.get_row(db)
    c = Campaign(
        id=uuid.uuid4(),
        tenant_id=tenant_of(db),
        name=data.name,
        channel=data.channel,
        audience_id=data.audience_id,
        owner_id=principal.id,
        status="draft",
        email_subject=data.email_subject,
        email_body_html=data.email_body_html,
        email_template_id=data.email_template_id,
        channel_template_id=data.channel_template_id,
        job_links=await _job_links(db, data.job_links),
        ab_test=_ab_json(data.ab_test, row),
        counts={},
    )
    db.add(c)
    await db.flush()
    await db.refresh(c)
    return await to_out(db, c)


async def get_campaign(ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID) -> CampaignOut:
    return await to_out(db, await _get(db, campaign_id))


async def update_campaign(
    ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID, data: CampaignUpdate
) -> CampaignOut:
    c = await _get(db, campaign_id, lock=True)
    if c.status != "draft":
        raise conflict("campaign_not_draft", "Only a draft campaign can be edited", status=c.status)
    _validate_channel_fields(c.channel, data.ab_test)
    fields = data.model_dump(exclude_unset=True)
    if data.audience_id is not None:
        await audiences.get_audience(ctx, db, data.audience_id)
        c.audience_id = data.audience_id
    for key in ("name", "email_subject", "email_body_html", "email_template_id", "channel_template_id"):
        if key in fields:
            setattr(c, key, fields[key])
    if data.job_links is not None:
        c.job_links = await _job_links(db, data.job_links)
    if data.clear_ab_test:
        c.ab_test = None
    elif data.ab_test is not None:
        c.ab_test = _ab_json(data.ab_test, await mkt_settings.get_row(db))
    c.version += 1
    c.updated_at = now()
    await db.flush()
    await db.refresh(c)
    return await to_out(db, c)


# ================================================================ submit / approval
async def _validate_content(db: AsyncSession, c: Campaign) -> None:
    from app.services.messaging import templates

    if c.channel == "email":
        missing = [f for f in ("email_subject", "email_body_html") if not (getattr(c, f) or "").strip()]
        if missing:
            raise ValidationFailed(
                "Email campaigns need a subject and a body",
                code="content_required",
                details={"fields": dict.fromkeys(missing, "Required")},
            )
        row = await mkt_settings.get_row(db)
        if not (row.footer_contact or "").strip():
            raise ValidationFailed(
                "Set the postal / contact line for marketing emails in Marketing settings first",
                code="footer_contact_required",
                details={"fields": {"footer_contact": "Required"}},
            )
        return
    template = await db.get(ChannelTemplate, c.channel_template_id) if c.channel_template_id else None
    if (
        template is None
        or template.channel != c.channel
        or template.purpose != "marketing"
        or not templates.sendable(template)
    ):
        raise conflict(
            "template_not_registered",
            "SMS and WhatsApp campaigns need a registered marketing template (DLT / approved WhatsApp template)",
        )


async def submit(
    ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID, data: CampaignSubmitIn
) -> CampaignOut:
    c = await _get(db, campaign_id, lock=True)
    if c.status != "draft":
        raise conflict(
            "invalid_transition",
            f"A {c.status} campaign cannot be submitted",
            status=c.status,
            to="scheduled",
        )
    for guard in list(_SUBMIT_GUARDS):
        await guard(ctx, db, c)
    await _validate_content(db, c)
    preview = await audiences.preview(ctx, db, c.audience_id, c.channel)
    recipients = int(preview.reachable.get(c.channel, 0))
    c.counts = {
        "audience": preview.total,
        "reachable": recipients,
        "suppressed_preview": preview.suppressed.get(c.channel, {}),
    }
    c.scheduled_at = data.scheduled_at or now()
    row = await mkt_settings.get_row(db)
    if recipients >= row.approval_threshold:
        await transition(ctx, db, c, "pending_approval")
        await _start_approval(ctx, db, c, recipients)
    else:
        principal = principal_of(ctx)
        if not principal.has(SEND):
            raise Forbidden("Scheduling a campaign needs the campaign:send permission", code="forbidden")
        await _schedule(ctx, db, c)
    await db.flush()
    return await to_out(db, c)


async def _schedule(ctx: RequestContext, db: AsyncSession, c: Campaign) -> None:
    c.snapshot_id = await audiences.freeze(ctx, db, c.audience_id, "campaign", dict(c.counts or {}))
    await transition(ctx, db, c, "scheduled")
    if c.scheduled_at is None or c.scheduled_at <= now():
        emit(db, CampaignStartRequested(tenant_id=c.tenant_id, campaign_id=c.id))


async def _start_approval(ctx: RequestContext, db: AsyncSession, c: Campaign, recipients: int) -> None:
    from app.core.actors import ActorRef
    from app.models.approvals import ApprovalChain
    from app.services import approvals

    has_chain = (
        await db.execute(
            select(ApprovalChain.id).where(
                ApprovalChain.subject_type == APPROVAL_SUBJECT, ApprovalChain.is_active.is_(True)
            )
        )
    ).first() is not None
    result = await approvals.start(
        ctx,
        db,
        APPROVAL_SUBJECT,
        c.id,
        {"recipients": recipients, "channel": c.channel, "owner_id": str(c.owner_id)},
        steps=None
        if has_chain
        else [
            approvals.StepSpec("Recruitment Manager approval", "role", {"role_key": DEFAULT_APPROVER_ROLE})
        ],
        initiated_by=ActorRef("user", user_id_of(ctx)) if user_id_of(ctx) else ActorRef("system", None),
    )
    c.approval_request_id = result.request_id
    await db.flush()
    if result.request_id is not None:
        from app.services import notifications_service

        await notifications_service.fan_out(
            db,
            "campaign_approval_requested",
            {
                "campaign_id": str(c.id),
                "request_id": str(result.request_id),
                "actor_user_id": str(user_id_of(ctx) or ""),
            },
        )
    elif result.status == "auto_approved":
        await _schedule(ctx, db, c)


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    c = await db.get(Campaign, decision.subject_id, with_for_update=True)
    if c is None or c.status != "pending_approval":
        return
    c.approval_request_id = None
    if decision.outcome in ("approved", "auto_approved"):
        await _schedule(ctx, db, c)
    else:
        await transition(ctx, db, c, "draft", actor="approval")


async def describe(ctx: RequestContext, db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, Any]:
    from app.schemas.approvals import SubjectSummary

    rows = (await db.execute(select(Campaign).where(Campaign.id.in_(ids)))).scalars()
    return {
        c.id: SubjectSummary(
            label=f"{int((c.counts or {}).get('reachable', 0))} recipients ({c.channel})",
            title=f"Campaign {c.name}",
        )
        for c in rows
    }


# ================================================================ pause / resume / cancel
async def pause(ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID) -> CampaignOut:
    c = await _get(db, campaign_id, lock=True)
    await transition(ctx, db, c, "paused")
    return await to_out(db, c)


async def resume(ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID) -> CampaignOut:
    c = await _get(db, campaign_id, lock=True)
    if c.status != "paused":
        raise conflict(
            "invalid_transition", f"A {c.status} campaign cannot be resumed", status=c.status, to="sending"
        )
    if (await mkt_settings.get_row(db)).paused_at is not None:
        raise conflict(
            "marketing_paused", "Marketing is paused for this workspace; an admin must resume it first"
        )
    if c.started_at is None:
        await transition(ctx, db, c, "scheduled")
        if c.scheduled_at is None or c.scheduled_at <= now():
            emit(db, CampaignStartRequested(tenant_id=c.tenant_id, campaign_id=c.id))
        return await to_out(db, c)
    await transition(ctx, db, c, "sending")
    held = list(
        (
            await db.execute(
                select(CampaignMessage.id).where(
                    CampaignMessage.campaign_id == c.id,
                    CampaignMessage.status.in_(("pending", "deferred")),
                    (CampaignMessage.defer_until.is_(None)) | (CampaignMessage.defer_until <= now()),
                )
            )
        ).scalars()
    )
    _emit_batches(db, c.tenant_id, held)
    return await to_out(db, c)


async def cancel(ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID, reason: str) -> CampaignOut:
    c = await _get(db, campaign_id, lock=True)
    await transition(ctx, db, c, "cancelled")
    c.cancel_reason = reason[:500]
    await db.execute(
        update(CampaignMessage)
        .where(CampaignMessage.campaign_id == c.id, CampaignMessage.status.in_(("pending", "deferred")))
        .values(status="cancelled", defer_until=None, updated_at=now())
    )
    if c.approval_request_id is not None:
        from app.services import approvals

        await approvals.cancel_open(ctx, db, APPROVAL_SUBJECT, c.id)
        c.approval_request_id = None
    await db.flush()
    return await to_out(db, c)


# ================================================================ dispatch
def _bucket(candidate_id: uuid.UUID, salt: str) -> int:
    return int(hashlib.sha256(f"{salt}:{candidate_id}".encode()).hexdigest()[:8], 16)


def ab_split(candidate_ids: list[uuid.UUID], campaign_id: uuid.UUID, sample_pct: int) -> dict[uuid.UUID, str]:
    """Deterministic sample (``sample_pct`` of the snapshot by hash of candidate id, at least one per variant) split
    evenly A / B; the rest waits for the winner."""
    if sample_pct <= 0 or not candidate_ids:
        return {}
    size = (
        max(2, round(len(candidate_ids) * sample_pct / 100))
        if len(candidate_ids) >= 2
        else len(candidate_ids)
    )
    ranked = sorted(candidate_ids, key=lambda c: _bucket(c, str(campaign_id)))[:size]
    sample = sorted(ranked, key=lambda c: _bucket(c, "ab"))
    return {cid: ("A" if i % 2 == 0 else "B") for i, cid in enumerate(sample)}


def _emit_batches(db: AsyncSession, tenant_id: uuid.UUID, ids: list[uuid.UUID]) -> None:
    for i in range(0, len(ids), BATCH):
        emit(db, MarketingBatchReady(tenant_id=tenant_id, message_ids=tuple(ids[i : i + BATCH])))


async def _insert_messages(
    db: AsyncSession, c: Campaign, members: list[uuid.UUID], variant: dict[uuid.UUID, str] | None
) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    for i in range(0, len(members), 5000):
        part = members[i : i + 5000]
        values = [
            {
                "id": uuid.uuid4(),
                "tenant_id": c.tenant_id,
                "campaign_id": c.id,
                "candidate_id": cid,
                "channel": c.channel,
                "variant_key": (variant or {}).get(cid),
                "status": "pending",
                "token_nonce": tokens.new_nonce(),
            }
            for cid in part
        ]
        rows = await db.execute(
            pg_insert(CampaignMessage)
            .values(values)
            .on_conflict_do_nothing(
                index_elements=["campaign_id", "candidate_id"],
                index_where=CampaignMessage.campaign_id.is_not(None),
            )
            .returning(CampaignMessage.id)
        )
        ids.extend(rows.scalars())
    await db.flush()
    return ids


async def dispatch(db: AsyncSession, c: Campaign) -> list[uuid.UUID]:
    """``scheduled -> sending``: create the messages of the snapshot (A/B: the sample only) and emit the batches."""
    ctx = system_ctx(c.tenant_id)
    if c.status == "scheduled":
        await transition(ctx, db, c, "sending", actor="system")
    if c.status != "sending" or c.snapshot_id is None:
        return []
    members = list(
        (
            await db.execute(
                select(AudienceMember.candidate_id)
                .where(AudienceMember.snapshot_id == c.snapshot_id)
                .order_by(AudienceMember.candidate_id)
            )
        ).scalars()
    )
    variant: dict[uuid.UUID, str] | None = None
    if c.ab_test and c.channel == "email" and not c.ab_test.get("winner_key"):
        variant = ab_split(members, c.id, int(c.ab_test.get("sample_pct") or 0))
        members = [m for m in members if m in variant]
    ids = await _insert_messages(db, c, members, variant)
    _emit_batches(db, c.tenant_id, ids)
    return ids


async def start_due(
    db: AsyncSession, campaign_id: uuid.UUID | None = None, at: datetime | None = None
) -> int:
    stmt = (
        select(Campaign)
        .where(Campaign.status == "scheduled", Campaign.scheduled_at <= (at or now()))
        .with_for_update(skip_locked=True)
    )
    if campaign_id is not None:
        stmt = stmt.where(Campaign.id == campaign_id)
    if (await mkt_settings.get_row(db)).paused_at is not None:
        return 0
    started = 0
    for c in list((await db.execute(stmt)).scalars()):
        await dispatch(db, c)
        started += 1
    return started


async def _rate(db: AsyncSession, c: Campaign, key: str, metric: str) -> float:
    kind = "opened" if metric == "open" else "clicked"
    sent = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CampaignMessage)
                .where(
                    CampaignMessage.campaign_id == c.id,
                    CampaignMessage.variant_key == key,
                    CampaignMessage.status.in_(("queued", "sent", "delivered")),
                )
            )
        ).scalar_one()
    )
    engaged = int(
        (
            await db.execute(
                select(func.count(func.distinct(EngagementEvent.campaign_message_id)))
                .join(CampaignMessage, CampaignMessage.id == EngagementEvent.campaign_message_id)
                .where(
                    CampaignMessage.campaign_id == c.id,
                    CampaignMessage.variant_key == key,
                    EngagementEvent.kind == kind,
                )
            )
        ).scalar_one()
    )
    return engaged / sent if sent else 0.0


async def decide_ab(
    db: AsyncSession, at: datetime | None = None, campaign_id: uuid.UUID | None = None
) -> int:
    """After ``window_hours``: winner by open (or click) rate, ties -> A; dispatch the remainder with the winner."""
    when = at or now()
    stmt = (
        select(Campaign)
        .where(Campaign.status == "sending", Campaign.ab_test.is_not(None))
        .with_for_update(skip_locked=True)
    )
    if campaign_id is not None:
        stmt = stmt.where(Campaign.id == campaign_id)
    decided = 0
    for c in list((await db.execute(stmt)).scalars()):
        ab = dict(c.ab_test or {})
        if ab.get("winner_key") or c.started_at is None:
            continue
        if c.started_at + timedelta(hours=int(ab.get("window_hours") or 4)) > when:
            continue
        metric = str(ab.get("metric") or "open")
        a, b = await _rate(db, c, "A", metric), await _rate(db, c, "B", metric)
        ab["winner_key"] = "B" if b > a else "A"
        ab["decided_at"] = when.isoformat()
        c.ab_test = ab
        members = list(
            (
                await db.execute(
                    select(AudienceMember.candidate_id).where(AudienceMember.snapshot_id == c.snapshot_id)
                )
            ).scalars()
        )
        winner = dict.fromkeys(members, ab["winner_key"])
        ids = await _insert_messages(db, c, members, winner)
        _emit_batches(db, c.tenant_id, ids)
        audit.record("campaign.ab_decided", "campaign", c.id, session=db, winner=ab["winner_key"])
        decided += 1
    await db.flush()
    return decided


async def complete_finished(db: AsyncSession) -> int:
    """``sending -> completed`` once every message is terminal (and an A/B test has dispatched its remainder)."""
    from app.services import notifications_service

    done = 0
    for c in list(
        (
            await db.execute(
                select(Campaign).where(Campaign.status == "sending").with_for_update(skip_locked=True)
            )
        ).scalars()
    ):
        if c.ab_test and not c.ab_test.get("winner_key"):
            continue
        open_left = (
            await db.execute(
                select(CampaignMessage.id)
                .where(CampaignMessage.campaign_id == c.id, CampaignMessage.status.in_(OPEN_MESSAGE_STATUSES))
                .limit(1)
            )
        ).first()
        if open_left is not None:
            continue
        await transition(system_ctx(c.tenant_id), db, c, "completed", actor="system")
        await notifications_service.fan_out(
            db, "campaign_completed", {"campaign_id": str(c.id), "owner_id": str(c.owner_id)}
        )
        done += 1
    return done


# ================================================================ messages / test send
async def list_messages(
    ctx: RequestContext,
    db: AsyncSession,
    campaign_id: uuid.UUID,
    status: str | None,
    page: int,
    page_size: int,
) -> CampaignMessagesPage:
    await _get(db, campaign_id)
    base = (
        select(CampaignMessage, Candidate.full_name)
        .join(Candidate, Candidate.id == CampaignMessage.candidate_id)
        .where(CampaignMessage.campaign_id == campaign_id)
    )
    count_stmt = (
        select(func.count()).select_from(CampaignMessage).where(CampaignMessage.campaign_id == campaign_id)
    )
    if status:
        base = base.where(CampaignMessage.status == status)
        count_stmt = count_stmt.where(CampaignMessage.status == status)
    total = int((await db.execute(count_stmt)).scalar_one())
    rows = (
        await db.execute(
            base.order_by(Candidate.full_name, CampaignMessage.id)
            .offset((page - 1) * page_size)
            .limit(page_size)
        )
    ).all()
    return CampaignMessagesPage(
        items=[
            CampaignMessageOut(
                id=m.id,
                candidate_id=m.candidate_id,
                candidate_name=name,
                channel=m.channel,
                status=m.status,
                suppression_reason=m.suppression_reason,
                variant_key=m.variant_key,
                sent_at=m.sent_at,
                defer_until=m.defer_until,
            )
            for m, name in rows
        ],
        total=total,
        page=page,
        page_size=page_size,
    )


async def test_send(ctx: RequestContext, db: AsyncSession, campaign_id: uuid.UUID) -> TestSendOut:
    """Send the content to the caller's own address only (never to a candidate)."""
    from app.services import comms_service, entity_refs

    c = await _get(db, campaign_id)
    user = await entity_refs.user(db, user_id_of(ctx))
    if user is None:
        raise Forbidden("Test sends are for signed-in users")
    if c.channel != "email":
        return TestSendOut(
            channel=c.channel,
            queued=False,
            message="Test sends of SMS and WhatsApp use the template preview in Messaging settings",
        )
    await _validate_content(db, c)
    subject = c.email_subject or ""
    if c.ab_test and c.ab_test.get("variants"):
        subject = str(c.ab_test["variants"][0].get("subject") or subject)
    row = await mkt_settings.get_row(db)
    body = f"{c.email_body_html or ''}<hr><p>{row.footer_contact or ''}</p><p>(Test send: links and tracking are disabled.)</p>"
    await comms_service.queue_email(
        ctx,
        db,
        origin="user",
        sender_user_id=user.id,
        to=[comms_service.RecipientSpec("to", str(user.email))],
        subject=f"[Test] {subject}",
        body_html=body,
        category="process",
        raw=False,
    )
    return TestSendOut(channel="email", queued=True, message="Test email queued to your address")


def require_approve(ctx: RequestContext) -> None:
    if not principal_of(ctx).has(APPROVE):
        raise Forbidden("Approving campaigns needs campaign:approve")
