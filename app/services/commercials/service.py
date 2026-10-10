"""Client commercials contract (Epic 16): rate checks, PO consumption, contracts.

Epic 13 contract layer (AD-36): the signatures below are frozen (``tests/contracts/signatures.json``); Epic 16
fills them by delegating to the domain modules.
"""

import uuid
from datetime import date
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.commercials import (
    ContractDraftIn,
    ContractSummary,
    PoBalance,
    RateCheckRequest,
    RateCheckResult,
)
from app.services.commercials import consumption, contracts, purchase_orders, rate_checks


async def validate_rate(ctx: RequestContext, db: AsyncSession, req: RateCheckRequest) -> RateCheckResult:
    return await rate_checks.validate_rate(ctx, db, req)


async def record_po_consumption(
    ctx: RequestContext,
    db: AsyncSession,
    po_id: uuid.UUID,
    source_doc_kind: str,
    source_doc_id: uuid.UUID,
    amount: Decimal,
    hours: Decimal | None,
    currency: str,
) -> PoBalance:
    return await consumption.record_po_consumption(
        ctx, db, po_id, source_doc_kind, source_doc_id, amount, hours, currency
    )


async def po_balance(ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID) -> PoBalance:
    return await purchase_orders.po_balance(ctx, db, po_id)


async def active_contract_for(
    ctx: RequestContext, db: AsyncSession, client_id: uuid.UUID, legal_entity_id: uuid.UUID, on_date: date
) -> ContractSummary | None:
    return await contracts.active_contract_for(ctx, db, client_id, legal_entity_id, on_date)


async def create_contract_draft(
    ctx: RequestContext, db: AsyncSession, data: ContractDraftIn
) -> ContractSummary:
    return await contracts.create_contract_draft(ctx, db, data)
