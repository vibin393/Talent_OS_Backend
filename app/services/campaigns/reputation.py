# ruff: file-ignore[line-too-long, non-imperative-mood]
"""Sender reputation guard (Epic 39 A4, NFR-34): beat every 15 minutes per tenant.

Over the last 7 days of marketing email: complaint rate = complaints / delivered, hard-bounce rate = hard bounces /
sent (minimum denominator 500). Complaint > 0.1 % or hard bounce > 2 % -> the tenant is paused
(``marketing_settings.paused_*``), every ``sending|scheduled`` campaign -> ``paused`` (system actor), Tenant Admins
(``marketing_tenant_paused``, mandatory in-app) and campaign owners (``campaign_paused``) are notified. Resuming needs
``marketing:configure``.
"""

from datetime import datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaigns import Campaign, CampaignMessage, EngagementEvent
from app.services.campaigns import settings as mkt_settings
from app.services.campaigns.common import now

MIN_DENOMINATOR = 500
COMPLAINT_LIMIT = 0.001
HARD_BOUNCE_LIMIT = 0.02


async def _events(db: AsyncSession, kind: str, since: datetime, *, hard_only: bool = False) -> int:
    stmt = (
        select(func.count(func.distinct(EngagementEvent.campaign_message_id)))
        .join(CampaignMessage, CampaignMessage.id == EngagementEvent.campaign_message_id)
        .where(
            CampaignMessage.channel == "email",
            EngagementEvent.kind == kind,
            EngagementEvent.occurred_at >= since,
        )
    )
    if hard_only:
        stmt = stmt.where(EngagementEvent.meta["bounce_type"].astext == "hard")
    return int((await db.execute(stmt)).scalar_one())


async def check(db: AsyncSession, at: datetime | None = None) -> str | None:
    """Returns the pause reason when the tenant was paused by this run, else None."""
    row = await mkt_settings.get_row(db)
    if row.paused_at is not None:
        return None
    since = (at or now()) - timedelta(days=7)
    sent = int(
        (
            await db.execute(
                select(func.count())
                .select_from(CampaignMessage)
                .where(
                    CampaignMessage.channel == "email",
                    CampaignMessage.sent_at >= since,
                    CampaignMessage.status.in_(("queued", "sent", "delivered", "bounced")),
                )
            )
        ).scalar_one()
    )
    delivered = await _events(db, "delivered", since)
    complaints = await _events(db, "complained", since)
    hard = await _events(db, "bounced", since, hard_only=True)
    reason: str | None = None
    if delivered >= MIN_DENOMINATOR and complaints / delivered > COMPLAINT_LIMIT:
        reason = "complaint_rate"
    elif sent >= MIN_DENOMINATOR and hard / sent > HARD_BOUNCE_LIMIT:
        reason = "bounce_rate"
    if reason is None:
        return None
    paused_campaigns = list(
        (
            await db.execute(
                select(Campaign.id, Campaign.owner_id).where(Campaign.status.in_(("sending", "scheduled")))
            )
        ).all()
    )
    if not await mkt_settings.pause_tenant(db, reason):
        return None
    from app.services import notifications_service

    await notifications_service.fan_out(db, "marketing_tenant_paused", {"reason": reason})
    for cid, owner in paused_campaigns:
        await notifications_service.fan_out(
            db, "campaign_paused", {"campaign_id": str(cid), "owner_id": str(owner), "reason": reason}
        )
    return reason
