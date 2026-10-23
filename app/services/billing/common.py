# ruff: file-ignore[line-too-long]
"""Shared helpers of the ``billing`` domain: tenant / clock access, settings, document kinds and error builders."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any, Literal

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError, NotFound, ValidationFailed
from app.models.billing import (
    BillingSettings,
    CreditNote,
    CreditNoteLine,
    CreditNoteTaxLine,
    DebitNote,
    DebitNoteLine,
    DebitNoteTaxLine,
    Invoice,
    InvoiceLine,
    InvoiceTaxLine,
)

DocKind = Literal["invoice", "credit_note", "debit_note"]
DOC_KINDS: tuple[DocKind, ...] = ("invoice", "credit_note", "debit_note")
EDITABLE = ("draft", "pending_approval")

MODELS: dict[str, tuple[Any, Any, Any, str]] = {
    "invoice": (Invoice, InvoiceLine, InvoiceTaxLine, "invoice_id"),
    "credit_note": (CreditNote, CreditNoteLine, CreditNoteTaxLine, "credit_note_id"),
    "debit_note": (DebitNote, DebitNoteLine, DebitNoteTaxLine, "debit_note_id"),
}
TARGET_KIND = {"invoice": "invoice_line", "credit_note": "credit_note_line", "debit_note": "debit_note_line"}
# Columns that may change after issue (AD-21 allow-list) -> excluded from the content hash.
OPERATIONAL = frozenset(
    {
        "delivery_status",
        "last_sent_at",
        "pdf_file_id",
        "pdf_render_version",
        "einvoice_status",
        "irn",
        "ack_no",
        "ack_at",
        "signed_qr",
        "irn_report_by",
        "irn_cancelled_at",
        "irn_cancel_reason",
        "status",
        "cancel_basis",
        "cancelled_at",
        "cancelling_credit_note_id",
        "updated_at",
        "content_hash",
        "stale",
        "approved",
        "approval_request_id",
        "approved_fingerprint",
        "preview_fingerprint",
        "warnings",
        "preview_date",
        # Epic 51 (315_01): DSC signing state, allow-listed after issue.
        "signed_pdf_file_id",
        "dsc_status",
        "dsc_signed_at",
    }
)


def tenant_of(db: AsyncSession) -> uuid.UUID:
    raw = db.sync_session.info.get("tenant_id")
    if raw is None:
        msg = "billing needs a tenant session"
        raise RuntimeError(msg)
    return uuid.UUID(str(raw))


def now() -> datetime:
    return datetime.now(UTC)


def user_id(ctx: RequestContext) -> uuid.UUID | None:
    return getattr(ctx.principal, "id", None) if getattr(ctx.principal, "type", None) == "user" else None


def dec(value: Any) -> Decimal:
    return value if isinstance(value, Decimal) else Decimal(str(value))


def not_found(kind: str = "invoice") -> NotFound:
    return NotFound(f"{kind.replace('_', ' ').capitalize()} not found")


def conflict(code: str, message: str, **details: Any) -> ConflictError:
    return ConflictError(message, code=code, details=details or None)


def invalid(code: str, message: str, **details: Any) -> ValidationFailed:
    return ValidationFailed(message, code=code, details=details or None)


async def settings(db: AsyncSession) -> BillingSettings:
    tid = tenant_of(db)
    row = (
        await db.execute(select(BillingSettings).where(BillingSettings.tenant_id == tid))
    ).scalar_one_or_none()
    if row is None:
        await db.execute(pg_insert(BillingSettings).values(tenant_id=tid).on_conflict_do_nothing())
        row = (await db.execute(select(BillingSettings).where(BillingSettings.tenant_id == tid))).scalar_one()
    return row


async def get_doc(db: AsyncSession, kind: str, doc_id: uuid.UUID, *, lock: bool = False) -> Any:
    model = MODELS[kind][0]
    stmt = select(model).where(model.id == doc_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise not_found(kind)
    return row


async def lines_of(db: AsyncSession, kind: str, doc_id: uuid.UUID) -> list[Any]:
    _m, line_model, _t, fk = MODELS[kind]
    rows = await db.execute(
        select(line_model).where(getattr(line_model, fk) == doc_id).order_by(line_model.line_no)
    )
    return list(rows.scalars())


async def tax_lines_of(db: AsyncSession, kind: str, doc_id: uuid.UUID) -> list[Any]:
    _m, _l, tax_model, fk = MODELS[kind]
    rows = await db.execute(select(tax_model).where(getattr(tax_model, fk) == doc_id))
    return list(rows.scalars())


def assert_editable(doc: Any) -> None:
    if doc.status not in EDITABLE:
        raise conflict("document_not_editable", "Only draft documents can be changed", status=doc.status)


def fy_end(fiscal_year: str) -> date:
    from app.core.fiscal import fiscal_year_bounds

    return fiscal_year_bounds(fiscal_year)[1]
