# ruff: file-ignore[non-imperative-mood]
"""Reads over the generic SLA clocks (Epic 32): per-subject clocks and status counts for dashboards (Epic 34)
and Vendor performance (FR-99). Writes stay in the foundation ``service`` (``start_clock`` / ``stop_clock`` /
``cancel_clock``), which domains call inside their own transactions."""

import uuid
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.sla import SLA_CLOCK_STATUSES, SlaClock
from app.schemas.sla import SlaClockView, SlaSummaryOut


def view(clock: SlaClock) -> SlaClockView:
    return SlaClockView(
        id=clock.id,
        subject_type=clock.subject_type,
        subject_id=clock.subject_id,
        milestone=clock.milestone,
        ref_id=clock.ref_id,
        target_hours=clock.target_hours,
        calendar=clock.calendar,
        at_risk_percent=clock.at_risk_percent,
        started_at=clock.started_at,
        due_at=clock.due_at,
        at_risk_at=clock.at_risk_at,
        completed_at=clock.completed_at,
        breached_at=clock.breached_at,
        status=clock.status,  # type: ignore[arg-type]
    )


async def list_clocks(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID | None = None,
    limit: int = 200,
) -> list[SlaClockView]:
    stmt = select(SlaClock).where(SlaClock.subject_type == subject_type)
    if subject_id is not None:
        stmt = stmt.where(SlaClock.subject_id == subject_id)
    rows = await db.execute(stmt.order_by(SlaClock.started_at.desc(), SlaClock.id).limit(limit))
    return [view(c) for c in rows.scalars()]


async def summary(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    subject_type: str,
    milestone: str | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    vendor_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
) -> SlaSummaryOut:
    """Counts by status of the clocks started in ``[start, end)``. A clock that breached and was later
    completed counts as ``breached`` (its status keeps the breach)."""
    stmt = select(SlaClock.status, func.count()).where(SlaClock.subject_type == subject_type)
    if milestone:
        stmt = stmt.where(SlaClock.milestone == milestone)
    if start is not None:
        stmt = stmt.where(SlaClock.started_at >= start)
    if end is not None:
        stmt = stmt.where(SlaClock.started_at < end)
    if vendor_id is not None:
        stmt = stmt.where(SlaClock.org_vendor_id == vendor_id)
    if client_id is not None:
        stmt = stmt.where(SlaClock.org_client_id == client_id)
    rows = (await db.execute(stmt.group_by(SlaClock.status))).tuples().all()
    counts = dict.fromkeys(SLA_CLOCK_STATUSES, 0)
    for status, n in rows:
        counts[str(status)] = int(n)
    return SlaSummaryOut(
        subject_type=subject_type, milestone=milestone, counts=counts, total=sum(counts.values())
    )
