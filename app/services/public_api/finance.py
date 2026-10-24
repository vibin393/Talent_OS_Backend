# ruff: file-ignore[line-too-long]
"""Public finance reads (Epic 67, spec A4). Money is ``{amount, currency}`` with decimal strings; no PDF bytes, notes
or receipts in v1. Scope predicates: timesheets and expense claims follow their placement (recruiter / account manager
under ``timesheet:read`` / ``expense:read``); invoices, credit notes and payments follow the client owner under
``billing:read`` (``clients_service`` owner scope)."""

import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.permissions import scope_condition
from app.models.billing import CreditNote, Invoice
from app.models.clients import Client
from app.models.expenses import ExpenseClaim, ExpenseLine
from app.models.placements import Placement
from app.models.receivables import Payment
from app.models.time import Timesheet, TimesheetEntry
from app.services.public_api._core import Order, base, ctx_principal, expose, finish_all, money, one, page
from app.services.public_api.delivery import placement_condition


def _by_placement(model: Any, permission: str) -> Any:
    cond = placement_condition(permission)
    stmt = select(model)
    return stmt if cond is None else stmt.where(model.placement_id.in_(select(Placement.id).where(cond)))


# ================================================================ timesheets
async def _build_timesheets(db: AsyncSession, rows: Sequence[Timesheet]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    days: dict[uuid.UUID, dict[str, float]] = {}
    if rows:
        q = select(TimesheetEntry.timesheet_id, TimesheetEntry.entry_date, TimesheetEntry.hours).where(TimesheetEntry.timesheet_id.in_([r.id for r in rows]))
        for tid, day, hours in (await db.execute(q)).all():
            bucket = days.setdefault(tid, {})
            bucket[day.isoformat()] = bucket.get(day.isoformat(), 0.0) + float(hours or 0)
    await expose(principal, "candidate", [r.candidate_id for r in rows if r.candidate_id])
    return finish_all(principal, [
        {**base(t, "timesheet", human_id=t.human_id), "placement_id": t.placement_id, "candidate_id": t.candidate_id,
         "period_start": t.period_start, "period_end": t.period_end, "status": t.status,
         "hours_by_day": [{"date": d, "hours": f"{h:.2f}"} for d, h in sorted(days.get(t.id, {}).items())]}
        for t in rows
    ])  # fmt: skip


async def list_timesheets(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _by_placement(Timesheet, "timesheet:read"), sort_col=Timesheet.updated_at, id_col=Timesheet.id, limit=limit,
                      cursor=cursor, order=order, build=lambda r: _build_timesheets(db, r))  # fmt: skip


async def get_timesheet(db: AsyncSession, timesheet_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_timesheets(db, [await one(db, _by_placement(Timesheet, "timesheet:read").where(Timesheet.id == timesheet_id))]))[0]


# ================================================================ expense claims
async def _build_claims(db: AsyncSession, rows: Sequence[ExpenseClaim]) -> list[dict[str, Any]]:
    _, principal = ctx_principal()
    cats: dict[uuid.UUID, set[str]] = {}
    if rows:
        for cid, cat in (await db.execute(select(ExpenseLine.claim_id, ExpenseLine.category).where(ExpenseLine.claim_id.in_([r.id for r in rows])))).all():
            cats.setdefault(cid, set()).add(cat)
    await expose(principal, "candidate", [r.candidate_id for r in rows if r.candidate_id])
    return finish_all(principal, [
        {**base(c, "expense_claim", human_id=c.human_id), "placement_id": c.placement_id, "candidate_id": c.candidate_id,
         "title": c.title, "status": c.status, "total_claimed": money(c.total_claimed, c.currency),
         "total_approved": money(c.total_approved, c.currency), "categories": sorted(cats.get(c.id, set()))}
        for c in rows
    ])  # fmt: skip


async def list_expense_claims(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _by_placement(ExpenseClaim, "expense:read"), sort_col=ExpenseClaim.updated_at, id_col=ExpenseClaim.id, limit=limit,
                      cursor=cursor, order=order, build=lambda r: _build_claims(db, r))  # fmt: skip


async def get_expense_claim(db: AsyncSession, claim_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_claims(db, [await one(db, _by_placement(ExpenseClaim, "expense:read").where(ExpenseClaim.id == claim_id))]))[0]


# ================================================================ invoices / credit notes / payments
def _by_client(model: Any) -> Any:
    _, principal = ctx_principal()
    cond = scope_condition(principal, "billing:read", Client.owner_id)
    stmt = select(model)
    return stmt if cond is None else stmt.where(model.client_id.in_(select(Client.id).where(cond)))


def _doc(d: Any, obj: str) -> dict[str, Any]:
    return {**base(d, obj, human_id=d.number), "number": d.number, "client_id": d.client_id, "legal_entity_id": d.legal_entity_id,
            "doc_date": d.doc_date, "due_date": d.due_date, "status": d.status, "total": money(d.document_total, d.currency),
            "irn_status": getattr(d, "einvoice_status", None)}  # fmt: skip


async def _build_invoices(rows: Sequence[Invoice]) -> list[dict[str, Any]]:  # NOSONAR
    _, principal = ctx_principal()
    return finish_all(principal, [_doc(d, "invoice") for d in rows])


async def list_invoices(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _by_client(Invoice), sort_col=Invoice.updated_at, id_col=Invoice.id, limit=limit, cursor=cursor, order=order, build=_build_invoices)


async def get_invoice(db: AsyncSession, invoice_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_invoices([await one(db, _by_client(Invoice).where(Invoice.id == invoice_id))]))[0]


async def _build_credit_notes(rows: Sequence[CreditNote]) -> list[dict[str, Any]]:  # NOSONAR
    _, principal = ctx_principal()
    return finish_all(principal, [{**_doc(d, "credit_note"), "original_invoice_id": d.original_invoice_id, "reason": d.reason} for d in rows])


async def list_credit_notes(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _by_client(CreditNote), sort_col=CreditNote.updated_at, id_col=CreditNote.id, limit=limit, cursor=cursor, order=order, build=_build_credit_notes)


async def get_credit_note(db: AsyncSession, note_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_credit_notes([await one(db, _by_client(CreditNote).where(CreditNote.id == note_id))]))[0]


async def _build_payments(rows: Sequence[Payment]) -> list[dict[str, Any]]:  # NOSONAR
    _, principal = ctx_principal()
    return finish_all(principal, [
        {**base(p, "payment", human_id=p.human_id), "client_id": p.client_id, "payment_date": p.payment_date,
         "amount": money(p.amount, p.currency), "mode": p.mode, "reference": p.reference,
         "allocation_status": p.allocation_status, "reversed": p.reversed_at is not None}
        for p in rows
    ])  # fmt: skip


async def list_payments(db: AsyncSession, *, limit: int, cursor: str | None, order: Order) -> dict[str, Any]:
    return await page(db, _by_client(Payment), sort_col=Payment.updated_at, id_col=Payment.id, limit=limit, cursor=cursor, order=order, build=_build_payments)


async def get_payment(db: AsyncSession, payment_id: uuid.UUID) -> dict[str, Any]:
    return (await _build_payments([await one(db, _by_client(Payment).where(Payment.id == payment_id))]))[0]


__all__ = [
    "get_credit_note", "get_expense_claim", "get_invoice", "get_payment", "get_timesheet", "list_credit_notes",
    "list_expense_claims", "list_invoices", "list_payments", "list_timesheets",
]  # fmt: skip
