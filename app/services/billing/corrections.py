# ruff: noqa: E501, ARG001
"""Hours / rate corrections (FR-150, AD-23): unposted negative ``adjustment_entry`` items and every
``rate_adjustment`` become draft Credit Notes (negative deltas) and Debit Notes (positive deltas) on the Invoice
that billed the original work. The original line is found through the ledger (``postings.targets_for`` of the
referenced source, state ``posted``), else the issued time line of the placement / category whose period covers
the item date. Postings are reserved on the note lines (``credit_note_line`` / ``debit_note_line``)."""

import uuid
from collections import defaultdict
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError
from app.core.money import MONEY_CONTEXT, quantize_amount
from app.models.billing import Invoice, InvoiceLine
from app.services import postings
from app.services.billing import pricing

ExceptionRow = tuple[str, uuid.UUID | None, uuid.UUID | None, str]


async def _original_line(db: AsyncSession, item: postings.SourceItem) -> InvoiceLine | None:
    attrs = item.attributes or {}
    kind, sid = attrs.get("original_source_kind"), attrs.get("original_source_id")
    if kind and sid:
        for p in await postings.targets_for(
            db, "billing", str(kind), sid if isinstance(sid, uuid.UUID) else uuid.UUID(str(sid))
        ):
            if p.state == "posted" and p.target_kind == "invoice_line":
                return (
                    await db.execute(select(InvoiceLine).where(InvoiceLine.id == p.target_id))
                ).scalar_one_or_none()
    if item.placement_id is None:
        return None
    stmt = (
        select(InvoiceLine)
        .join(Invoice, Invoice.id == InvoiceLine.invoice_id)
        .where(
            InvoiceLine.placement_id == item.placement_id,
            InvoiceLine.line_kind.in_(("time", "adjustment")),
            InvoiceLine.category == str(attrs.get("category") or "regular"),
            InvoiceLine.period_start <= item.item_date,
            InvoiceLine.period_end >= item.item_date,
            Invoice.status.in_(("issued", "cancelled")),
        )
        .order_by(Invoice.doc_date.desc())
        .limit(1)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def build(
    ctx: RequestContext,
    db: AsyncSession,
    items: list[postings.SourceItem],
    *,
    run_id: uuid.UUID | None = None,
) -> list[ExceptionRow]:
    from app.services.billing import notes

    exceptions: list[ExceptionRow] = []
    credits: dict[uuid.UUID, list[notes.NoteLineSpec]] = defaultdict(list)
    debits: dict[uuid.UUID, list[notes.NoteLineSpec]] = defaultdict(list)
    reasons: dict[tuple[str, uuid.UUID], str] = {}
    for item in items:
        line = await _original_line(db, item)
        if line is None:
            exceptions.append(
                (
                    "correction_original_missing",
                    item.placement_id,
                    None,
                    f"{item.source_kind} {item.source_id}",
                )
            )
            continue
        invoice = (await db.execute(select(Invoice).where(Invoice.id == line.invoice_id))).scalar_one()
        if invoice.status != "issued":
            exceptions.append(
                ("correction_original_cancelled", item.placement_id, invoice.client_id, str(invoice.number))
            )
            continue
        attrs = item.attributes or {}
        qty = Decimal(item.quantity)
        if item.source_kind == "rate_adjustment":
            delta = Decimal(attrs.get("rate_delta") or 0)
            mult = Decimal(attrs.get("multiplier") or line.multiplier or 1)
            amount = quantize_amount(
                MONEY_CONTEXT.multiply(MONEY_CONTEXT.multiply(qty, delta), mult), invoice.currency
            )
            spec = notes.NoteLineSpec(
                line,
                abs(amount),
                abs(qty),
                abs(delta),
                mult,
                description=f"Rate correction - {line.description}"[:500],
                sources=[item],
            )
            reason = "rate_correction"
        else:
            amount = -pricing.line_amount(
                abs(qty), Decimal(line.rate), Decimal(line.multiplier), invoice.currency
            )
            spec = notes.NoteLineSpec(
                line,
                abs(amount),
                abs(qty),
                Decimal(line.rate),
                Decimal(line.multiplier),
                description=f"Hours correction - {line.description}"[:500],
                sources=[item],
            )
            reason = "hours_correction"
        if amount == 0:
            continue
        target = debits if amount > 0 else credits
        target[invoice.id].append(spec)
        reasons.setdefault(("debit" if amount > 0 else "credit", invoice.id), reason)
    for kind, groups in (("credit_note", credits), ("debit_note", debits)):
        for invoice_id, specs in groups.items():
            invoice = (await db.execute(select(Invoice).where(Invoice.id == invoice_id))).scalar_one()
            reason = reasons["credit" if kind == "credit_note" else "debit", invoice_id]
            try:
                async with db.begin_nested():
                    await notes._write_note(ctx, db, kind, invoice, specs, reason=reason)
            except DomainError as exc:
                exceptions.append((exc.code, specs[0].original.placement_id, invoice.client_id, str(exc)))
    return exceptions


__all__ = ["Any", "build"]
