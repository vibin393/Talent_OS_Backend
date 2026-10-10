# ruff: noqa: E501
"""Marketing erasure handler ``campaigns.marketing`` (order 55), Epic 39 A9 (FR-258, AD-34).

Deletes the subject's audience list entries, snapshot memberships, campaign messages, engagement events, attributions
and enrolments. Suppression facts survive in P1 ``email_address_status`` / P2 ``channel_opt_outs`` (hash) as before;
campaign aggregates (``campaigns.counts``) are kept. ``export`` lists the campaigns / sequences that reached the
subject (names, channel, status and dates only -- never bodies).
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaigns import (
    AudienceListEntry,
    AudienceMember,
    Campaign,
    CampaignAttribution,
    CampaignMessage,
    EngagementEvent,
    Enrollment,
)
from app.models.campaigns import Sequence as SequenceModel
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

MODELS = (
    EngagementEvent,
    CampaignAttribution,
    CampaignMessage,
    Enrollment,
    AudienceMember,
    AudienceListEntry,
)


class CampaignsMarketingHandler:
    name = "campaigns.marketing"
    order = 55
    tables = (
        "audience_list_entries",
        "audience_members",
        "campaign_messages",
        "engagement_events",
        "campaign_attributions",
        "enrollments",
    )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        out: dict[str, int] = {}
        for model in MODELS:
            result = await db.execute(delete(model).where(model.candidate_id.in_(ids)))  # type: ignore[attr-defined]
            out[model.__tablename__] = int(getattr(result, "rowcount", 0) or 0)
        await db.flush()
        return out

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        campaigns = (
            await db.execute(
                select(
                    Campaign.name, CampaignMessage.channel, CampaignMessage.status, CampaignMessage.sent_at
                )
                .join(Campaign, Campaign.id == CampaignMessage.campaign_id)
                .where(CampaignMessage.candidate_id == ctx.candidate_id)
                .order_by(CampaignMessage.created_at)
            )
        ).all()
        sequences = (
            await db.execute(
                select(SequenceModel.name, Enrollment.status, Enrollment.started_at, Enrollment.ended_at)
                .join(SequenceModel, SequenceModel.id == Enrollment.sequence_id)
                .where(Enrollment.candidate_id == ctx.candidate_id)
                .order_by(Enrollment.started_at)
            )
        ).all()
        if not campaigns and not sequences:
            return None
        return {
            "campaign_messages": [
                {"campaign": n, "channel": c, "status": s, "sent_at": t} for n, c, s, t in campaigns
            ],
            "sequence_enrollments": [
                {"sequence": n, "status": s, "started_at": a, "ended_at": b} for n, s, a, b in sequences
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left: list[str] = []
        for model in MODELS:
            row = (
                await db.execute(select(model.candidate_id).where(model.candidate_id.in_(ids)).limit(1))
            ).first()  # type: ignore[attr-defined]
            if row is not None:
                left.append(f"{model.__tablename__} rows remain")
        return left


register_erasure_handler(CampaignsMarketingHandler())
