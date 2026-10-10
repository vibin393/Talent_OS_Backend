# ruff: noqa: E501
"""Client statements (FR-159): opening balance, chronological lines from the dated facts, closing balance.

Balance at ``d`` = sum(document totals issued <= d, not IRN-cancelled by d) - sum(settlement facts <= d) -
unallocated credits at d. Invariant (asserted): closing = sum(open of documents at ``end``) - unallocated credits
at ``end``.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.models.receivables import ClientCredit
from app.schemas.receivables import StatementLine, StatementOut
from app.services.receivables import credits as credit_svc
from app.services.receivables import settlement
from app.services.receivables.common import ZERO, DocRow, documents, q2

LABELS = {
    "allocation": "Payment / credit applied",
    "allocation_reversal": "Allocation reversed",
    "tds": "TDS deducted",
    "tds_reversal": "TDS reversed",
    "write_off": "Written off",
    "write_off_reversal": "Write-off reversed",
}
CREDIT_LABELS = {
    "payment_remainder": "Unallocated payment (on account)",
    "credit_note_excess": "Credit note excess (on account)",
    "applied": "On-account credit applied",
    "reversal": "On-account credit reversed",
}


class StatementMismatch(RuntimeError):  # noqa: N818 - domain name
    pass


async def _balance(db: AsyncSession, docs: list[DocRow], client_id: uuid.UUID, legal_entity_id: uuid.UUID | None, currency: str, as_of: date) -> Decimal:
    live = [d for d in docs if d.doc_date and d.doc_date <= as_of]
    bals = await settlement.balances(db, live, as_of, with_disputes=False)
    open_total = sum((b.open for b in bals.values()), ZERO)
    credit = await credit_svc.balance(db, client_id, legal_entity_id=legal_entity_id, currency=currency, as_of=as_of)
    return q2(open_total - credit)


async def build(
    ctx: RequestContext,  # noqa: ARG001
    db: AsyncSession,
    client_id: uuid.UUID,
    currency: str,
    start: date,
    end: date,
    legal_entity_id: uuid.UUID | None = None,
) -> StatementOut:
    docs = await documents(db, client_id=client_id, legal_entity_id=legal_entity_id, currency=currency)
    by_id = {d.id: d for d in docs}
    opening = await _balance(db, docs, client_id, legal_entity_id, currency, start - timedelta(days=1))
    raw: list[tuple[date, int, str, str | None, str, Decimal]] = []
    for d in docs:
        if d.doc_date and start <= d.doc_date <= end:
            raw.append((d.doc_date, 0, d.kind, d.number, "Invoice" if d.kind == "invoice" else "Debit note", d.document_total))
        if d.status == "cancelled" and d.cancel_basis == "irn_cancelled" and d.cancelled_at is not None and start <= d.cancelled_at.date() <= end:
            raw.append((d.cancelled_at.date(), 1, "cancellation", d.number, "Document cancelled (IRN)", -d.document_total))
    if by_id:
        f = settlement.facts_union()
        rows = (await db.execute(select(f.c.doc_id, f.c.amt, f.c.d, f.c.kind).where(f.c.doc_id.in_(list(by_id)), f.c.d >= start, f.c.d <= end))).all()
        for doc_id, amt, d, kind in rows:
            doc = by_id[doc_id]
            raw.append((d, 2, kind, doc.number, f"{LABELS.get(kind, kind)} - {doc.number}", -Decimal(amt)))
    stmt = select(ClientCredit).where(ClientCredit.client_id == client_id, ClientCredit.currency == currency, ClientCredit.credit_date >= start, ClientCredit.credit_date <= end)
    if legal_entity_id is not None:
        stmt = stmt.where(ClientCredit.legal_entity_id == legal_entity_id)
    for c in (await db.execute(stmt)).scalars():
        raw.append((c.credit_date, 3, f"credit_{c.kind}", None, CREDIT_LABELS.get(c.kind, c.kind), -Decimal(c.amount)))
    raw.sort(key=lambda r: (r[0], r[1]))
    running = opening
    lines: list[StatementLine] = []
    for d, _, kind, ref, desc, delta in raw:
        if delta == 0:
            continue
        running = q2(running + delta)
        lines.append(StatementLine(date=d, kind=kind, reference=ref, description=desc, debit=delta if delta > 0 else ZERO, credit=-delta if delta < 0 else ZERO, balance=running))
    closing = await _balance(db, docs, client_id, legal_entity_id, currency, end)
    if closing != running:
        msg = f"statement invariant broken for client {client_id}: lines {running} != facts {closing}"
        raise StatementMismatch(msg)
    return StatementOut(client_id=client_id, legal_entity_id=legal_entity_id, currency=currency, start=start, end=end, opening_balance=opening, closing_balance=closing, lines=lines)


def to_pdf(statement: StatementOut, client_name: str) -> bytes:
    from app.services.documents_service import render

    def m(v: Any) -> str:
        return f"{Decimal(v):,.2f}"

    md = [
        f"# Statement of account - {client_name}",
        f"Period: {statement.start.isoformat()} to {statement.end.isoformat()} | Currency: {statement.currency}",
        "",
        f"**Opening balance:** {m(statement.opening_balance)}",
        "",
        "| Date | Reference | Description | Debit | Credit | Balance |",
        "|---|---|---|---|---|---|",
    ]
    md += [f"| {ln.date.isoformat()} | {ln.reference or ''} | {ln.description} | {m(ln.debit)} | {m(ln.credit)} | {m(ln.balance)} |" for ln in statement.lines]
    md += ["", f"**Closing balance:** {m(statement.closing_balance)}"]
    return render.render_markdown("\n".join(md), header="Statement of account", footer="statement.v1", title="Statement of account")
