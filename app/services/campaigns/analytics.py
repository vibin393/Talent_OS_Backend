# ruff: noqa: E501
"""Marketing analytics (Epic 39 A8, FR-198): funnel counts and rates from ``campaign_messages`` +
``engagement_events`` (aggregate SQL), per Campaign (and A/B variant), per Sequence (and step) and tenant-wide.
Opens are approximate (pixel only with ``tracking`` consent). ``funnel`` is exposed to Epic 52's dashboard tile."""

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.campaigns import Campaign, CampaignMessage, EngagementEvent, Enrollment, SequenceStep
from app.schemas.campaigns import FunnelCounts, FunnelOut, FunnelRates, FunnelSlice
from app.services.campaigns.common import not_found

SENT_STATUSES = ("queued", "sent", "delivered", "bounced")
EVENT_KINDS = ("delivered", "opened", "clicked", "replied", "unsubscribed", "applied", "hired", "bounced")


def _rates(c: FunnelCounts) -> FunnelRates:
    def r(n: int, d: int) -> float:
        return round(n / d, 4) if d else 0.0

    return FunnelRates(
        delivery_rate=r(c.delivered, c.sent),
        open_rate=r(c.opened, c.delivered or c.sent),
        click_rate=r(c.clicked, c.delivered or c.sent),
        reply_rate=r(c.replied, c.sent),
        unsubscribe_rate=r(c.unsubscribed, c.sent),
        apply_rate=r(c.applied, c.sent),
        hire_rate=r(c.hired, c.sent),
    )


async def _counts(
    db: AsyncSession, where: list[Any], group: Any | None = None
) -> dict[Any, tuple[FunnelCounts, dict[str, int]]]:
    m, ev = CampaignMessage, EngagementEvent
    if group is None:
        status_rows = [
            (None, *r)
            for r in (
                await db.execute(
                    select(m.status, m.suppression_reason, func.count())
                    .where(*where)
                    .group_by(m.status, m.suppression_reason)
                )
            ).all()
        ]
        event_rows = [
            (None, *r)
            for r in (
                await db.execute(
                    select(ev.kind, func.count(func.distinct(ev.campaign_message_id)))
                    .join(m, m.id == ev.campaign_message_id)
                    .where(*where)
                    .group_by(ev.kind)
                )
            ).all()
        ]
    else:
        status_rows = list(
            (
                await db.execute(
                    select(group, m.status, m.suppression_reason, func.count())
                    .where(*where)
                    .group_by(group, m.status, m.suppression_reason)
                )
            ).all()
        )
        event_rows = list(
            (
                await db.execute(
                    select(group, ev.kind, func.count(func.distinct(ev.campaign_message_id)))
                    .join(m, m.id == ev.campaign_message_id)
                    .where(*where)
                    .group_by(group, ev.kind)
                )
            ).all()
        )
    out: dict[Any, tuple[FunnelCounts, dict[str, int]]] = {}

    def slot(k: Any) -> tuple[FunnelCounts, dict[str, int]]:
        if k not in out:
            out[k] = (FunnelCounts(), {})
        return out[k]

    for k, status, reason, n in status_rows:
        counts, reasons = slot(k)
        counts.audience += int(n)
        if status in SENT_STATUSES:
            counts.sent += int(n)
        if status == "suppressed":
            counts.suppressed += int(n)
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + int(n)
    for k, kind, n in event_rows:
        counts, _ = slot(k)
        if kind in EVENT_KINDS:
            setattr(counts, kind, getattr(counts, kind) + int(n))
    return out


def _funnel(counts: FunnelCounts, reasons: dict[str, int]) -> FunnelOut:
    return FunnelOut(counts=counts, rates=_rates(counts), suppressed_by_reason=reasons)


async def campaign_funnel(db: AsyncSession, campaign_id: uuid.UUID) -> FunnelOut:
    c = await db.get(Campaign, campaign_id)
    if c is None:
        raise not_found("Campaign")
    where = [CampaignMessage.campaign_id == campaign_id]
    total = (await _counts(db, where)).get(None, (FunnelCounts(), {}))
    out = _funnel(*total)
    if c.ab_test:
        by_variant = await _counts(db, where, CampaignMessage.variant_key)
        out.variants = [
            FunnelSlice(key=str(k), label=f"Variant {k}", counts=v[0], rates=_rates(v[0]))
            for k, v in sorted(by_variant.items(), key=lambda kv: str(kv[0]))
            if k
        ]
    return out


async def sequence_funnel(db: AsyncSession, sequence_id: uuid.UUID) -> FunnelOut:
    enrollment_ids = select(Enrollment.id).where(Enrollment.sequence_id == sequence_id).scalar_subquery()
    where = [CampaignMessage.enrollment_id.in_(enrollment_ids)]
    total = (await _counts(db, where)).get(None, (FunnelCounts(), {}))
    out = _funnel(*total)
    by_step = await _counts(db, where, CampaignMessage.step_id)
    steps = {
        s.id: s
        for s in (
            await db.execute(select(SequenceStep).where(SequenceStep.sequence_id == sequence_id))
        ).scalars()
    }
    merged: dict[int, tuple[FunnelCounts, str]] = {}
    for step_id, (counts, _) in by_step.items():
        step = steps.get(step_id)
        if step is None:
            continue
        current, label = merged.get(step.position, (FunnelCounts(), ""))
        for field in FunnelCounts.model_fields:
            setattr(current, field, getattr(current, field) + getattr(counts, field))
        merged[step.position] = (
            current,
            label or f"Step {step.position} ({(step.config or {}).get('channel', step.kind)})",
        )
    # key = the 1-based step position (summed across published versions)
    out.steps = [
        FunnelSlice(key=str(pos), label=label, counts=c, rates=_rates(c))
        for pos, (c, label) in sorted(merged.items())
    ]
    return out


async def overview(db: AsyncSession, start: datetime | None, end: datetime | None) -> FunnelOut:
    where: list[Any] = []
    if start is not None:
        where.append(CampaignMessage.created_at >= start)
    if end is not None:
        where.append(CampaignMessage.created_at < end)
    total = (await _counts(db, where)).get(None, (FunnelCounts(), {}))
    return _funnel(*total)
