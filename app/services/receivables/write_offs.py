# ruff: file-ignore[line-too-long]
"""Write-offs (FR-158): mutable requests on approval subject ``write_off``. Execution runs in the approval callback
transaction: it locks the document and writes the immutable ``write_offs`` fact only while the requested amount
still equals the open balance; otherwise the request lapses with "balance changed". ``written_off`` is derived.
"""

import uuid
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.receivables import InvoiceWrittenOff
from app.models.receivables import WriteOff, WriteOffRequest, WriteOffReversal
from app.schemas.receivables import ReverseIn, WriteOffIn, WriteOffRequestOut
from app.services.receivables import settlement
from app.services.receivables.common import (
    assert_open,
    conflict,
    document,
    is_closed,
    late_date,
    q2,
    target_cols,
    target_of,
    tenant_of,
    today,
    user_id,
)

SUBJECT = "write_off"


def out(r: WriteOffRequest) -> WriteOffRequestOut:
    kind, doc_id = target_of(r)
    return WriteOffRequestOut(
        id=r.id, legal_entity_id=r.legal_entity_id, client_id=r.client_id, document_kind=kind, document_id=doc_id, currency=r.currency,
        requested_amount=r.requested_amount, reason=r.reason, status=r.status, status_reason=r.status_reason,
        approval_request_id=r.approval_request_id, write_off_id=r.write_off_id, created_at=r.created_at,
    )  # fmt: skip


async def _request(db: AsyncSession, request_id: uuid.UUID, *, lock: bool = False) -> WriteOffRequest:
    stmt = select(WriteOffRequest).where(WriteOffRequest.id == request_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Write-off request not found")
    return row


async def request(ctx: RequestContext, db: AsyncSession, data: WriteOffIn) -> WriteOffRequestOut:
    from app.services.approvals import service as approvals

    doc = await document(db, data.document_kind, data.document_id)
    bal = (await settlement.balances(db, [doc]))[doc.id]
    if q2(data.amount) > bal.open:
        raise conflict("allocation_exceeds_open", "The write-off exceeds the open balance", open=str(bal.open))
    row = WriteOffRequest(
        id=uuid.uuid4(), tenant_id=tenant_of(db), legal_entity_id=doc.legal_entity_id, client_id=doc.client_id,
        **target_cols(doc.kind, doc.id), currency=doc.currency, requested_amount=q2(data.amount), reason=data.reason,
        status="pending_approval", requested_by=user_id(ctx),
    )  # fmt: skip
    db.add(row)
    await db.flush()
    await db.refresh(row)
    result = await approvals.start(ctx, db, SUBJECT, row.id, {"amount": str(row.requested_amount), "currency": row.currency, "client_id": str(row.client_id)})
    row = await _request(db, row.id)
    if row.approval_request_id is None and result.request_id is not None:
        row.approval_request_id = result.request_id
        await db.flush()
    await db.refresh(row)
    return out(row)


async def execute(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID, on: date | None = None) -> WriteOffRequestOut:
    row = await _request(db, request_id, lock=True)
    if row.status not in ("pending_approval", "approved"):
        return out(row)
    kind, doc_id = target_of(row)
    bal = (await settlement.lock(ctx, db, [(kind, doc_id)]))[doc_id]
    if bal.open != row.requested_amount or bal.doc.status != "issued":
        row.status = "lapsed"
        row.status_reason = "balance changed"
        await db.flush()
        audit.record("write_off.lapsed", "write_off_request", row.id, session=db, open=str(bal.open))
        await db.refresh(row)
        return out(row)
    when, prior = await late_date(db, row.legal_entity_id, on or await today(db))
    inr_exact = q2(bal.doc.inr_document_total - bal.settled_inr)
    inr_simple = q2(row.requested_amount * bal.doc.fx)
    wo = WriteOff(
        id=uuid.uuid4(), tenant_id=row.tenant_id, legal_entity_id=row.legal_entity_id, client_id=row.client_id, request_id=row.id,
        **target_cols(kind, doc_id), amount=row.requested_amount, currency=row.currency, write_off_date=when,
        inr_amount=inr_exact, residual_fx=q2(inr_exact - inr_simple), prior_period=prior,
    )  # fmt: skip
    db.add(wo)
    await db.flush()
    row.status = "executed"
    row.write_off_id = wo.id
    await db.flush()
    await settlement.refresh_cache(db, [doc_id])
    audit.record("write_off.executed", "write_off", wo.id, session=db, amount=str(wo.amount))
    emit(db, InvoiceWrittenOff(tenant_id=row.tenant_id, write_off_id=wo.id, document_kind=kind, document_id=doc_id))
    await db.refresh(row)
    return out(row)


async def on_final_decided(ctx: RequestContext, db: AsyncSession, decision: Any) -> None:
    row = await _request(db, decision.subject_id, lock=True)
    if row.approval_request_id is None and decision.request_id is not None:
        row.approval_request_id = decision.request_id
    if row.status != "pending_approval":
        await db.flush()
        return
    if decision.outcome in ("approved", "auto_approved"):
        row.status = "approved"
        await db.flush()
        await execute(ctx, db, row.id)
    else:
        row.status = "rejected"
        row.status_reason = (decision.comment or "")[:300] or None
        await db.flush()


async def reverse(ctx: RequestContext, db: AsyncSession, write_off_id: uuid.UUID, data: ReverseIn) -> None:
    wo = (await db.execute(select(WriteOff).where(WriteOff.id == write_off_id))).scalar_one_or_none()
    if wo is None:
        raise NotFound("Write-off not found")
    kind, doc_id = target_of(wo)
    await settlement.lock(ctx, db, [(kind, doc_id)])
    if (await db.execute(select(WriteOffReversal.id).where(WriteOffReversal.write_off_id == wo.id))).first():
        raise conflict("already_reversed", "The write-off is already reversed")
    on = data.reversal_date or await today(db)
    await assert_open(db, wo.legal_entity_id, on)
    prior = await is_closed(db, wo.legal_entity_id, wo.write_off_date)
    db.add(WriteOffReversal(id=uuid.uuid4(), tenant_id=wo.tenant_id, write_off_id=wo.id, legal_entity_id=wo.legal_entity_id, reversal_date=on, reason=data.reason, prior_period=prior))
    await db.flush()
    await settlement.refresh_cache(db, [doc_id])


async def list_requests(ctx: RequestContext, db: AsyncSession, status: str | None = None) -> list[WriteOffRequestOut]:  # ruff: ignore[unused-function-argument]
    stmt = select(WriteOffRequest)
    if status:
        stmt = stmt.where(WriteOffRequest.status == status)
    return [out(r) for r in (await db.execute(stmt.order_by(WriteOffRequest.created_at.desc()).limit(500))).scalars()]


async def get_request(ctx: RequestContext, db: AsyncSession, request_id: uuid.UUID) -> WriteOffRequestOut:  # ruff: ignore[unused-function-argument]
    return out(await _request(db, request_id))
