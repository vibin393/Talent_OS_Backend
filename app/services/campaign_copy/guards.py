# ruff: file-ignore[line-too-long]
"""Re-approval guards (Epic 78 A5, FR-403 "edits after approval need approval again"), registered in the P3 additive
registries ``campaigns.register_submit_guard`` / ``sequences.register_step_activation_guard``.

A Campaign (or Sequence step) whose copy came from an AI draft (a ``used`` draft) can be submitted / published only
while its current copy hash equals the hash recorded when the approved copy was applied. Campaigns without AI copy
are unaffected. After the guard, the P3 approval threshold chain and send governance apply unchanged."""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.campaign_copy import CampaignCopyDraft
from app.schemas.campaign_copy import CopyStatusOut
from app.services.campaign_copy.apply import campaign_hash, step_hash
from app.services.campaign_copy.common import conflict

REAPPROVAL_MESSAGE = "This campaign's AI-drafted copy changed after approval. Approve it again."
STEP_REAPPROVAL_MESSAGE = "This sequence step's AI-drafted copy changed after approval. Approve it again."


async def _latest_used_for_campaign(db: AsyncSession, campaign_id: Any) -> CampaignCopyDraft | None:
    return (
        await db.execute(
            select(CampaignCopyDraft)
            .where(CampaignCopyDraft.used_in_campaign_id == campaign_id, CampaignCopyDraft.status == "used")
            .order_by(CampaignCopyDraft.used_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def submit_guard(ctx: RequestContext, db: AsyncSession, campaign: Any) -> None:  # ruff: ignore[unused-function-argument]
    used = await _latest_used_for_campaign(db, campaign.id)
    if used is None:
        return
    if campaign_hash(campaign) != used.approved_sha256:
        raise conflict("copy_reapproval_required", REAPPROVAL_MESSAGE, draft_id=str(used.id), request_id=str(used.request_id))


async def step_activation_guard(ctx: RequestContext, db: AsyncSession, seq: Any, steps: list[dict[str, Any]]) -> None:  # ruff: ignore[unused-function-argument]
    rows = list(
        (
            await db.execute(
                select(CampaignCopyDraft)
                .where(CampaignCopyDraft.sequence_id == seq.id, CampaignCopyDraft.status == "used")
                .order_by(CampaignCopyDraft.used_at.desc())
            )
        ).scalars()
    )
    latest: dict[int, CampaignCopyDraft] = {}
    for d in rows:
        if d.step_position is not None and d.step_position not in latest:
            latest[d.step_position] = d
    for position, d in latest.items():
        if position < 1 or position > len(steps):
            continue
        step = steps[position - 1]
        config = dict(step.get("config") or {})
        if step.get("kind") != "send" or str(config.get("channel") or "email") != d.channel:
            continue
        if step_hash(d.channel, config) != d.approved_sha256:
            raise conflict("copy_reapproval_required", STEP_REAPPROVAL_MESSAGE, draft_id=str(d.id), step_position=position)


async def campaign_status(ctx: RequestContext, db: AsyncSession, campaign_id: Any) -> CopyStatusOut:  # ruff: ignore[unused-function-argument]
    from app.models.campaigns import Campaign
    from app.services import entity_refs

    used = await _latest_used_for_campaign(db, campaign_id)
    c = await db.get(Campaign, campaign_id)
    if used is None or c is None:
        return CopyStatusOut(uses_ai_copy=False)
    info = await entity_refs.user(db, used.approved_by) if used.approved_by else None
    name = info.name if info is not None else None
    return CopyStatusOut(
        uses_ai_copy=True, draft_id=used.id, request_id=used.request_id, approved_by=used.approved_by,
        approved_by_name=name, approved_at=used.approved_at, changed_after_approval=campaign_hash(c) != used.approved_sha256,
    )  # fmt: skip


def register() -> None:
    from app.services.campaigns import campaigns, sequences

    campaigns.register_submit_guard(submit_guard)
    sequences.register_step_activation_guard(step_activation_guard)


__all__ = ["campaign_status", "register", "step_activation_guard", "submit_guard"]
