# ruff: file-ignore[line-too-long]
"""Copy requests (Epic 78 A2, FR-401 / FR-402): limits, template and target rules, budget, one open request per
target, then a ``campaign_copy`` agent run (queue ``agents``). Human only (``campaign:write`` / ``sequence:write``)."""

import contextlib
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.campaign_copy import (
    MAX_SEGMENTS,
    MAX_VARIANTS,
    OPEN_REQUEST_STATUSES,
    CampaignCopyDraft,
    CampaignCopyRequest,
)
from app.schemas.campaign_copy import CopyRequestDetailOut, CopyRequestIn, CopyRequestOut, SegmentOut
from app.services.campaign_copy import context as facts
from app.services.campaign_copy import drafts as drafts_mod
from app.services.campaign_copy.common import (
    AGENT_KEY,
    BUDGET_MESSAGE,
    DEFAULT_MONTHLY_TOKENS,
    FALLBACK_MESSAGE,
    FEATURE,
    FEATURE_OFF_MESSAGE,
    conflict,
    human_user,
    load_request,
    require_perm,
    tenant_of,
)


def to_out(r: CampaignCopyRequest) -> CopyRequestOut:
    return CopyRequestOut(
        id=r.id, campaign_id=r.campaign_id, sequence_id=r.sequence_id, sequence_step_id=r.sequence_step_id,
        step_position=r.step_position, channel=r.channel, status=r.status,  # type: ignore[arg-type]
        variants_per_segment=r.variants_per_segment, segment_count=len(r.segments or []), agent_run_id=r.agent_run_id,
        error_code=r.error_code, fallback_message=FALLBACK_MESSAGE if r.status == "failed" else None,
        requested_by=r.requested_by, created_at=r.created_at,
    )  # fmt: skip


async def _feature_on(db: AsyncSession) -> None:
    from app.services import ai

    features = await ai.features_map(db)
    if not features.get("agentic", False) or not features.get(FEATURE, False):
        raise conflict("ai_feature_disabled", FEATURE_OFF_MESSAGE, feature=FEATURE)


async def agent_tokens_this_month(db: AsyncSession) -> int:
    from app.models.ai import AIAction
    from app.services.agent_runtime.state import month_start

    tokens = func.coalesce(AIAction.input_tokens, 0) + func.coalesce(AIAction.output_tokens, 0)
    stmt = select(func.coalesce(func.sum(tokens), 0)).where(AIAction.agent_key == AGENT_KEY, AIAction.created_at >= month_start())
    return int((await db.execute(stmt)).scalar_one() or 0)


async def monthly_budget(db: AsyncSession) -> int:
    from app.models.agent_runtime import AgentConfig

    config = (await db.execute(select(AgentConfig).where(AgentConfig.agent_key == AGENT_KEY))).scalar_one_or_none()
    if config is not None and config.monthly_token_budget is not None:
        return int(config.monthly_token_budget)
    return DEFAULT_MONTHLY_TOKENS


async def _budget(db: AsyncSession) -> None:
    from app.services import ai

    if await ai.bulk_blocked(db, tenant_of(db)) or await agent_tokens_this_month(db) >= await monthly_budget(db):
        raise conflict("budget_exhausted", BUDGET_MESSAGE)


async def _target(db: AsyncSession, data: CopyRequestIn) -> tuple[uuid.UUID | None, uuid.UUID | None, int | None, str]:
    """(campaign_id, sequence_id, step_position, required permission); 409 ``campaign_not_draft`` when not editable."""
    from app.models.campaigns import Campaign, Sequence, SequenceStep

    if data.campaign_id is not None:
        c = await db.get(Campaign, data.campaign_id)
        if c is None:
            raise NotFound("Campaign not found")
        if c.status != "draft":
            raise conflict("campaign_not_draft", "AI copy can be drafted for a draft campaign only", status=c.status)
        if c.channel != data.channel:
            raise ValidationFailed("The channel must match the campaign", code="channel_mismatch", details={"fields": {"channel": c.channel}})
        return c.id, None, None, "campaign:write"
    step = await db.get(SequenceStep, data.sequence_step_id)
    if step is None:
        raise NotFound("Sequence step not found")
    seq = await db.get(Sequence, step.sequence_id)
    if seq is None or seq.status not in ("draft", "paused") or step.version != seq.current_version + 1:
        raise conflict("campaign_not_draft", "AI copy can be drafted for a step of a draft or paused sequence only")
    if step.kind != "send" or str((step.config or {}).get("channel") or "email") != data.channel:
        raise ValidationFailed("The step must be a send step on this channel", code="channel_mismatch")
    return None, seq.id, int(step.position), "sequence:write"


async def _template(db: AsyncSession, data: CopyRequestIn) -> None:
    from app.models.comms import ChannelTemplate

    if data.channel == "email":
        return
    t = await db.get(ChannelTemplate, data.channel_template_id) if data.channel_template_id else None
    if t is None or t.channel != data.channel or t.purpose != "marketing" or t.status != "registered":
        raise ValidationFailed(
            "SMS and WhatsApp copy needs an approved marketing template", code="template_required",
            details={"fields": {"channel_template_id": "Pick an approved marketing template"}},
        )  # fmt: skip


async def request_copy(ctx: RequestContext, db: AsyncSession, data: CopyRequestIn) -> CopyRequestOut:
    from app.core.agentic import Kickoff
    from app.services import agent_runtime_service as runtime

    principal = human_user(ctx)
    if len(data.segments) > MAX_SEGMENTS or data.variants_per_segment > MAX_VARIANTS:
        raise ValidationFailed(
            f"Ask for at most {MAX_SEGMENTS} segments and {MAX_VARIANTS} variants per segment",
            code="copy_request_too_large", details={"max_segments": MAX_SEGMENTS, "max_variants": MAX_VARIANTS},
        )  # fmt: skip
    await _feature_on(db)
    campaign_id, sequence_id, position, permission = await _target(db, data)
    require_perm(principal, permission)
    await _template(db, data)
    open_q = select(CampaignCopyRequest.id).where(CampaignCopyRequest.status.in_(OPEN_REQUEST_STATUSES))
    open_q = (
        open_q.where(CampaignCopyRequest.campaign_id == campaign_id)
        if campaign_id
        else open_q.where(CampaignCopyRequest.sequence_id == sequence_id, CampaignCopyRequest.step_position == position)
    )
    if (await db.execute(open_q.limit(1))).first() is not None:
        raise conflict("copy_request_open", "AI copy is already being drafted for this campaign")
    await _budget(db)
    req = CampaignCopyRequest(
        id=uuid.uuid4(), tenant_id=tenant_of(db), campaign_id=campaign_id, sequence_id=sequence_id,
        sequence_step_id=data.sequence_step_id, step_position=position, channel=data.channel,
        brief=data.brief.model_dump(mode="json"), segments=[s.model_dump(mode="json") for s in data.segments],
        variants_per_segment=data.variants_per_segment, channel_template_id=data.channel_template_id,
        status="requested", requested_by=principal.id, prompt_version="v1",
    )  # fmt: skip
    db.add(req)
    await db.flush()
    subject = ("campaign", campaign_id) if campaign_id else ("sequence_step", data.sequence_step_id)
    ref = await runtime.start_run(
        ctx, db, agent_key=AGENT_KEY, trigger_kind="manual", trigger_key="copy_requested", subject=subject,  # type: ignore[arg-type]
        kickoff=Kickoff(inputs={"request_id": str(req.id), "channel": data.channel}), owner_user_id=principal.id,
        dedup_key=f"copy:{req.id}", origin="agent",
    )  # fmt: skip
    if ref.run_id is None:
        if ref.reason in ("feature_off", "kill_switch"):
            raise conflict("ai_feature_disabled", FEATURE_OFF_MESSAGE, feature=FEATURE, reason=ref.reason)
        if ref.reason == "agent_budget_paused":
            raise conflict("budget_exhausted", BUDGET_MESSAGE)
        req.status, req.error_code = "failed", (ref.reason or "not_started")[:40]
    else:
        req.status, req.agent_run_id = "generating", ref.run_id
    await db.flush()
    audit.record(
        "campaign_copy.requested", "campaign_copy_request", req.id, session=db,
        segments=len(data.segments), variants=data.variants_per_segment, channel=data.channel,
    )  # fmt: skip
    return to_out(req)


async def get_request(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> CopyRequestDetailOut:
    req = await load_request(db, request_id)
    rows = list(
        (
            await db.execute(
                select(CampaignCopyDraft)
                .where(CampaignCopyDraft.request_id == req.id)
                .order_by(CampaignCopyDraft.segment_key, CampaignCopyDraft.variant_no)
            )
        ).scalars()
    )
    by_segment: dict[str, list[CampaignCopyDraft]] = defaultdict(list)
    for d in rows:
        by_segment[d.segment_key].append(d)
    names = await drafts_mod.approver_names(db, rows)
    segments: list[SegmentOut] = []
    for spec in req.segments or []:
        segments.append(
            SegmentOut(
                key=spec["key"], label=spec["label"], source=dict(spec.get("source") or {}),
                reachable=await facts.reachable(ctx, db, req.channel, dict(spec.get("source") or {})),
                drafts=[drafts_mod.to_out(d, names) for d in by_segment.get(spec["key"], [])],
            )
        )  # fmt: skip
    blocked = sum(1 for d in rows if d.status != "discarded" and drafts_mod.is_blocked(d))
    base = to_out(req).model_dump()
    return CopyRequestDetailOut(
        **base, brief=dict(req.brief or {}), segments=segments,
        summary={"segments": len(req.segments or []), "variants": len(rows), "blocked": blocked},
    )  # fmt: skip


async def list_requests(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    campaign_id: uuid.UUID | None = None,
    sequence_step_id: uuid.UUID | None = None,
) -> list[CopyRequestOut]:
    stmt = select(CampaignCopyRequest).order_by(CampaignCopyRequest.created_at.desc()).limit(100)
    if campaign_id is not None:
        stmt = stmt.where(CampaignCopyRequest.campaign_id == campaign_id)
    if sequence_step_id is not None:
        stmt = stmt.where(CampaignCopyRequest.sequence_step_id == sequence_step_id)
    return [to_out(r) for r in (await db.execute(stmt)).scalars()]


async def cancel(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> CopyRequestOut:
    from app.services import agent_runtime_service as runtime

    principal = human_user(ctx)
    req = await load_request(db, request_id, lock=True)
    require_perm(principal, "campaign:write" if req.campaign_id else "sequence:write")
    if req.status not in OPEN_REQUEST_STATUSES:
        raise conflict("invalid_transition", f"A {req.status} request cannot be cancelled", status=req.status)
    req.status = "cancelled"
    if req.agent_run_id is not None:
        with contextlib.suppress(Exception):  # the run may have ended already
            await runtime.cancel_run(ctx, db, req.agent_run_id, reason="Copy request cancelled")
    await db.flush()
    audit.record("campaign_copy.cancelled", "campaign_copy_request", req.id, session=db)
    return to_out(req)


def segment_specs(req: CampaignCopyRequest) -> list[dict[str, Any]]:
    return [dict(s) for s in req.segments or []]


__all__ = ["agent_tokens_this_month", "cancel", "get_request", "list_requests", "monthly_budget", "request_copy", "to_out"]
