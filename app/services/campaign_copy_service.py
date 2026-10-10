# ruff: noqa: E501
"""AI campaign copy facade (Epic 78, FR-401..FR-403, AD-91). The ``campaign_copy`` agent drafts copy from the brief,
the aggregate segment definitions, linked postings' public fields and the Tenant's templates -- never recipient
records. Drafts are checked deterministically, edited, approved by a person (★ ``campaign:copy_approve``) and only
then applied to a draft Campaign / Sequence step; any later edit needs approval again (P3 guards). The P3 approval
chain and send governance apply unchanged; nothing here sends."""

from datetime import timedelta

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaign_copy import CampaignCopyDraft
from app.services.campaign_copy.apply import apply
from app.services.campaign_copy.approval import approve
from app.services.campaign_copy.common import PURGE_DAYS, now
from app.services.campaign_copy.drafts import (
    acknowledge_warnings,
    discard,
    edit_draft,
    mark_opened,
    mark_segment_opened,
)
from app.services.campaign_copy.guards import campaign_status
from app.services.campaign_copy.requests import cancel, get_request, list_requests, request_copy
from app.services.campaign_copy.results import finalize_from_run


async def purge(db: AsyncSession) -> int:
    """Retention: discarded or never-used drafts older than 180 days."""
    cutoff = now() - timedelta(days=PURGE_DAYS)
    res = await db.execute(
        delete(CampaignCopyDraft).where(
            CampaignCopyDraft.created_at < cutoff,
            or_(CampaignCopyDraft.status == "discarded", CampaignCopyDraft.status.in_(("generated", "checked", "edited", "approved"))),
        )
    )
    return int(getattr(res, "rowcount", 0) or 0)


__all__ = [
    "acknowledge_warnings",
    "apply",
    "approve",
    "campaign_status",
    "cancel",
    "discard",
    "edit_draft",
    "finalize_from_run",
    "get_request",
    "list_requests",
    "mark_opened",
    "mark_segment_opened",
    "purge",
    "request_copy",
]
