# ruff: file-ignore[line-too-long]
"""Settlement core (AD-32, FR-153): open balances computed in SQL from dated facts, the settlement lock and the
``invoice_balances`` read cache.

``open = document_total - sum(allocations net of reversals) - sum(TDS net of reversals) - sum(write-offs net of
reversals)``, counting facts dated <= ``as_of``. IRN-cancelled documents are excluded from ``cancelled_at``.
Every settlement write locks the documents with ``billing_service.lock_for_settlement`` in uuid order and
computes the open balances in the same transaction; the cache is never used for guards.
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import Select, and_, func, literal, select, union_all
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.receivables import (
    Allocation,
    AllocationReversal,
    Dispute,
    InvoiceBalance,
    TdsDeduction,
    TdsDeductionReversal,
    WriteOff,
    WriteOffReversal,
)
from app.services.receivables.common import ZERO, DocRow, documents, now, q2, tenant_of

A = Allocation.__table__
AR = AllocationReversal.__table__
T = TdsDeduction.__table__
TR = TdsDeductionReversal.__table__
W = WriteOff.__table__
WR = WriteOffReversal.__table__


def _doc(t: Any) -> Any:
    return func.coalesce(t.c.invoice_id, t.c.debit_note_id)


def facts_union() -> Any:
    """Signed settlement facts: (doc_id, amt, inr, d, kind, ref_id). Reversal rows carry negated values."""
    return union_all(
        select(_doc(A).label("doc_id"), A.c.amount.label("amt"), A.c.inr_at_document.label("inr"), A.c.allocation_date.label("d"), literal("allocation").label("kind"), A.c.id.label("ref_id")),
        select(_doc(A), AR.c.amount, AR.c.inr_at_document, AR.c.reversal_date, literal("allocation_reversal"), AR.c.id).join_from(AR, A, A.c.id == AR.c.allocation_id),
        select(_doc(T), T.c.amount, T.c.inr_amount, T.c.deduction_date, literal("tds"), T.c.id),
        select(_doc(T), -T.c.amount, -T.c.inr_amount, TR.c.reversal_date, literal("tds_reversal"), TR.c.id).join_from(TR, T, T.c.id == TR.c.tds_deduction_id),
        select(_doc(W), W.c.amount, W.c.inr_amount, W.c.write_off_date, literal("write_off"), W.c.id),
        select(_doc(W), -W.c.amount, -W.c.inr_amount, WR.c.reversal_date, literal("write_off_reversal"), WR.c.id).join_from(WR, W, W.c.id == WR.c.write_off_id),
    ).subquery("facts")


async def settled(db: AsyncSession, ids: Iterable[uuid.UUID], as_of: date | None = None) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    """Doc id -> (settled amount, settled INR at document fx)."""
    id_list = list(ids)
    if not id_list:
        return {}
    f = facts_union()
    stmt: Select[Any] = select(f.c.doc_id, func.coalesce(func.sum(f.c.amt), 0), func.coalesce(func.sum(f.c.inr), 0)).where(f.c.doc_id.in_(id_list))
    if as_of is not None:
        stmt = stmt.where(f.c.d <= as_of)
    rows = (await db.execute(stmt.group_by(f.c.doc_id))).all()
    return {r[0]: (Decimal(r[1]), Decimal(r[2])) for r in rows}


async def live_write_off(db: AsyncSession, ids: Iterable[uuid.UUID], as_of: date | None = None) -> set[uuid.UUID]:
    id_list = list(ids)
    if not id_list:
        return set()
    rev = select(WR.c.write_off_id)
    if as_of is not None:
        rev = rev.where(WR.c.reversal_date <= as_of)
    stmt = select(_doc(W)).where(_doc(W).in_(id_list), W.c.id.not_in(rev))
    if as_of is not None:
        stmt = stmt.where(W.c.write_off_date <= as_of)
    return {r[0] for r in (await db.execute(stmt)).all()}


async def disputed(db: AsyncSession, ids: Iterable[uuid.UUID], as_of: date | None = None) -> dict[uuid.UUID, Decimal]:
    id_list = list(ids)
    if not id_list:
        return {}
    doc = func.coalesce(Dispute.invoice_id, Dispute.debit_note_id)
    stmt = select(doc, func.sum(Dispute.amount)).where(doc.in_(id_list))
    if as_of is None:
        stmt = stmt.where(Dispute.status == "open")
    else:
        stmt = stmt.where(
            func.date(Dispute.opened_at) <= as_of,
            (Dispute.status == "open") | and_(Dispute.resolved_at.is_not(None), func.date(Dispute.resolved_at) > as_of),
        )
    return {r[0]: Decimal(r[1]) for r in (await db.execute(stmt.group_by(doc))).all()}


def open_of(doc: DocRow, settled_amount: Decimal, as_of: date | None = None) -> Decimal:
    if doc.cancelled_by(as_of):
        return ZERO
    return q2(doc.document_total - settled_amount)


def payment_status(total: Decimal, open_amount: Decimal, written_off: bool) -> str:
    if open_amount <= 0:
        return "written_off" if written_off else "paid"
    if open_amount >= total:
        return "unpaid"
    return "partially_paid"


@dataclass
class Balance:
    doc: DocRow
    open: Decimal
    settled_inr: Decimal
    disputed: Decimal = ZERO
    written_off: bool = False

    @property
    def status(self) -> str:
        return payment_status(self.doc.document_total, self.open, self.written_off)


async def balances(
    db: AsyncSession, docs: list[DocRow], as_of: date | None = None, *, with_disputes: bool = True
) -> dict[uuid.UUID, Balance]:
    ids = [d.id for d in docs]
    s = await settled(db, ids, as_of)
    wo = await live_write_off(db, ids, as_of)
    dis = await disputed(db, ids, as_of) if with_disputes else {}
    out: dict[uuid.UUID, Balance] = {}
    for d in docs:
        amt, inr = s.get(d.id, (ZERO, ZERO))
        out[d.id] = Balance(d, open_of(d, amt, as_of), q2(inr), dis.get(d.id, ZERO), d.id in wo)
    return out


async def open_balance(db: AsyncSession, doc: DocRow, as_of: date | None = None) -> Decimal:
    return (await balances(db, [doc], as_of, with_disputes=False))[doc.id].open


# ---------------------------------------------------------------- the settlement lock
async def lock(ctx: RequestContext, db: AsyncSession, keys: Iterable[tuple[str, uuid.UUID]]) -> dict[uuid.UUID, Balance]:
    """Lock every document (``lock_for_settlement``, uuid order) and return their open balances from facts."""
    from app.services import billing_service

    uniq = sorted(set(keys), key=lambda k: k[1])
    for kind, doc_id in uniq:
        await billing_service.lock_for_settlement(ctx, db, kind, doc_id)  # type: ignore[arg-type]
    docs = await documents(db, ids=[k[1] for k in uniq])
    return await balances(db, docs)


async def refresh_cache(db: AsyncSession, ids: Iterable[uuid.UUID]) -> None:
    id_list = list(set(ids))
    if not id_list:
        return
    docs = await documents(db, ids=id_list)
    bal = await balances(db, docs)
    tid = tenant_of(db)
    for b in bal.values():
        values = {
            "tenant_id": tid,
            "receivable_id": b.doc.id,
            "receivable_kind": b.doc.kind,
            "legal_entity_id": b.doc.legal_entity_id,
            "client_id": b.doc.client_id,
            "currency": b.doc.currency,
            "document_total": b.doc.document_total,
            "open_amount": b.open,
            "payment_status": "cancelled" if b.doc.cancelled_by(None) else b.status,
            "disputed_amount": b.disputed,
            "due_date": b.doc.due_date,
            "rebuilt_at": now(),
        }
        stmt = pg_insert(InvoiceBalance.__table__).values(**values)
        stmt = stmt.on_conflict_do_update(index_elements=["receivable_id"], set_={k: stmt.excluded[k] for k in values if k not in ("tenant_id", "receivable_id")})
        await db.execute(stmt)


async def rebuild_cache(db: AsyncSession) -> int:
    docs = await documents(db)
    await refresh_cache(db, [d.id for d in docs])
    return len(docs)


async def has_settlements(ctx: RequestContext, db: AsyncSession, document_kind: str, document_id: uuid.UUID) -> bool:  # ruff: ignore[unused-function-argument]
    """Settlement probe registered with billing (IRN-cancel guard): any live allocation, TDS or write-off."""
    f = facts_union()
    total = (await db.execute(select(func.count()).select_from(f).where(f.c.doc_id == document_id))).scalar() or 0
    if not total:
        return False
    s = await settled(db, [document_id])
    return s.get(document_id, (ZERO, ZERO))[0] != 0
