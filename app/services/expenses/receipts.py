"""Expense receipts (FR-138, AD-13): upload through ``files_service`` (size / type sniffing / malware scan;
infected -> 422 ``receipt_infected``, never linked), owned by the Worker (``owner_entity_type =
'expense_receipt'``, ``owner_entity_id = candidate_id``); the SHA-256 drives the duplicate-receipt flag.
Downloads re-check scope (Worker: own receipts; Client: billable lines of its claims; internal:
``expense:read``).

Optional AI receipt read (AD-6): suggestions only -- the Worker confirms every field; ``ok=False``
means manual
entry; the amount must be a plain decimal string (``"1,234.50"`` is dropped, never parsed as a float).
"""

import uuid
from datetime import date
from typing import Any

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound, ValidationFailed
from app.models.files import StoredFile
from app.schemas.expenses_portal import ReceiptOut, ReceiptReadOut, ReceiptSuggestion
from app.services.expenses.amounts import parse_suggested_amount
from app.services.expenses.common import settings_row
from app.services.expenses.lines import RECEIPT_OWNER

UPLOAD_KINDS = frozenset({"pdf", "png", "jpeg"})
FEATURE = "expense_receipt_read"
PROMPT = "expense_receipt_read.v1"
CATEGORIES = ("travel", "accommodation", "meals", "mileage", "equipment", "other")
_CODE_MAP = {
    "file_infected": "receipt_infected",
    "unsupported_file_type": "receipt_type_unsupported",
    "file_too_large": "receipt_too_large",
}


class ReceiptRead(BaseModel):
    date: str | None = None
    merchant: str | None = None
    amount: str | None = None
    currency: str | None = None
    category_guess: str | None = None


def _receipt_out(row: StoredFile) -> ReceiptOut:
    return ReceiptOut(
        file_id=row.id,
        original_name=row.original_name,
        content_type=row.content_type,
        size_bytes=row.size_bytes,
        scan_status=row.scan_status,
    )


async def upload(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, data: bytes, filename: str | None
) -> ReceiptOut:
    from app.services import files_service

    settings = await settings_row(db)
    if len(data) > settings.max_receipt_mb * 1024 * 1024:
        raise ValidationFailed(
            f"The receipt is larger than {settings.max_receipt_mb} MB",
            code="receipt_too_large",
            details={"max_mb": settings.max_receipt_mb},
        )
    try:
        row = await files_service.store_upload(
            ctx, db, data, filename, UPLOAD_KINDS, RECEIPT_OWNER, candidate_id
        )
    except ValidationFailed as exc:
        code = _CODE_MAP.get(exc.code)
        if code is None:
            raise
        raise ValidationFailed(exc.message, code=code, details=exc.details) from exc
    return _receipt_out(row)


async def own_receipt(db: AsyncSession, candidate_id: uuid.UUID, file_id: uuid.UUID) -> StoredFile:
    row = (await db.execute(select(StoredFile).where(StoredFile.id == file_id))).scalar_one_or_none()
    if row is None or row.owner_entity_type != RECEIPT_OWNER or row.owner_entity_id != candidate_id:
        raise NotFound("Receipt not found")
    return row


async def read_bytes(row: StoredFile) -> bytes:
    from app.services import files_service

    if row.scan_status == "infected":
        raise NotFound("Receipt not found")
    try:
        return await files_service.read_file(row)
    except FileNotFoundError as exc:
        raise NotFound("Receipt not found") from exc


def _receipt_text(data: bytes, content_type: str) -> str | None:
    from app.services.candidates.extraction import extract_text

    kind = "pdf" if content_type == "application/pdf" else None
    if kind is None:
        return None
    try:
        result = extract_text(data, kind)
    except Exception:  # extraction failures fall back to manual entry
        return None
    return (result.text or "").strip() or None


async def _run_ai(ctx: RequestContext, db: AsyncSession, text: str, candidate_id: uuid.UUID) -> Any:
    from app.services import ai

    return await ai.run(
        ctx,
        db,
        FEATURE,
        PROMPT,
        inputs={},
        untrusted={"receipt": text},
        output_schema=ReceiptRead,
        subject=("candidate", candidate_id),
    )


def suggestion_from(data: ReceiptRead) -> ReceiptSuggestion:
    on: date | None = None
    if data.date:
        try:
            on = date.fromisoformat(data.date.strip()[:10])
        except ValueError:
            on = None
    currency = (data.currency or "").strip().upper() or None
    if currency is not None and (len(currency) != 3 or not currency.isalpha()):
        currency = None
    category = (data.category_guess or "").strip().lower() or None
    return ReceiptSuggestion(
        expense_date=on,
        merchant=(data.merchant or "").strip()[:200] or None,
        amount=parse_suggested_amount(data.amount),
        currency=currency,
        category=category if category in CATEGORIES else None,
    )


async def read_receipt(
    ctx: RequestContext, db: AsyncSession, candidate_id: uuid.UUID, file_id: uuid.UUID
) -> ReceiptReadOut:
    if not (await settings_row(db)).ai_receipt_read_enabled:
        raise NotFound("Receipt reading is not enabled")
    row = await own_receipt(db, candidate_id, file_id)
    text = _receipt_text(await read_bytes(row), row.content_type)
    if text is None:
        return ReceiptReadOut(ok=False, suggestion=None, error_code="unreadable")
    result = await _run_ai(ctx, db, text[:20000], candidate_id)
    if not getattr(result, "ok", False) or getattr(result, "data", None) is None:
        return ReceiptReadOut(
            ok=False, suggestion=None, error_code=getattr(result, "error_code", None) or "ai_failed"
        )
    data = (
        result.data
        if isinstance(result.data, ReceiptRead)
        else ReceiptRead.model_validate(result.data.model_dump())
    )
    return ReceiptReadOut(ok=True, suggestion=suggestion_from(data))


async def line_receipt(db: AsyncSession, claim_id: uuid.UUID, line_id: uuid.UUID) -> tuple[bytes, str, str]:
    """Read a line's receipt for an internal download (the caller holds ``expense:read``)."""
    from app.models.expenses import ExpenseLine

    line = (
        await db.execute(
            select(ExpenseLine).where(ExpenseLine.id == line_id, ExpenseLine.claim_id == claim_id)
        )
    ).scalar_one_or_none()
    if line is None or line.receipt_file_id is None:
        raise NotFound("Receipt not found")
    row = (await db.execute(select(StoredFile).where(StoredFile.id == line.receipt_file_id))).scalar_one()
    return await read_bytes(row), row.original_name, row.content_type
