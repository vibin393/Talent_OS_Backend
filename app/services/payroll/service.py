"""Payroll contract (Epic 28): pay periods.

Epic 13 contract layer (AD-36): ``pay_period_for`` keeps its FINAL signature
(``tests/contracts/signatures.json``);
Epic 28 fills it. The export API lives in ``pay_periods`` / ``exports`` / ``mappings`` / ``files`` /
``connector``.
"""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.payroll import PayPeriodRef


async def pay_period_for(
    ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, on_date: date
) -> PayPeriodRef | None:
    """Return the pay period of ``legal_entity_id`` covering ``on_date`` (``None`` when none is generated)."""
    from app.services.payroll import pay_periods

    return await pay_periods.pay_period_for(ctx, db, legal_entity_id, on_date)
