"""Generic SLA clocks (Epic 13, AD-31). The evaluation beat, notifications and the migration of the demand SLA
onto these clocks are Epic 32; the Phase 1 demand SLA (``services/demands/sla.py``) is untouched.

* ``start_clock`` picks the most specific active ``sla_targets`` row for (subject type, milestone): every key of
  the row's ``scope`` must equal the clock's scope; most matching keys win, then the latest row. No target ->
  ``None``. A running clock for the same (subject, milestone, ref) is returned unchanged (idempotent).
* ``due_at`` / ``at_risk_at`` use ``core.business_hours`` with the tenant calendar for ``business`` targets.
"""

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import business_hours
from app.core.business_hours import Calendar
from app.core.context import RequestContext
from app.core.events import emit
from app.events.sla import SlaClockCompleted, SlaClockStarted
from app.models.sla import SlaClock, SlaTarget
from app.schemas.sla import SlaClockOut
from app.services.sla.registry import milestone as milestone_def


def _now() -> datetime:
    return datetime.now(UTC)


def _tenant(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "SLA clocks need a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def _scope_value(value: Any) -> str:
    return str(value)


async def _target(
    db: AsyncSession, subject_type: str, milestone: str, scope: Mapping[str, Any]
) -> SlaTarget | None:
    rows = list(
        (
            await db.execute(
                select(SlaTarget).where(
                    SlaTarget.subject_type == subject_type,
                    SlaTarget.milestone == milestone,
                    SlaTarget.is_active.is_(True),
                )
            )
        ).scalars()
    )
    given = {k: _scope_value(v) for k, v in scope.items() if v is not None}
    matching = [
        r for r in rows if all(given.get(k) == _scope_value(v) for k, v in (r.scope or {}).items())
    ]
    if not matching:
        return None
    return max(matching, key=lambda r: (len(r.scope or {}), r.created_at, str(r.id)))


def _hours_after(start: datetime, hours: Decimal, calendar: str, cal: Calendar, tz: str) -> datetime:
    if calendar == "business":
        return business_hours.add_business_hours(start, float(hours), cal, tz)
    return business_hours.add_calendar_hours(start, float(hours))


def _running(subject_type: str, subject_id: uuid.UUID, milestone: str, ref_id: uuid.UUID | None) -> Any:
    stmt = select(SlaClock).where(
        SlaClock.subject_type == subject_type,
        SlaClock.subject_id == subject_id,
        SlaClock.milestone == milestone,
        SlaClock.completed_at.is_(None),
    )
    return stmt.where(SlaClock.ref_id.is_(None) if ref_id is None else SlaClock.ref_id == ref_id)


def _org_id(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except ValueError:
        return None


def _completed(db: AsyncSession, clock: SlaClock) -> None:
    emit(
        db,
        SlaClockCompleted(
            tenant_id=clock.tenant_id,
            clock_id=clock.id,
            subject_type=clock.subject_type,
            subject_id=clock.subject_id,
            milestone=clock.milestone,
            status=clock.status,
        ),
    )


async def start_clock(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID,
    milestone: str,
    *,
    ref_id: uuid.UUID | None = None,
    started_at: datetime | None = None,
    scope: Mapping[str, Any] | None = None,
) -> SlaClockOut | None:
    if milestone_def(subject_type, milestone) is None:
        msg = f"unregistered SLA milestone {subject_type}.{milestone}"
        raise ValueError(msg)
    existing = (await db.execute(_running(subject_type, subject_id, milestone, ref_id))).scalar_one_or_none()
    if existing is not None:
        return SlaClockOut.model_validate(existing)
    target = await _target(db, subject_type, milestone, scope or {})
    if target is None:
        return None
    start = (started_at or _now()).astimezone(UTC)
    cal, tz = await business_hours.tenant_calendar(db)
    hours = Decimal(target.target_hours)
    at_risk_hours = hours * Decimal(target.at_risk_percent) / Decimal(100)
    clock = SlaClock(
        id=uuid.uuid4(),
        tenant_id=_tenant(db),
        subject_type=subject_type,
        subject_id=subject_id,
        milestone=milestone,
        ref_id=ref_id,
        target_id=target.id,
        target_hours=hours,
        calendar=target.calendar,
        at_risk_percent=target.at_risk_percent,
        started_at=start,
        due_at=_hours_after(start, hours, target.calendar, cal, tz),
        at_risk_at=_hours_after(start, at_risk_hours, target.calendar, cal, tz),
        # Epic 32: org columns for portal visibility (org RLS) and the evaluator's initial status.
        org_client_id=_org_id((scope or {}).get("client_id")),
        org_vendor_id=_org_id((scope or {}).get("vendor_id")),
        status="healthy",
    )
    try:
        async with db.begin_nested():
            db.add(clock)
            await db.flush()
    except IntegrityError:
        # A concurrent start won the race: return the running clock.
        winner = (await db.execute(_running(subject_type, subject_id, milestone, ref_id))).scalar_one()
        return SlaClockOut.model_validate(winner)
    emit(
        db,
        SlaClockStarted(
            tenant_id=clock.tenant_id,
            clock_id=clock.id,
            subject_type=subject_type,
            subject_id=subject_id,
            milestone=milestone,
        ),
    )
    return SlaClockOut.model_validate(clock)


async def stop_clock(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID,
    milestone: str,
    *,
    ref_id: uuid.UUID | None = None,
    at: datetime | None = None,
) -> None:
    clock = (await db.execute(_running(subject_type, subject_id, milestone, ref_id).with_for_update())).scalar_one_or_none()
    if clock is None:
        return
    when = (at or _now()).astimezone(UTC)
    clock.completed_at = when
    clock.outcome = "met" if when <= clock.due_at else "breached"
    # Epic 32: a breach is never erased (the evaluator may have recorded it before the stop).
    if clock.outcome == "breached" or clock.breached_at is not None:
        clock.status, clock.breached_at = "breached", clock.breached_at or clock.due_at
    else:
        clock.status = "met"
    await db.flush()
    _completed(db, clock)


async def cancel_clock(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject_type: str,
    subject_id: uuid.UUID,
    milestone: str,
    *,
    ref_id: uuid.UUID | None = None,
) -> None:
    clock = (await db.execute(_running(subject_type, subject_id, milestone, ref_id).with_for_update())).scalar_one_or_none()
    if clock is None:
        return
    clock.completed_at = _now()
    clock.outcome = "cancelled"
    # Epic 32: a clock cancelled after it breached keeps ``status='breached'`` (dashboards count it).
    if clock.breached_at is None and clock.completed_at >= clock.due_at:
        clock.breached_at = clock.due_at
    clock.status = "breached" if clock.breached_at is not None else "cancelled"
    await db.flush()
    _completed(db, clock)


async def clocks_for(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    subject_type: str,
    subject_ids: Sequence[uuid.UUID],
) -> dict[uuid.UUID, list[SlaClockOut]]:
    out: dict[uuid.UUID, list[SlaClockOut]] = {sid: [] for sid in subject_ids}
    if not subject_ids:
        return out
    rows = await db.execute(
        select(SlaClock)
        .where(SlaClock.subject_type == subject_type, SlaClock.subject_id.in_(list(subject_ids)))
        .order_by(SlaClock.started_at, SlaClock.id)
    )
    for clock in rows.scalars():
        out.setdefault(clock.subject_id, []).append(SlaClockOut.model_validate(clock))
    return out


def compute_status(
    clock: SlaClockOut,
    at: datetime,
    calendar: Calendar,  # noqa: ARG001 - due / at-risk instants are computed with the calendar at start
    tz: str,  # noqa: ARG001
) -> Literal["healthy", "at_risk", "breached", "met"]:
    if clock.completed_at is not None:
        return "breached" if clock.outcome == "breached" else "met"
    if at >= clock.due_at:
        return "breached"
    if at >= clock.at_risk_at:
        return "at_risk"
    return "healthy"
