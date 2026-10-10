"""Placements contract (Epic 19): the hub record (AD-25).

Epic 13 contract layer (AD-36): the signatures below are frozen (``tests/contracts/signatures.json``); Epic 19 fills
them. Contexts return only placement-owned facts; Decimals are never floats.
"""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.placements import Placement, RateSegment
from app.schemas.placements import (
    PlacementBillingContext,
    PlacementCreateIn,
    PlacementPayContext,
    PlacementSummary,
    RateSegmentOut,
    WorkerPaymentIdentity,
)
from app.services.placements.common import get_placement


def _summary(p: Placement) -> PlacementSummary:
    return PlacementSummary(
        id=p.id,
        human_id=p.human_id,
        status=p.status,
        candidate_id=p.candidate_id,
        client_id=p.client_id,
        vendor_id=p.vendor_id,
        legal_entity_id=p.legal_entity_id,
        supplier_gst_registration_id=p.supplier_gst_registration_id,
        payee_kind=p.payee_kind,  # type: ignore[arg-type]
        worker_type=(p.worker_type or "employee"),  # type: ignore[arg-type]
        planned_start_date=p.planned_start_date,
        actual_start_date=p.actual_start_date,
        end_date=p.end_date,
        currency=p.currency,
    )


def _segment_out(s: RateSegment) -> RateSegmentOut:
    return RateSegmentOut(
        id=s.id,
        effective_from=s.effective_from,
        effective_to=s.effective_to,
        rate_unit=s.rate_unit,  # type: ignore[arg-type]
        currency=s.currency,
        pay_rate=s.pay_rate,
        bill_rate=s.bill_rate,
        vendor_rate=s.vendor_rate,
        burden_pct=s.burden_pct,
        vendor_burden_pct=s.vendor_burden_pct,
    )


async def create_from_offer(ctx: RequestContext, db: AsyncSession, offer_id: uuid.UUID) -> PlacementSummary:
    from app.services.placements import creation

    return _summary(await creation.create_from_offer(ctx, db, offer_id))


async def create_direct(ctx: RequestContext, db: AsyncSession, data: PlacementCreateIn) -> PlacementSummary:
    from app.schemas.placements_api import PlacementCreateBody
    from app.services.placements import creation

    body = PlacementCreateBody(
        **data.model_dump(exclude={"offer_id", "engagement_kind", "worker_type"}),
        engagement_kind=data.engagement_kind or "contract",  # type: ignore[arg-type]
        worker_type=data.worker_type,
    )
    return _summary(await creation.create_direct(ctx, db, body))


async def get_placement_summary(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement_id: uuid.UUID,
) -> PlacementSummary:
    return _summary(await get_placement(db, placement_id))


async def _active_on(db: AsyncSession, placement_id: uuid.UUID, on_date: date) -> RateSegment | None:
    return (
        await db.execute(
            select(RateSegment)
            .where(
                RateSegment.placement_id == placement_id,
                RateSegment.status == "active",
                RateSegment.effective_from <= on_date,
                (RateSegment.effective_to.is_(None)) | (RateSegment.effective_to >= on_date),
            )
            .order_by(RateSegment.effective_from.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


async def billing_context(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement_id: uuid.UUID,
    on_date: date,
) -> PlacementBillingContext:
    p = await get_placement(db, placement_id)
    seg = await _active_on(db, placement_id, on_date)
    if seg is None:
        raise NotFound("No active rate segment on that date", code="rate_segment_not_found")
    return PlacementBillingContext(
        placement_id=p.id,
        client_id=p.client_id,
        legal_entity_id=p.legal_entity_id,
        supplier_gst_registration_id=p.supplier_gst_registration_id,
        contract_id=p.contract_id,
        po_ids=[p.po_id] if p.po_id else [],
        currency=p.currency,
        rate_segment_id=seg.id,
        rate_unit=seg.rate_unit,  # type: ignore[arg-type]
        bill_rate=seg.bill_rate,
        work_state_code=p.work_state_code,
        actual_start_date=p.actual_start_date,
        end_date=p.end_date,
    )


async def pay_context(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement_id: uuid.UUID,
    on_date: date,
) -> PlacementPayContext:
    p = await get_placement(db, placement_id)
    seg = await _active_on(db, placement_id, on_date)
    if seg is None:
        raise NotFound("No active rate segment on that date", code="rate_segment_not_found")
    return PlacementPayContext(
        placement_id=p.id,
        candidate_id=p.candidate_id,
        legal_entity_id=p.legal_entity_id,
        payee_kind=p.payee_kind,  # type: ignore[arg-type]
        vendor_id=p.vendor_id,
        worker_type=(p.worker_type or "employee"),  # type: ignore[arg-type]
        currency=p.currency,
        rate_segment_id=seg.id,
        rate_unit=seg.rate_unit,  # type: ignore[arg-type]
        pay_rate=seg.pay_rate,
        vendor_rate=seg.vendor_rate,
        actual_start_date=p.actual_start_date,
        end_date=p.end_date,
        burden_pct=seg.burden_pct,
        vendor_burden_pct=seg.vendor_burden_pct,
    )


async def rate_segment_on(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement_id: uuid.UUID,
    on_date: date,
) -> RateSegmentOut | None:
    await get_placement(db, placement_id)
    seg = await _active_on(db, placement_id, on_date)
    return _segment_out(seg) if seg is not None else None


async def get_rate_segment(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    rate_segment_id: uuid.UUID,
) -> RateSegmentOut:
    from app.services.placements.rate_segments import get_segment

    return _segment_out(await get_segment(db, rate_segment_id))


async def worker_payment_identity(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, *, unmask: bool = False
) -> WorkerPaymentIdentity:
    from app.services.placements import workers

    return await workers.worker_payment_identity(ctx, db, candidate_id, unmask=unmask)


# ================================================================ Epic 36 contract (AD-39): project links
_CLOSED_STATUSES = frozenset({"ended", "dropped"})


async def link_project(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    placement_id: uuid.UUID,
    *,
    project_id: uuid.UUID | None,
    project_role_id: uuid.UUID | None,
) -> PlacementSummary:
    """Link a placement to a project (and optionally one of its roles) -- the only writer of
    ``placements.project_id`` / ``project_role_id`` (Epic 45 staffing). Both ``None`` unlinks.

    422 ``project_client_mismatch`` (project of another client), 422 ``project_role_mismatch`` (role of another
    project, or a role without a project), 404 unknown project / role, 409 ``placement_closed`` (ended / dropped).
    Audited through the placement's ``AuditedMixin``.
    """
    from app.models.projects import Project, ProjectRole

    placement = await get_placement(db, placement_id, lock=True)
    if placement.status in _CLOSED_STATUSES:
        raise ConflictError("An ended or dropped placement cannot be linked", code="placement_closed")
    if project_id is None and project_role_id is not None:
        raise ValidationFailed(
            "A project role needs its project",
            code="project_role_mismatch",
            details={"fields": [{"field": "project_id", "message": "A project role needs its project"}]},
        )
    if project_id is not None:
        project = await db.get(Project, project_id)
        if project is None:
            raise NotFound("Project not found")
        if project.client_id != placement.client_id:
            raise ValidationFailed(
                "The project belongs to another client",
                code="project_client_mismatch",
                details={"fields": [{"field": "project_id", "message": "The project belongs to another client"}]},
            )
        if project_role_id is not None:
            role = await db.get(ProjectRole, project_role_id)
            if role is None:
                raise NotFound("Project role not found")
            if role.project_id != project.id:
                raise ValidationFailed(
                    "The role belongs to another project",
                    code="project_role_mismatch",
                    details={"fields": [{"field": "project_role_id", "message": "The role belongs to another project"}]},
                )
    if placement.project_id != project_id or placement.project_role_id != project_role_id:
        placement.project_id = project_id
        placement.project_role_id = project_role_id
        await db.flush()
    return _summary(placement)
