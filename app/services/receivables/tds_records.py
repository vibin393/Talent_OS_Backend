# ruff: noqa: E501
"""TDS / GST-TDS facts (FR-154): sections (platform seed + tenant copies), certificate tracking, reversals and the
TDS receivable report (outstanding certificates by client and quarter, GST-TDS separately)."""

import uuid
from decimal import Decimal

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.receivables import TdsDeduction, TdsDeductionReversal, TdsSection
from app.schemas.receivables import (
    CertificateIn,
    ReverseIn,
    TdsOut,
    TdsReceivableRow,
    TdsSectionIn,
    TdsSectionOut,
)
from app.services.receivables import settlement
from app.services.receivables.common import assert_open, conflict, is_closed, target_of, tenant_of, today


async def list_sections(ctx: RequestContext, db: AsyncSession) -> list[TdsSectionOut]:  # noqa: ARG001
    stmt = select(TdsSection).where(or_(TdsSection.tenant_id.is_(None), TdsSection.tenant_id == tenant_of(db)))
    return [TdsSectionOut.model_validate(r) for r in (await db.execute(stmt.order_by(TdsSection.kind, TdsSection.code))).scalars()]


async def create_section(ctx: RequestContext, db: AsyncSession, data: TdsSectionIn) -> TdsSectionOut:  # noqa: ARG001
    row = TdsSection(id=uuid.uuid4(), tenant_id=tenant_of(db), **data.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return TdsSectionOut.model_validate(row)


async def _get(db: AsyncSession, tds_id: uuid.UUID) -> TdsDeduction:
    row = (await db.execute(select(TdsDeduction).where(TdsDeduction.id == tds_id))).scalar_one_or_none()
    if row is None:
        raise NotFound("TDS deduction not found")
    return row


async def update_certificate(ctx: RequestContext, db: AsyncSession, tds_id: uuid.UUID, data: CertificateIn) -> TdsOut:  # noqa: ARG001
    from app.services.receivables.payments import tds_out

    row = await _get(db, tds_id)
    row.certificate_status = data.certificate_status
    row.certificate_ref = data.certificate_ref
    row.certificate_received_at = data.certificate_received_at
    await db.flush()
    audit.record("tds.certificate_updated", "tds_deduction", row.id, session=db, status=data.certificate_status)
    return tds_out(row)


async def reverse(ctx: RequestContext, db: AsyncSession, tds_id: uuid.UUID, data: ReverseIn) -> TdsOut:
    from app.services.receivables.payments import tds_out

    row = await _get(db, tds_id)
    kind, doc_id = target_of(row)
    await settlement.lock(ctx, db, [(kind, doc_id)])
    if (await db.execute(select(TdsDeductionReversal.id).where(TdsDeductionReversal.tds_deduction_id == row.id))).first():
        raise conflict("already_reversed", "The TDS deduction is already reversed")
    on = data.reversal_date or await today(db)
    await assert_open(db, row.legal_entity_id, on)
    prior = await is_closed(db, row.legal_entity_id, row.deduction_date)
    db.add(TdsDeductionReversal(id=uuid.uuid4(), tenant_id=row.tenant_id, tds_deduction_id=row.id, legal_entity_id=row.legal_entity_id, reversal_date=on, reason=data.reason, prior_period=prior))
    await db.flush()
    await settlement.refresh_cache(db, [doc_id])
    return tds_out(row, reversed_=True)


async def list_deductions(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    quarter: str | None = None,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
    kind: str | None = None,
) -> list[TdsOut]:
    from app.services.receivables.payments import tds_out

    stmt = select(TdsDeduction, TdsDeductionReversal.id).outerjoin(TdsDeductionReversal, TdsDeductionReversal.tds_deduction_id == TdsDeduction.id)
    if quarter:
        stmt = stmt.where(TdsDeduction.quarter == quarter)
    if client_id:
        stmt = stmt.where(TdsDeduction.client_id == client_id)
    if status:
        stmt = stmt.where(TdsDeduction.certificate_status == status)
    if kind:
        stmt = stmt.where(TdsDeduction.kind == kind)
    rows = (await db.execute(stmt.order_by(TdsDeduction.deduction_date.desc()).limit(1000))).all()
    return [tds_out(t, None, r is not None) for t, r in rows]


async def receivable(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    *,
    quarter: str | None = None,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
) -> list[TdsReceivableRow]:
    reversed_ids = select(TdsDeductionReversal.tds_deduction_id)
    t = TdsDeduction
    stmt = select(t.client_id, t.quarter, t.kind, t.certificate_status, t.currency, func.count(), func.sum(t.amount)).where(t.id.not_in(reversed_ids))
    if quarter:
        stmt = stmt.where(t.quarter == quarter)
    if client_id:
        stmt = stmt.where(t.client_id == client_id)
    stmt = stmt.where(t.certificate_status == status) if status else stmt.where(t.certificate_status != "received")
    rows = (await db.execute(stmt.group_by(t.client_id, t.quarter, t.kind, t.certificate_status, t.currency))).all()
    return [
        TdsReceivableRow(client_id=r[0], quarter=r[1], kind=r[2], certificate_status=r[3], currency=r[4], count=int(r[5]), amount=Decimal(r[6]))
        for r in rows
    ]
