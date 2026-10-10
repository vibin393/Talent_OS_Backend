# ruff: noqa: E501
"""Early termination (FR-166): a client termination or worker resignation creates the offboarding case with the
Contract's ``notice_period_days`` snapshot, ``notice_shortfall_days`` (recorded only; the commercial consequence is
a PRD open item) and ``early_attrition``."""

import uuid

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ValidationFailed
from app.models.placements_exit import OffboardingCase
from app.schemas.placements_exit import EarlyTerminationIn
from app.services.placements import offboarding

REASONS = {"client": "client_termination", "worker": "worker_resignation"}


async def notice_period_days(
    ctx: RequestContext, db: AsyncSession, contract_id: uuid.UUID | None
) -> int | None:
    if contract_id is None:
        return None
    from app.services import commercials_service

    try:
        return (await commercials_service.get_contract(ctx, db, contract_id)).notice_period_days
    except Exception:
        logger.exception("contract read failed for {}", contract_id)
        return None


async def terminate(
    ctx: RequestContext, db: AsyncSession, placement_id: uuid.UUID, data: EarlyTerminationIn
) -> OffboardingCase:
    from app.services.placements.lists import get_or_404

    if data.requested_lwd < data.notice_date:
        raise ValidationFailed(
            "The last working day is before the notice date", code="last_working_day_invalid"
        )
    placement = await get_or_404(ctx, db, placement_id)
    return await offboarding.initiate(
        ctx,
        db,
        placement.id,
        REASONS[data.initiated_by],
        data.requested_lwd,
        notice_date=data.notice_date,
        requested_lwd=data.requested_lwd,
        reason_note=data.reason,
        template_id=data.template_id,
        early_termination_by=data.initiated_by,
        notice_period_days=await notice_period_days(ctx, db, placement.contract_id),
    )
