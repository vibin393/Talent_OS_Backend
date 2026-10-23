# ruff: file-ignore[line-too-long]
"""Apply approved copy (Epic 78 A5, FR-403): only approved drafts, only to a ``draft`` Campaign (or a draft / paused
Sequence's editable step), always through the P3 services as the signed-in person. The applied P3 content hash is
recorded on each draft (``approved_sha256``) so the P3 submit / activation guards can demand re-approval after any
later edit. Nothing here submits, schedules or sends: the P3 approval chain and send governance apply unchanged.

* ``single`` -- one approved draft -> the Campaign (or the Sequence step).
* ``ab_subjects`` -- two approved email drafts of the same segment with the same body -> P3 A/B subjects A and B.
* ``per_segment_campaigns`` -- one approved draft per audience-source segment -> a new draft Campaign per segment
  (P3 ``create_audience`` + ``create_campaign``; no clone function existed, so no P3 edit). The base stays unchanged.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.campaign_copy import CampaignCopyDraft, CampaignCopyRequest
from app.schemas.campaign_copy import ApplyIn, ApplyOut
from app.services.campaign_copy.common import (
    campaign_copy_hash,
    conflict,
    human_user,
    load_request,
    now,
    require_perm,
    sha256_of,
)

AB_SAMPLE_PCT = 10


def campaign_hash(c: Any) -> str:
    ab = [str(v.get("subject") or "") for v in ((c.ab_test or {}).get("variants") or [])] if c.ab_test else None
    return campaign_copy_hash(
        c.channel, subject=c.email_subject, body_html=c.email_body_html, channel_template_id=c.channel_template_id,
        job_links=c.job_links, ab_subjects=ab,
    )  # fmt: skip


def step_hash(channel: str, config: dict[str, Any]) -> str:
    return campaign_copy_hash(
        channel, subject=config.get("subject"), body_html=config.get("body_html"),
        channel_template_id=config.get("channel_template_id"), job_links=config.get("job_links"),
    )  # fmt: skip


def _job_links(d: CampaignCopyDraft) -> list[Any]:
    from app.schemas.campaigns import JobLinkIn

    return [JobLinkIn(posting_id=uuid.UUID(str(b["posting_id"])), label=str(b.get("cta") or "")[:200] or None) for b in d.job_link_blocks or []]


async def _drafts(db: AsyncSession, ids: list[uuid.UUID]) -> list[CampaignCopyDraft]:
    rows = list(
        (await db.execute(select(CampaignCopyDraft).where(CampaignCopyDraft.id.in_(ids)).with_for_update())).scalars()
    )
    if len(rows) != len(ids):
        raise ValidationFailed("Unknown draft", code="unknown_draft")
    order = {i: n for n, i in enumerate(ids)}
    rows.sort(key=lambda d: order[d.id])
    not_approved = [str(d.id) for d in rows if d.status != "approved" or d.approved_sha256 != d.content_sha256]
    if not_approved:
        raise conflict("copy_not_approved", "Only approved copy can be used", draft_ids=not_approved)
    if len({d.request_id for d in rows}) != 1:
        raise ValidationFailed("Drafts must come from one request", code="mixed_requests")
    return rows


def _mark_used(d: CampaignCopyDraft, campaign_id: uuid.UUID | None, applied_hash: str) -> None:
    d.status = "used"
    d.used_in_campaign_id = campaign_id
    d.used_at = now()
    d.approved_sha256 = applied_hash


async def _draft_campaign(db: AsyncSession, campaign_id: uuid.UUID | None) -> Any:
    from app.models.campaigns import Campaign

    c = await db.get(Campaign, campaign_id) if campaign_id else None
    if c is None or c.status != "draft":
        raise conflict("campaign_not_draft", "Copy can be used on a draft campaign only", status=getattr(c, "status", None))
    return c


async def _apply_campaign(ctx: RequestContext, db: AsyncSession, req: CampaignCopyRequest, d: CampaignCopyDraft) -> uuid.UUID:
    from app.schemas.campaigns import CampaignUpdate
    from app.services.campaigns import campaigns

    c = await _draft_campaign(db, req.campaign_id)
    if req.channel == "email":
        update = CampaignUpdate(email_subject=d.subject, email_body_html=d.body_html, job_links=_job_links(d), clear_ab_test=True)
    else:
        update = CampaignUpdate(channel_template_id=req.channel_template_id, job_links=_job_links(d))
    await campaigns.update_campaign(ctx, db, c.id, update)
    await db.refresh(c)
    _mark_used(d, c.id, campaign_hash(c))
    return c.id


async def _apply_step(ctx: RequestContext, db: AsyncSession, req: CampaignCopyRequest, d: CampaignCopyDraft) -> None:
    from app.models.campaigns import Sequence
    from app.schemas.campaigns import SequenceUpdate, StepIn
    from app.services.campaigns import sequences

    seq = await db.get(Sequence, req.sequence_id)
    if seq is None or seq.status not in ("draft", "paused"):
        raise conflict("campaign_not_draft", "Copy can be used on a draft or paused sequence only")
    steps = await sequences.steps_of(db, seq.id, seq.current_version + 1)
    if not any(s.position == req.step_position and s.kind == "send" for s in steps):
        raise conflict("step_changed", "The sequence step this copy was drafted for no longer exists")
    out: list[StepIn] = []
    for s in steps:
        config = dict(s.config or {})
        if s.position == req.step_position:
            if req.channel == "email":
                config.update(subject=d.subject, body_html=d.body_html)
            else:
                config["channel_template_id"] = str(req.channel_template_id) if req.channel_template_id else None
            config["job_links"] = [{"posting_id": str(b["posting_id"]), "label": str(b.get("cta") or "")} for b in d.job_link_blocks or []]
        out.append(StepIn(kind=s.kind, config=config))
    await sequences.update_sequence(ctx, db, seq.id, SequenceUpdate(steps=out))
    live = await sequences.steps_of(db, seq.id, seq.current_version + 1)
    target = next(s for s in live if s.position == req.step_position)
    _mark_used(d, None, step_hash(req.channel, dict(target.config or {})))


async def _per_segment(ctx: RequestContext, db: AsyncSession, req: CampaignCopyRequest, rows: list[CampaignCopyDraft]) -> list[uuid.UUID]:
    from app.models.campaigns import AudienceSource
    from app.schemas.campaigns import AudienceCreate, AudienceSourceIn, CampaignCreate
    from app.services.campaigns import audiences, campaigns

    base = await _draft_campaign(db, req.campaign_id)
    if len({d.segment_key for d in rows}) != len(rows):
        raise ValidationFailed("Pick one approved variant per segment", code="one_variant_per_segment")
    specs = {s["key"]: s for s in req.segments or []}
    created: list[uuid.UUID] = []
    for d in rows:
        source = dict(specs.get(d.segment_key, {}).get("source") or {})
        if source.get("kind") != "audience_source":
            raise ValidationFailed(
                "A campaign per segment needs segments based on an audience source", code="segment_not_audience_source",
                details={"segment": d.segment_key},
            )  # fmt: skip
        src = await db.get(AudienceSource, uuid.UUID(str(source["id"])))
        if src is None:
            raise ValidationFailed("The segment's audience source no longer exists", code="segment_not_audience_source")
        audience = await audiences.create_audience(
            ctx, db,
            AudienceCreate(
                name=f"{base.name} - {d.segment_label}"[:200],
                sources=[AudienceSourceIn(mode="include", kind=src.kind, talent_pool_id=src.talent_pool_id, segment_id=src.segment_id)],
            ),
        )  # fmt: skip
        child = await campaigns.create_campaign(
            ctx, db,
            CampaignCreate(
                name=f"{base.name} - {d.segment_label}"[:200], channel=base.channel, audience_id=audience.id,
                email_subject=d.subject if base.channel == "email" else None,
                email_body_html=d.body_html if base.channel == "email" else None,
                channel_template_id=req.channel_template_id if base.channel != "email" else None, job_links=_job_links(d),
            ),
        )  # fmt: skip
        from app.models.campaigns import Campaign

        row = await db.get(Campaign, child.id)
        _mark_used(d, child.id, campaign_hash(row))
        created.append(child.id)
    return created


async def apply(ctx: RequestContext, db: AsyncSession, data: ApplyIn) -> ApplyOut:
    from app.schemas.campaigns import AbTestIn, AbVariant, CampaignUpdate
    from app.services.campaigns import campaigns

    principal = human_user(ctx)
    rows = await _drafts(db, data.draft_ids)
    req = await load_request(db, rows[0].request_id)
    require_perm(principal, "campaign:write" if req.campaign_id else "sequence:write")
    campaign_ids: list[uuid.UUID] = []
    if data.mode == "single":
        if len(rows) != 1:
            raise ValidationFailed("Pick exactly one approved variant", code="single_variant_only")
        if req.campaign_id:
            campaign_ids.append(await _apply_campaign(ctx, db, req, rows[0]))
        else:
            await _apply_step(ctx, db, req, rows[0])
    elif data.mode == "ab_subjects":
        if req.channel != "email" or req.campaign_id is None:
            raise ValidationFailed("A/B subjects are available for email campaigns only", code="ab_email_only")
        if len(rows) != 2:
            raise ValidationFailed("A/B tests use exactly two variants (A and B)", code="ab_two_variants_only")
        a, b = rows
        if a.segment_key != b.segment_key or sha256_of(a.body_html or "") != sha256_of(b.body_html or ""):
            raise ValidationFailed("A/B subjects need two variants of the same segment with the same body", code="ab_same_body_required")
        c = await _draft_campaign(db, req.campaign_id)
        await campaigns.update_campaign(
            ctx, db, c.id,
            CampaignUpdate(
                email_subject=a.subject, email_body_html=a.body_html, job_links=_job_links(a),
                ab_test=AbTestIn(variants=[AbVariant(key="A", subject=a.subject or ""), AbVariant(key="B", subject=b.subject or "")], sample_pct=AB_SAMPLE_PCT),
            ),
        )  # fmt: skip
        await db.refresh(c)
        applied = campaign_hash(c)
        for d in rows:
            _mark_used(d, c.id, applied)
        campaign_ids.append(c.id)
    else:
        if req.campaign_id is None:
            raise ValidationFailed("A campaign per segment needs a campaign request", code="per_segment_campaign_only")
        campaign_ids = await _per_segment(ctx, db, req, rows)
    await db.flush()
    for d in rows:
        audit.record(
            "campaign_copy.used", "campaign_copy_draft", d.id, session=db, mode=data.mode,
            campaign_id=str(d.used_in_campaign_id) if d.used_in_campaign_id else None, applied_sha256=d.approved_sha256,
        )  # fmt: skip
    return ApplyOut(mode=data.mode, campaign_ids=campaign_ids, sequence_id=req.sequence_id, draft_ids=[d.id for d in rows])


__all__ = ["apply", "campaign_hash", "step_hash"]
