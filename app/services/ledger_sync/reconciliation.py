# ruff: file-ignore[line-too-long]
"""Reconciliation (Epic 71, story 71.6, spec A9): per Legal Entity and date range, each issued (or ledger-linked)
invoice with its Talent OS amount, outstanding and status against the ledger amount, balance and status
(``document_status``, read once per request). Flags: ``amount_differs``, ``status_differs``, ``only_in_talent_os``
(issued, never reached the ledger) and ``only_in_ledger`` (linked in the ledger while no longer issued in Talent OS).
TallyPrime does not report back (D-5): the ledger columns stay empty."""

import csv
import io
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.integrations import ManualActionRequired
from app.models.billing import Invoice
from app.models.integrations import ConnectorLink
from app.models.receivables import InvoiceBalance
from app.schemas.ledger_sync import ReconciliationOut, ReconciliationRowOut
from app.services import integrations_service
from app.services.ledger_sync.common import category_installation

__all__ = ["export_csv", "reconcile"]

_LEDGER_STATUS = {"paid": "paid", "partially_paid": "partially_paid", "sent": "unpaid", "overdue": "unpaid", "unpaid": "unpaid", "void": "void", "draft": "draft"}


def _dec(v: Any) -> Decimal | None:
    try:
        return Decimal(str(v)) if v not in (None, "") else None
    except InvalidOperation:
        return None


async def reconcile(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, from_date: date | None, to_date: date | None, flag: str | None = None) -> ReconciliationOut:
    ei = await category_installation(db, "accounting", legal_entity_id)
    if ei is None:
        raise NotFound("No accounting connector serves this legal entity")
    stmt = select(Invoice).where(Invoice.legal_entity_id == legal_entity_id, Invoice.status.in_(("issued", "cancelled")), Invoice.number.is_not(None))
    if from_date:
        stmt = stmt.where(Invoice.doc_date >= from_date)
    if to_date:
        stmt = stmt.where(Invoice.doc_date <= to_date)
    invoices = list((await db.execute(stmt.order_by(Invoice.doc_date, Invoice.number))).scalars())
    ids = [i.id for i in invoices]
    links = {
        r.local_id: r.external_id
        for r in (await db.execute(select(ConnectorLink).where(ConnectorLink.installation_id == ei.installation_id, ConnectorLink.entity_type == "invoice", ConnectorLink.local_id.in_(ids)))).scalars()
    } if ids else {}  # fmt: skip
    balances = {b.receivable_id: b for b in (await db.execute(select(InvoiceBalance).where(InvoiceBalance.receivable_id.in_(ids)))).scalars()} if ids else {}
    ledger: dict[str, tuple[str | None, Decimal | None, Decimal | None]] = {}
    available = True
    if links:
        adapter = await integrations_service.adapter_for(ctx, db, ei.installation_id, config_id=ei.config_id)
        try:
            for res in await adapter.document_status(sorted(set(links.values()))):
                parts = (res.message or "").split("|") if res.status == "synced" else []
                status = _LEDGER_STATUS.get(parts[0], parts[0]) if parts and parts[0] else ("unknown" if res.status == "synced" else "missing")
                ledger[str(res.external_ref)] = (status, _dec(parts[1]) if len(parts) > 1 else None, _dec(parts[2]) if len(parts) > 2 else None)
        except (ManualActionRequired, NotImplementedError):
            available = False
    rows: list[ReconciliationRowOut] = []
    for inv in invoices:
        ref = links.get(inv.id)
        bal = balances.get(inv.id)
        ours_status = "cancelled" if inv.status == "cancelled" else (bal.payment_status if bal is not None else "unpaid")
        outstanding = Decimal(str(bal.open_amount)) if bal is not None else Decimal(str(inv.document_total))
        flags: list[str] = []
        l_status, l_amount, l_balance = ledger.get(ref, (None, None, None)) if ref else (None, None, None)
        if ref is None:
            if inv.status == "issued":
                flags.append("only_in_talent_os")
        else:
            if inv.status == "cancelled" and l_status not in (None, "void", "unknown"):
                flags.append("only_in_ledger")
            if l_amount is not None and l_amount != Decimal(str(inv.document_total)):
                flags.append("amount_differs")
            mapped = "void" if ours_status == "cancelled" else ours_status
            if l_status not in (None, "unknown", "missing") and l_status != mapped:
                flags.append("status_differs")
        if flag and flag not in flags:
            continue
        diff = (Decimal(str(inv.document_total)) - l_amount) if l_amount is not None else None
        rows.append(
            ReconciliationRowOut(
                invoice_id=inv.id, invoice_number=str(inv.number), talent_os_amount=Decimal(str(inv.document_total)), talent_os_outstanding=outstanding,
                talent_os_status=ours_status, ledger_ref=ref, ledger_amount=l_amount, ledger_balance=l_balance, ledger_status=l_status, difference=diff, flags=flags,
            )
        )  # fmt: skip
    return ReconciliationOut(legal_entity_id=legal_entity_id, from_date=from_date, to_date=to_date, rows=rows, ledger_available=available)


async def export_csv(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, from_date: date | None, to_date: date | None, flag: str | None = None) -> bytes:
    out = await reconcile(ctx, db, legal_entity_id, from_date, to_date, flag)
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\r\n")
    w.writerow(["Invoice", "Talent OS amount", "Talent OS outstanding", "Talent OS status", "Ledger amount", "Ledger balance", "Ledger status", "Difference", "Flags"])
    for r in out.rows:
        w.writerow([r.invoice_number, r.talent_os_amount, r.talent_os_outstanding, r.talent_os_status, r.ledger_amount or "", r.ledger_balance or "", r.ledger_status or "", r.difference or "", " ".join(r.flags)])
    audit.record("ledger.reconciliation_exported", "legal_entity", legal_entity_id, session=db, rows=len(out.rows))
    return ("﻿" + buf.getvalue()).encode("utf-8")
