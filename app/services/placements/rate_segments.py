"""Rate Segments (Epic 19 A7, P2-6.4, FR-107).

``add_segment``: margin -> ``commercials_service.validate_rate`` -> below the tenant minimum margin, or outside the
rate card, the segment is ``pending_approval`` and approval ``placement_rate_exception`` starts (its callback
activates the segment in the decision transaction); otherwise it activates immediately.

Activation locks the placement, closes the previous active segment (``effective_to = new.effective_from - 1``),
refuses a segment dated on or before the latest active one's start (409 ``segment_out_of_order``) and emits
``RateSegmentAdded``. Placements never edit Timesheets or Invoices (AD-23). An overlap that slips past the service
hits the EXCLUDE constraint -> 409 ``segment_overlap``.
"""

import uuid
from datetime import timedelta
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import ActorRef, actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.core.events import emit
from app.events.placements import RateSegmentAdded
from app.models.placements import Placement, RateSegment
from app.schemas.placements_api import MarginOut, RateSegmentDetailOut, RateSegmentIn
from app.services.placements import margin
from app.services.placements.common import get_placement, settings_row

EXCEPTION_SUBJECT = "placement_rate_exception"


def out_of_order() -> ConflictError:
    return ConflictError("A later rate segment already exists", code="segment_out_of_order")


def overlap() -> ConflictError:
    return ConflictError("The rate segment overlaps an active segment", code="segment_overlap")


async def list_segments(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
) -> list[RateSegmentDetailOut]:
    rows = (
        await db.execute(
            select(RateSegment)
            .where(RateSegment.placement_id == placement_id)
            .order_by(RateSegment.effective_from, RateSegment.created_at)
        )
    ).scalars()
    return [RateSegmentDetailOut.model_validate(r) for r in rows]


async def preview_margin(db: AsyncSession, tenant_id: uuid.UUID, data: Any) -> MarginOut:
    amount, pct = margin.compute(
        bill_rate=data.bill_rate,
        pay_rate=data.pay_rate,
        burden_pct=data.burden_pct,
        vendor_rate=data.vendor_rate,
        vendor_burden_pct=data.vendor_burden_pct,
        currency=data.currency,
    )
    settings = await settings_row(db, tenant_id)
    below = pct is not None and settings.min_margin_pct is not None and pct < settings.min_margin_pct
    return MarginOut(margin_amount=amount, margin_pct=pct, below_minimum=bool(below))


async def _latest_active(db: AsyncSession, placement_id: uuid.UUID) -> RateSegment | None:
    return (
        await db.execute(
            select(RateSegment)
            .where(RateSegment.placement_id == placement_id, RateSegment.status == "active")
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
            .with_for_update()
        )
    ).scalar_one_or_none()


async def activate(ctx: RequestContext, db: AsyncSession, segment: RateSegment) -> RateSegment:  # ruff: ignore[unused-function-argument]
    placement = await get_placement(db, segment.placement_id, lock=True)
    previous = await _latest_active(db, placement.id)
    superseded: tuple[tuple[uuid.UUID, Any, Any], ...] = ()
    if previous is not None and previous.id != segment.id:
        if previous.effective_from >= segment.effective_from:
            raise out_of_order()
        new_to = segment.effective_from - timedelta(days=1)
        if previous.effective_to is None or previous.effective_to > new_to:
            superseded = ((previous.id, segment.effective_from, previous.effective_to),)
            previous.effective_to = new_to
    try:
        async with db.begin_nested():
            # The EXCLUDE constraint is not deferrable: end-date the previous segment in its own flush BEFORE the new
            # one becomes active (one flush could order the two UPDATEs either way -> spurious overlap).
            await db.flush()
            segment.status = "active"
            await db.flush()
    except DBAPIError as exc:
        if "ex_rate_segments_no_overlap" in str(exc.orig):
            raise overlap() from exc
        raise
    emit(
        db,
        RateSegmentAdded(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            rate_segment_id=segment.id,
            effective_from=segment.effective_from,
            previous_segment_id=previous.id if previous is not None and previous.id != segment.id else None,
            superseded=superseded,
        ),
    )
    return segment


async def add_segment(
    ctx: RequestContext,
    db: AsyncSession,
    placement_id: uuid.UUID,
    data: RateSegmentIn,
    *,
    actor: ActorRef | None = None,
) -> RateSegmentDetailOut:
    from app.services import approvals, commercials_service

    actor = actor or actor_from_ctx(ctx)
    placement = await get_placement(db, placement_id, lock=True)
    if placement.status in ("ended", "dropped"):
        raise ConflictError("Closed placements take no new rates", code="placement_closed")
    if data.pay_rate is None and data.bill_rate is None and data.vendor_rate is None:
        raise ValidationFailed("At least one rate is required", code="rate_required")
    previous = await _latest_active(db, placement.id)
    if previous is not None and previous.effective_from >= data.effective_from:
        raise out_of_order()
    unit = data.rate_unit or (previous.rate_unit if previous else "hour")
    amount, pct = margin.compute(
        bill_rate=data.bill_rate,
        pay_rate=data.pay_rate,
        burden_pct=data.burden_pct,
        vendor_rate=data.vendor_rate,
        vendor_burden_pct=data.vendor_burden_pct,
        currency=placement.currency,
    )
    seg = RateSegment(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        placement_id=placement.id,
        effective_from=data.effective_from,
        rate_unit=unit,
        currency=placement.currency,
        pay_rate=data.pay_rate,
        bill_rate=data.bill_rate,
        vendor_rate=data.vendor_rate,
        burden_pct=data.burden_pct,
        vendor_burden_pct=data.vendor_burden_pct,
        margin_amount=amount,
        margin_pct=pct,
        status="pending_approval",
        created_by=actor.id if actor.kind == "user" else None,
        reason=data.reason,
    )
    needs_exception = False
    if data.bill_rate is not None:
        from app.schemas.commercials import RateCheckRequest

        try:
            async with db.begin_nested():
                result = await commercials_service.validate_rate(
                    ctx,
                    db,
                    RateCheckRequest(
                        subject_kind="rate_segment",
                        subject_id=seg.id,
                        client_id=placement.client_id,
                        contract_id=placement.contract_id,
                        on_date=data.effective_from,
                        role=placement.role_title,
                        location=placement.work_location_text,
                        bill_rate=data.bill_rate,
                        vendor_rate=data.vendor_rate,
                        currency=placement.currency,
                        rate_unit=unit,
                    ),
                )
            seg.rate_check = result.model_dump(mode="json")
            needs_exception = bool(result.requires_exception)
        except NotImplementedError:
            seg.rate_check = None
        except Exception:
            logger.exception("rate check failed for placement {}", placement.id)
    settings = await settings_row(db, placement.tenant_id)
    if pct is not None and settings.min_margin_pct is not None and pct < Decimal(settings.min_margin_pct):
        needs_exception = True
    db.add(seg)
    await db.flush()
    if needs_exception:
        facts = {
            "placement_id": str(placement.id),
            "client_id": str(placement.client_id),
            "margin_pct": str(pct) if pct is not None else None,
        }
        result = await approvals.start(
            ctx, db, EXCEPTION_SUBJECT, seg.id, facts, steps=await _exception_steps(db)
        )
        if result.request_id is not None:
            seg.approval_request_id = result.request_id
            await db.flush()
    else:
        await activate(ctx, db, seg)
    await db.refresh(seg)
    return RateSegmentDetailOut.model_validate(seg)


async def _exception_steps(db: AsyncSession) -> Any:
    """Return the tenant's ``placement_rate_exception`` chain when active, else one Tenant Admin step."""
    from app.models.approvals import ApprovalChain
    from app.services.approvals import StepSpec

    chain = (
        await db.execute(
            select(ApprovalChain.id).where(
                ApprovalChain.subject_type == EXCEPTION_SUBJECT, ApprovalChain.is_active.is_(True)
            )
        )
    ).scalar_one_or_none()
    if chain is not None:
        return None
    return [StepSpec(name="Rate exception", approver_kind="role", approver_ref={"role_key": "tenant_admin"})]


async def on_exception_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    seg = (
        await db.execute(select(RateSegment).where(RateSegment.id == decision.subject_id).with_for_update())
    ).scalar_one_or_none()
    if seg is None or seg.status != "pending_approval":
        return
    if decision.outcome in ("approved", "auto_approved"):
        await activate(ctx, db, seg)
    else:
        seg.status = "rejected"
        await db.flush()


async def describe_exceptions(
    ctx: RequestContext,
    db: AsyncSession,
    ids: list[uuid.UUID],
) -> dict[uuid.UUID, Any]:
    from app.schemas.approvals import SubjectSummary

    rows = (
        await db.execute(
            select(RateSegment, Placement.human_id)
            .join(Placement, Placement.id == RateSegment.placement_id)
            .where(RateSegment.id.in_(ids))
        )
    ).all()
    return {
        s.id: SubjectSummary(
            label="Placement rate exception",
            title=f"{human} from {s.effective_from} (margin {s.margin_pct}%)",
        )
        for s, human in rows
    }


async def get_segment(db: AsyncSession, segment_id: uuid.UUID) -> RateSegment:
    row = (await db.execute(select(RateSegment).where(RateSegment.id == segment_id))).scalar_one_or_none()
    if row is None:
        raise NotFound("Rate segment not found")
    return row
