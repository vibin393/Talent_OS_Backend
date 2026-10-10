# ruff: noqa: E501
"""Receivables contract (Epic 30): open balances, aging and DSO read functions.

Epic 13 contract layer (AD-36): ``open_balance`` keeps its FINAL signature (``tests/contracts/signatures.json``).
``aging_query`` / ``dso`` are the facade read functions for Epic 34 (reconciliation note).
"""

import uuid
from datetime import date
from decimal import Decimal
from typing import Literal

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.schemas.receivables import AgingOut


async def open_balance(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    document_kind: Literal["invoice", "debit_note"],
    document_id: uuid.UUID,
) -> Decimal:
    from app.services.receivables import settlement
    from app.services.receivables.common import document

    return await settlement.open_balance(db, await document(db, document_kind, document_id))


async def aging_query(
    ctx: RequestContext,
    db: AsyncSession,
    legal_entity_id: uuid.UUID,
    as_of: date,
    group_by: Literal["entity", "client", "document"],
    client_id: uuid.UUID | None = None,
) -> AgingOut:
    from app.services.receivables import aging

    return await aging.query(ctx, db, legal_entity_id, as_of, group_by, client_id)


async def dso(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, period_start: date, period_end: date) -> Decimal | None:
    from app.services.receivables import aging

    return (await aging.dso(ctx, db, legal_entity_id, period_start, period_end)).dso
