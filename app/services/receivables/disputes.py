# ruff: noqa: E501
"""Disputes (FR-157): operational, audited. The open disputed amount shows separately in aging and is excluded
from auto-allocation and reminders. Resolution by Credit Note links the CN (raised in billing)."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.events.receivables import DisputeStatusChanged
from app.models.receivables import Dispute
from app.schemas.receivables import DisputeIn, DisputeOut, DisputeResolveIn
from app.services.receivables import settlement
from app.services.receivables.common import (
    conflict,
    document,
    invalid,
    now,
    q2,
    target_cols,
    target_of,
    tenant_of,
    user_id,
)


def out(d: Dispute) -> DisputeOut:
    kind, doc_id = target_of(d)
    end = d.resolved_at or now()
    return DisputeOut(
        id=d.id, legal_entity_id=d.legal_entity_id, client_id=d.client_id, document_kind=kind, document_id=doc_id, amount=d.amount,
        currency=d.currency, reason=d.reason, owner_id=d.owner_id, status=d.status, resolution_note=d.resolution_note,
        linked_credit_note_id=d.linked_credit_note_id, opened_at=d.opened_at, resolved_at=d.resolved_at,
        age_days=max(0, (end - d.opened_at).days),
    )  # fmt: skip


async def open_dispute(ctx: RequestContext, db: AsyncSession, data: DisputeIn) -> DisputeOut:
    doc = await document(db, data.document_kind, data.document_id)
    bal = (await settlement.balances(db, [doc]))[doc.id]
    if q2(data.amount) > bal.open - bal.disputed:
        raise invalid("dispute_exceeds_open", "The disputed amount exceeds the undisputed open balance", open=str(bal.open - bal.disputed))
    d = Dispute(
        id=uuid.uuid4(), tenant_id=tenant_of(db), legal_entity_id=doc.legal_entity_id, client_id=doc.client_id, **target_cols(doc.kind, doc.id),
        line_ids=list(data.line_ids), amount=q2(data.amount), currency=doc.currency, reason=data.reason,
        owner_id=data.owner_id or user_id(ctx), status="open",
    )  # fmt: skip
    db.add(d)
    await db.flush()
    await db.refresh(d)
    await settlement.refresh_cache(db, [doc.id])
    emit(db, DisputeStatusChanged(tenant_id=d.tenant_id, dispute_id=d.id, invoice_id=doc.id, from_status="none", to_status="open"))
    return out(d)


async def resolve(ctx: RequestContext, db: AsyncSession, dispute_id: uuid.UUID, data: DisputeResolveIn) -> DisputeOut:  # noqa: ARG001
    d = (await db.execute(select(Dispute).where(Dispute.id == dispute_id).with_for_update())).scalar_one_or_none()
    if d is None:
        raise NotFound("Dispute not found")
    if d.status != "open":
        raise conflict("dispute_state", "The dispute is not open", status=d.status)
    if data.status == "resolved_credit_note" and data.linked_credit_note_id is None:
        raise invalid("credit_note_required", "Link the credit note that resolves the dispute")
    previous = d.status
    d.status = data.status
    d.resolution_note = data.resolution_note
    d.linked_credit_note_id = data.linked_credit_note_id
    d.resolved_at = now()
    await db.flush()
    await db.refresh(d)
    _, doc_id = target_of(d)
    await settlement.refresh_cache(db, [doc_id])
    emit(db, DisputeStatusChanged(tenant_id=d.tenant_id, dispute_id=d.id, invoice_id=doc_id, from_status=previous, to_status=d.status))
    return out(d)


async def list_disputes(ctx: RequestContext, db: AsyncSession, *, client_id: uuid.UUID | None = None, status: str | None = None) -> list[DisputeOut]:  # noqa: ARG001
    stmt = select(Dispute)
    if client_id:
        stmt = stmt.where(Dispute.client_id == client_id)
    if status:
        stmt = stmt.where(Dispute.status == status)
    return [out(d) for d in (await db.execute(stmt.order_by(Dispute.opened_at.desc()).limit(500))).scalars()]
