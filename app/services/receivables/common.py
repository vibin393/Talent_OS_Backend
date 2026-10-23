# ruff: file-ignore[line-too-long]
"""Shared helpers of the receivables domain: errors, dates / periods, document reads, FX quantization."""

import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal, cast

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import fiscal
from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.billing import CreditNote, DebitNote, Invoice
from app.models.legal_entities import AccountingPeriod

DocKind = Literal["invoice", "debit_note"]
CENT = Decimal("0.01")
ZERO = Decimal("0.00")
DOC_KINDS: tuple[DocKind, ...] = ("invoice", "debit_note")


def q2(value: Decimal) -> Decimal:
    return Decimal(value).quantize(CENT)


def now() -> datetime:
    return datetime.now(UTC)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "receivables needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    principal = ctx.principal
    return getattr(principal, "id", None) if principal is not None and getattr(principal, "type", None) == "user" else None


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details)


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details)


def model_for(kind: str) -> type[Invoice] | type[DebitNote]:
    if kind == "invoice":
        return Invoice
    if kind == "debit_note":
        return DebitNote
    raise invalid("invalid_document_kind", "Unknown document kind", document_kind=kind)


def target_cols(kind: str, doc_id: uuid.UUID) -> dict[str, uuid.UUID | None]:
    return {"invoice_id": doc_id if kind == "invoice" else None, "debit_note_id": doc_id if kind == "debit_note" else None}


def target_of(row: Any) -> tuple[DocKind, uuid.UUID]:
    if row.invoice_id is not None:
        return "invoice", row.invoice_id
    return "debit_note", row.debit_note_id


async def today(db: AsyncSession) -> date:
    tz = (await db.execute(text("SELECT timezone FROM legal_entities ORDER BY created_at LIMIT 1"))).scalar() or "Asia/Kolkata"
    return fiscal.today_for(str(tz))


async def period_status(db: AsyncSession, legal_entity_id: uuid.UUID, on: date) -> str | None:
    return (
        await db.execute(
            select(AccountingPeriod.status).where(
                AccountingPeriod.legal_entity_id == legal_entity_id,
                AccountingPeriod.year == on.year,
                AccountingPeriod.month == on.month,
            )
        )
    ).scalar_one_or_none()


async def is_closed(db: AsyncSession, legal_entity_id: uuid.UUID, on: date) -> bool:
    return (await period_status(db, legal_entity_id, on)) != "open"


async def assert_open(db: AsyncSession, legal_entity_id: uuid.UUID, on: date) -> None:
    from app.services import legal_entities_service

    await legal_entities_service.assert_open(db, legal_entity_id, on)


async def late_date(db: AsyncSession, legal_entity_id: uuid.UUID, wanted: date) -> tuple[date, bool]:
    """Date a late item: ``wanted`` when its month is open, else today (open) with ``prior_period``."""
    if not await is_closed(db, legal_entity_id, wanted):
        return wanted, False
    when = await today(db)
    await assert_open(db, legal_entity_id, when)
    return when, True


def quarter_of(d: date) -> str:
    fy = fiscal.fiscal_year(d)
    q = {4: 1, 5: 1, 6: 1, 7: 2, 8: 2, 9: 2, 10: 3, 11: 3, 12: 3, 1: 4, 2: 4, 3: 4}[d.month]
    return f"{fy} Q{q}"


@dataclass(frozen=True)
class DocRow:
    kind: DocKind
    id: uuid.UUID
    number: str | None
    legal_entity_id: uuid.UUID
    client_id: uuid.UUID
    currency: str
    document_total: Decimal
    inr_document_total: Decimal
    fx_rate: Decimal | None
    doc_date: date | None
    due_date: date | None
    status: str
    cancel_basis: str | None
    cancelled_at: datetime | None
    taxable_total: Decimal

    @property
    def fx(self) -> Decimal:
        return Decimal(1) if self.currency == "INR" or self.fx_rate is None else Decimal(self.fx_rate)

    def cancelled_by(self, as_of: date | None) -> bool:
        if self.status != "cancelled" or self.cancel_basis != "irn_cancelled":
            return False
        return as_of is None or self.cancelled_at is None or self.cancelled_at.date() <= as_of


def _doc_row(kind: DocKind, d: Any) -> DocRow:
    return DocRow(
        kind, d.id, d.number, d.legal_entity_id, d.client_id, d.currency, Decimal(d.document_total),
        Decimal(d.inr_document_total or 0) or (Decimal(d.document_total) if d.currency == "INR" else ZERO),
        d.fx_rate, d.doc_date, d.due_date, d.status, d.cancel_basis, d.cancelled_at, Decimal(d.taxable_total or 0),
    )  # fmt: skip


async def documents(
    db: AsyncSession,
    *,
    legal_entity_id: uuid.UUID | None = None,
    client_id: uuid.UUID | None = None,
    currency: str | None = None,
    ids: list[uuid.UUID] | None = None,
    as_of: date | None = None,
) -> list[DocRow]:
    out: list[DocRow] = []
    for kind in DOC_KINDS:
        m = cast("type[Invoice]", model_for(kind))
        stmt = select(m).where(m.status.in_(("issued", "cancelled")), m.doc_date.is_not(None))
        if legal_entity_id is not None:
            stmt = stmt.where(m.legal_entity_id == legal_entity_id)
        if client_id is not None:
            stmt = stmt.where(m.client_id == client_id)
        if currency is not None:
            stmt = stmt.where(m.currency == currency)
        if ids is not None:
            stmt = stmt.where(m.id.in_(ids))
        if as_of is not None:
            stmt = stmt.where(m.doc_date <= as_of)
        out.extend(_doc_row(kind, d) for d in (await db.execute(stmt)).scalars())
    return out


async def document(db: AsyncSession, kind: str, doc_id: uuid.UUID) -> DocRow:
    m = cast("type[Invoice]", model_for(kind))
    d = (await db.execute(select(m).where(m.id == doc_id))).scalar_one_or_none()
    if d is None or d.status not in ("issued", "cancelled") or d.doc_date is None:
        raise NotFound("Receivable document not found")
    return _doc_row(kind, d)  # type: ignore[arg-type]


async def find_document(db: AsyncSession, doc_id: uuid.UUID) -> DocRow:
    for kind in DOC_KINDS:
        try:
            return await document(db, kind, doc_id)
        except NotFound:
            continue
    raise NotFound("Receivable document not found")


async def credit_note(db: AsyncSession, cn_id: uuid.UUID) -> Any:
    return (await db.execute(select(CreditNote).where(CreditNote.id == cn_id))).scalar_one_or_none()
