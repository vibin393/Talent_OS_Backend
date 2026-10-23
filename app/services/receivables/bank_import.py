# ruff: file-ignore[line-too-long, invalid-formatter-suppression-comment]
"""Bank CSV import (FR-153): parse -> preview (matched / unmatched / soft duplicate) -> apply selected rows.

* The file batch is recorded in ``file_import_batches`` (sha256 dedupe): the same file returns the same batch.
* Matching finds issued document numbers of the Legal Entity in the reference / narrative.
* Soft duplicate: an existing payment with the same (legal entity, bank account, reference, amount, date).
* Apply records each row's payment in its own savepoint (auto allocation to the matched documents' client).
CSV columns (header, case-insensitive): ``date`` (YYYY-MM-DD or DD/MM/YYYY), ``amount``, ``reference``,
``narrative``.
"""

import contextlib
import csv
import hashlib
import io
import re
import uuid
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import DomainError, NotFound
from app.models.imports import FileImportBatch
from app.models.receivables import BankStatementRow, Payment
from app.schemas.receivables import BankApplyIn, BankImportOut, BankRowOut, PaymentIn
from app.services.receivables.common import documents, invalid, tenant_of

KIND = "bank_statement"
MAX_ROWS = 5000


def _date(raw: str) -> date | None:
    raw = (raw or "").strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d-%m-%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _amount(raw: str) -> Decimal | None:
    try:
        value = Decimal((raw or "").replace(",", "").strip())
    except InvalidOperation:
        return None
    return value.quantize(Decimal("0.01")) if value > 0 else None


def parse(data: bytes) -> list[dict[str, str]]:
    text = data.decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    rows = [{(k or "").strip().lower(): (v or "").strip() for k, v in r.items()} for r in reader]
    if len(rows) > MAX_ROWS:
        raise invalid("too_many_rows", f"At most {MAX_ROWS} rows per file")
    return rows


async def _out(db: AsyncSession, batch: FileImportBatch, legal_entity_id: uuid.UUID) -> BankImportOut:
    rows = (await db.execute(select(BankStatementRow).where(BankStatementRow.batch_id == batch.id).order_by(BankStatementRow.row_no))).scalars()
    return BankImportOut(batch_id=batch.id, status=batch.status, legal_entity_id=legal_entity_id, rows=[BankRowOut.model_validate(r) for r in rows])


async def preview(ctx: RequestContext, db: AsyncSession, legal_entity_id: uuid.UUID, bank_account_id: uuid.UUID | None, data: bytes) -> BankImportOut:
    from app.services import imports_service

    digest = hashlib.sha256(data).hexdigest()
    existing = (
        await db.execute(
            select(FileImportBatch).where(FileImportBatch.kind == KIND, FileImportBatch.sha256 == digest, FileImportBatch.status.in_(("previewed", "applied"))).order_by(FileImportBatch.created_at)
        )
    ).scalars().first()
    if existing is not None:
        return await _out(db, existing, legal_entity_id)
    parsed = parse(data)
    docs = [d for d in await documents(db, legal_entity_id=legal_entity_id) if d.status == "issued" and d.number]
    numbers = sorted(((d.number, d) for d in docs if d.number), key=lambda x: -len(x[0]))
    batch = await imports_service.register_preview(ctx, db, KIND, None, digest, len(parsed), 0, {"legal_entity_id": str(legal_entity_id)})
    errors = 0
    for i, r in enumerate(parsed, start=1):
        d, amt = _date(r.get("date", "")), _amount(r.get("amount", "") or r.get("credit", ""))
        ref, narrative = r.get("reference") or None, r.get("narrative") or r.get("description") or None
        hay = f"{ref or ''} {narrative or ''}".upper()
        matched = [doc for num, doc in numbers if re.search(rf"(?<![A-Z0-9]){re.escape(num.upper())}(?![A-Z0-9])", hay)]
        dup = None
        if d is not None and amt is not None:
            dup = (
                await db.execute(
                    select(Payment.id).where(
                        Payment.legal_entity_id == legal_entity_id, Payment.reference == ref, Payment.amount == amt, Payment.payment_date == d,
                        Payment.receiving_bank_account_id == bank_account_id if bank_account_id else Payment.receiving_bank_account_id.is_(None),
                    )
                )
            ).scalars().first()
        kind = "failed" if d is None or amt is None else ("soft_duplicate" if dup else ("matched" if matched else "unmatched"))
        errors += kind == "failed"
        clients = {m.client_id for m in matched}
        db.add(
            BankStatementRow(
                id=uuid.uuid4(), tenant_id=tenant_of(db), legal_entity_id=legal_entity_id, batch_id=batch.id, bank_account_id=bank_account_id, row_no=i,
                date=d, amount=amt, reference=(ref or "")[:200] or None, narrative=(narrative or "")[:500] or None, match_kind=kind,
                matched_document_ids=[m.id for m in matched], client_id=next(iter(clients)) if len(clients) == 1 else None,
                soft_duplicate_of=dup, error="unreadable date or amount" if kind == "failed" else None,
            )  # fmt: skip
        )
    await db.flush()
    row = (await db.execute(select(FileImportBatch).where(FileImportBatch.id == batch.id))).scalar_one()
    row.error_count = errors
    await db.flush()
    return await _out(db, row, legal_entity_id)


async def apply(ctx: RequestContext, db: AsyncSession, batch_id: uuid.UUID, data: BankApplyIn) -> BankImportOut:
    from app.services.receivables import payments

    batch = (await db.execute(select(FileImportBatch).where(FileImportBatch.id == batch_id, FileImportBatch.kind == KIND))).scalar_one_or_none()
    if batch is None:
        raise NotFound("Import batch not found")
    rows = (await db.execute(select(BankStatementRow).where(BankStatementRow.batch_id == batch.id, BankStatementRow.id.in_(data.row_ids)).with_for_update())).scalars().all()
    legal_entity_id = rows[0].legal_entity_id if rows else uuid.UUID(str((batch.summary or {}).get("legal_entity_id")))
    for r in rows:
        if r.match_kind in ("applied", "failed") or r.client_id is None or r.date is None or r.amount is None:
            continue
        try:
            async with db.begin_nested():
                out = await payments.record(
                    ctx, db,
                    PaymentIn(legal_entity_id=r.legal_entity_id, client_id=r.client_id, payment_date=r.date, amount=r.amount, currency=r.currency, mode=data.mode, reference=r.reference, receiving_bank_account_id=r.bank_account_id, allocations="auto"),
                    import_batch_id=batch.id,
                )  # fmt: skip
            r.payment_id = out.id
            r.match_kind = "applied"
            r.error = None
        except DomainError as exc:
            r.error = f"{exc.code}: {exc.message}"[:300]
    await db.flush()
    if batch.status == "previewed" and all(r.match_kind == "applied" for r in rows):
        from app.services import imports_service

        with contextlib.suppress(DomainError):
            await imports_service.mark_applied(ctx, db, batch.id)
    return await _out(db, batch, legal_entity_id)
