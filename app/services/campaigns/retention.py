# ruff: noqa: E501, ARG001
"""Retention of marketing logs (Epic 39 A9; user decision: 2 years).

Class ``marketing_logs`` (24 months, delete) is seeded by Epic 52; ``campaign_messages`` (anchor ``created_at``) and
``engagement_events`` (anchor ``occurred_at``) are bound to it from ``hooks.py``. Expiry deletes the rows and folds
their per-status counts into ``campaigns.counts['expired']`` so campaign aggregates survive.
"""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaigns import Campaign, CampaignMessage, EngagementEvent


async def expire_messages(
    db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete"
) -> dict[str, int]:
    ids = list(ids)
    if not ids:
        return {"deleted": 0}
    rows = await db.execute(
        select(CampaignMessage.campaign_id, CampaignMessage.status, func.count())
        .where(CampaignMessage.id.in_(ids), CampaignMessage.campaign_id.is_not(None))
        .group_by(CampaignMessage.campaign_id, CampaignMessage.status)
    )
    folded: dict[uuid.UUID, dict[str, int]] = {}
    for cid, status, n in rows.all():
        folded.setdefault(cid, {})[status] = int(n)
    for cid, counts in folded.items():
        campaign = await db.get(Campaign, cid)
        if campaign is None:
            continue
        merged: dict[str, Any] = dict(campaign.counts or {})
        expired = dict(merged.get("expired") or {})
        for status, n in counts.items():
            expired[status] = int(expired.get(status, 0)) + n
        merged["expired"] = expired
        campaign.counts = merged
    result = await db.execute(delete(CampaignMessage).where(CampaignMessage.id.in_(ids)))
    await db.flush()
    return {"deleted": int(getattr(result, "rowcount", 0) or 0)}


async def expire_events(db: AsyncSession, ids: Sequence[uuid.UUID], action: str = "delete") -> dict[str, int]:
    ids = list(ids)
    if not ids:
        return {"deleted": 0}
    result = await db.execute(delete(EngagementEvent).where(EngagementEvent.id.in_(ids)))
    await db.flush()
    return {"deleted": int(getattr(result, "rowcount", 0) or 0)}
