# ruff: noqa: E501
"""Conversion / contract-to-hire (FR-164). ``record`` runs in one transaction: validate the date, snapshot the
Contract's ``conversion_fee_rule``, compute the fee (``conversion_fee``, pure), initiate the offboarding case
(``converted``, never bench-eligible), create the row and emit ``PlacementConverted`` -- billing turns it into a
``conversion_fee`` schedule item. Compensation is masked by the router without ``compensation:view``."""

import uuid
from decimal import Decimal
from typing import Any

from loguru import logger
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx
from app.core.context import RequestContext
from app.core.errors import ConflictError, ValidationFailed
from app.core.events import emit
from app.events.placements import PlacementConverted
from app.models.placements import Placement
from app.models.placements_exit import Conversion
from app.schemas.placements_exit import ConversionIn, ConversionOut, FeePreviewIn, FeePreviewOut
from app.services.placements import conversion_fee, offboarding
from app.services.placements.common import local_today

_NONE: dict[str, Any] = {"kind": "none", "value": None, "waive_after_months": None}


async def fee_rule(ctx: RequestContext, db: AsyncSession, placement: Placement) -> dict[str, Any]:
    if placement.contract_id is None:
        return dict(_NONE)
    from app.services import commercials_service

    try:
        contract = await commercials_service.get_contract(ctx, db, placement.contract_id)
    except Exception:
        logger.exception("contract read failed for placement {}", placement.id)
        return dict(_NONE, unavailable=True)
    return dict(contract.conversion_fee_rule.model_dump(mode="json"), contract_id=str(contract.id))


def _fee(rule: dict[str, Any], placement: Placement, comp: Decimal, on: Any, currency: str) -> Decimal | None:
    months = conversion_fee.months_between(placement.actual_start_date, on) if on else None
    return conversion_fee.compute(rule, comp, currency=currency, months_served=months)


async def preview(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: FeePreviewIn
) -> FeePreviewOut:
    from app.services.placements.lists import visible

    placement = await visible(ctx, db, placement_id)
    rule = await fee_rule(ctx, db, placement)
    return FeePreviewOut(
        fee_rule=rule,
        fee_proposal=_fee(
            rule, placement, data.offered_compensation, data.conversion_date, placement.currency
        ),
        currency=placement.currency,
    )


async def get_for_placement(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID
) -> ConversionOut | None:
    from app.services.placements.lists import visible

    await visible(ctx, db, placement_id)
    row = (
        await db.execute(select(Conversion).where(Conversion.placement_id == placement_id))
    ).scalar_one_or_none()
    return ConversionOut.model_validate(row) if row else None


async def record(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: ConversionIn
) -> ConversionOut:
    from app.services.placements.lists import get_or_404

    actor = actor_from_ctx(ctx)
    placement = await get_or_404(ctx, db, placement_id)
    existing = (
        await db.execute(select(Conversion.id).where(Conversion.placement_id == placement.id))
    ).scalar_one_or_none()
    if existing is not None:
        raise ConflictError("This placement was already converted", code="conversion_exists")
    if data.conversion_date < await local_today(db, placement):
        raise ValidationFailed("The conversion date cannot be in the past", code="conversion_date_invalid")
    currency = data.currency or placement.currency
    rule = await fee_rule(ctx, db, placement)
    fee = _fee(rule, placement, data.offered_compensation, data.conversion_date, currency)
    case = await offboarding.initiate(
        ctx,
        db,
        placement.id,
        "converted",
        data.conversion_date,
        actor=actor,
        bench_eligible_override=False,
        reason_note=data.note,
    )
    row = Conversion(
        id=uuid.uuid4(),
        tenant_id=placement.tenant_id,
        placement_id=placement.id,
        client_id=placement.client_id,
        conversion_date=data.conversion_date,
        offered_compensation=data.offered_compensation,
        currency=currency,
        fee_rule_snapshot=rule,
        fee_proposal=fee,
        offboarding_case_id=case.id,
        recorded_by=actor.id if actor.kind == "user" else None,
        note=data.note,
    )
    db.add(row)
    try:
        async with db.begin_nested():
            await db.flush()
    except IntegrityError as exc:
        raise ConflictError("This placement was already converted", code="conversion_exists") from exc
    emit(
        db,
        PlacementConverted(
            tenant_id=placement.tenant_id,
            placement_id=placement.id,
            conversion_id=row.id,
            client_id=placement.client_id,
            conversion_date=data.conversion_date,
            fee_amount=str(fee) if fee is not None else None,
            currency=currency,
        ),
    )
    await db.refresh(row)
    return ConversionOut.model_validate(row)
