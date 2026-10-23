# ruff: file-ignore[line-too-long]
"""Application attribution (Epic 39 A6, FR-198): last touch within 30 days (user decision, fixed in Phase 3).

``ApplicationCreated``: ``source='campaign'`` uses ``source_ref`` (the campaign message id set by the careers apply
``attribution_token``); otherwise the candidate's latest ``clicked`` (else handed-off / sent) marketing message
within 30 days before the application wins. Writes ``campaign_attributions`` + an ``applied`` event, and exits
enrolments that exit on ``applied``. ``PlacementCreated`` / stage ``hired`` marks the attribution hired.
"""

import uuid
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaigns import CampaignAttribution, CampaignMessage, EngagementEvent, Enrollment
from app.models.pipeline import Application
from app.services.campaigns import engagement, tokens
from app.services.campaigns.common import as_uuid, now

WINDOW = timedelta(days=30)


async def _last_touch(
    db: AsyncSession, candidate_id: uuid.UUID, at: datetime
) -> tuple[CampaignMessage, datetime] | None:
    since = at - WINDOW
    click = (
        await db.execute(
            select(CampaignMessage, EngagementEvent.occurred_at)
            .join(EngagementEvent, EngagementEvent.campaign_message_id == CampaignMessage.id)
            .where(
                CampaignMessage.candidate_id == candidate_id,
                EngagementEvent.kind == "clicked",
                EngagementEvent.occurred_at >= since,
                EngagementEvent.occurred_at <= at,
            )
            .order_by(EngagementEvent.occurred_at.desc())
            .limit(1)
        )
    ).first()
    if click is not None:
        return click[0], click[1]
    sent = (
        await db.execute(
            select(CampaignMessage)
            .where(
                CampaignMessage.candidate_id == candidate_id,
                CampaignMessage.status.in_(("queued", "sent", "delivered")),
                CampaignMessage.sent_at >= since,
                CampaignMessage.sent_at <= at,
            )
            .order_by(CampaignMessage.sent_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    return (sent, sent.sent_at) if sent is not None and sent.sent_at is not None else None


async def on_application_created(db: AsyncSession, application_id: uuid.UUID) -> bool:
    app_row = await db.get(Application, application_id)
    if app_row is None:
        return False
    at = app_row.created_at or now()
    touch: tuple[CampaignMessage, datetime] | None = None
    if app_row.source == "campaign":
        ref = as_uuid(app_row.source_ref)
        cm = await db.get(CampaignMessage, ref) if ref else None
        if cm is not None and cm.candidate_id == app_row.candidate_id:
            touch = (cm, at)
    if touch is None:
        touch = await _last_touch(db, app_row.candidate_id, at)
    if touch is None:
        return False
    cm, touched = touch
    sequence_id = None
    if cm.enrollment_id is not None:
        sequence_id = (
            await db.execute(select(Enrollment.sequence_id).where(Enrollment.id == cm.enrollment_id))
        ).scalar_one_or_none()
    inserted = (
        await db.execute(
            pg_insert(CampaignAttribution)
            .values(
                tenant_id=cm.tenant_id,
                application_id=application_id,
                campaign_message_id=cm.id,
                candidate_id=cm.candidate_id,
                campaign_id=cm.campaign_id,
                sequence_id=sequence_id,
                touched_at=touched,
                attributed_at=now(),
            )
            .on_conflict_do_nothing()
            .returning(CampaignAttribution.application_id)
        )
    ).scalar_one_or_none()
    if inserted is None:
        return False
    await engagement.record(
        db, cm, "applied", dedupe=f"applied:{application_id}", meta={"application_id": str(application_id)}
    )
    await engagement.exit_enrollments(db, app_row.candidate_id, "applied")
    return True


async def on_hired(
    db: AsyncSession, candidate_id: uuid.UUID, application_id: uuid.UUID | None = None
) -> bool:
    stmt = select(CampaignAttribution).where(
        CampaignAttribution.candidate_id == candidate_id, CampaignAttribution.hired_at.is_(None)
    )
    if application_id is not None:
        stmt = stmt.where(CampaignAttribution.application_id == application_id)
    rows = list((await db.execute(stmt.with_for_update())).scalars())
    for attribution in rows:
        attribution.hired_at = now()
        cm = await db.get(CampaignMessage, attribution.campaign_message_id)
        if cm is not None:
            await engagement.record(db, cm, "hired", dedupe=f"hired:{attribution.application_id}")
    await engagement.exit_enrollments(db, candidate_id, "hired")
    return bool(rows)


async def resolve_attribution_token(db: AsyncSession, tenant_id: uuid.UUID, token: str) -> str | None:
    """Careers apply hook: a valid token of this tenant -> the campaign message id (``source_ref``), else None."""
    parsed = tokens.parse(token)
    if parsed is None or parsed[0] != tenant_id:
        return None
    cm = await db.get(CampaignMessage, parsed[1])
    if cm is None or not tokens.verify(parsed[0], parsed[1], parsed[2], cm.token_nonce):
        return None
    return str(cm.id)
