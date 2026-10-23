"""Payment Terms (Epic 16 A1): tenant list seeded lazily, one default; due-date precedence for billing."""

import uuid
from datetime import date, timedelta

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.models.commercials import BillingProfile, Contract, PaymentTerm
from app.schemas.commercials import PaymentTermIn, PaymentTermOut, PaymentTermUpdate
from app.services.commercials.common import conflict, invalid, now, tenant_of

SEED: tuple[tuple[str, int, bool], ...] = (
    ("Due on receipt", 0, False),
    ("Net 30", 30, True),
    ("Net 45", 45, False),
    ("Net 60", 60, False),
)


async def ensure_seeded(db: AsyncSession) -> None:
    if (await db.execute(select(func.count()).select_from(PaymentTerm))).scalar_one():
        return
    tenant_id = tenant_of(db)
    for name, days, is_default in SEED:
        db.add(PaymentTerm(id=uuid.uuid4(), tenant_id=tenant_id, name=name, days=days, is_default=is_default))
    await db.flush()


async def _get(db: AsyncSession, term_id: uuid.UUID, *, lock: bool = False) -> PaymentTerm:
    stmt = select(PaymentTerm).where(PaymentTerm.id == term_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Payment terms not found")
    return row


async def assert_usable(db: AsyncSession, term_id: uuid.UUID | None) -> None:
    if term_id is None:
        return
    row = (await db.execute(select(PaymentTerm).where(PaymentTerm.id == term_id))).scalar_one_or_none()
    if row is None or row.archived_at is not None:
        raise invalid(
            "payment_terms_invalid", "Unknown or archived payment terms", payment_terms_id=str(term_id)
        )


async def _clear_default(db: AsyncSession, keep: uuid.UUID | None) -> None:
    stmt = update(PaymentTerm).where(PaymentTerm.is_default.is_(True)).values(is_default=False)
    if keep is not None:
        stmt = stmt.where(PaymentTerm.id != keep)
    await db.execute(stmt)


async def list_terms(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    include_archived: bool = False,
) -> list[PaymentTermOut]:
    await ensure_seeded(db)
    stmt = select(PaymentTerm).order_by(PaymentTerm.days, PaymentTerm.name)
    if not include_archived:
        stmt = stmt.where(PaymentTerm.archived_at.is_(None))
    return [PaymentTermOut.model_validate(r) for r in (await db.execute(stmt)).scalars()]


async def create_term(ctx: RequestContext, db: AsyncSession, data: PaymentTermIn) -> PaymentTermOut:  # ruff: ignore[unused-function-argument]
    await ensure_seeded(db)
    if data.is_default:
        await _clear_default(db, None)
    row = PaymentTerm(id=uuid.uuid4(), tenant_id=tenant_of(db), **data.model_dump())
    db.add(row)
    await db.flush()
    await db.refresh(row)
    return PaymentTermOut.model_validate(row)


async def update_term(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    term_id: uuid.UUID,
    data: PaymentTermUpdate,
) -> PaymentTermOut:
    row = await _get(db, term_id, lock=True)
    changes = data.model_dump(exclude_unset=True, exclude_none=True)
    if changes.get("is_default"):
        if row.archived_at is not None:
            raise conflict("payment_terms_archived", "Archived payment terms cannot be the default")
        await _clear_default(db, row.id)
    for key, value in changes.items():
        setattr(row, key, value)
    await db.flush()
    await db.refresh(row)
    return PaymentTermOut.model_validate(row)


async def archive_term(ctx: RequestContext, db: AsyncSession, term_id: uuid.UUID) -> PaymentTermOut:  # ruff: ignore[unused-function-argument]
    row = await _get(db, term_id, lock=True)
    if row.is_default:
        raise conflict("payment_terms_default", "Choose another default before archiving these payment terms")
    if row.archived_at is None:
        row.archived_at = now()
        await db.flush()
        await db.refresh(row)
    return PaymentTermOut.model_validate(row)


async def _days(db: AsyncSession, term_id: uuid.UUID | None) -> int | None:
    if term_id is None:
        return None
    return (await db.execute(select(PaymentTerm.days).where(PaymentTerm.id == term_id))).scalar_one_or_none()


async def payment_terms_days(
    db: AsyncSession, client_id: uuid.UUID | None, contract_id: uuid.UUID | None
) -> int:
    """Precedence: Contract override -> Billing Profile -> tenant default (-> 30 when nothing is seeded)."""
    if contract_id is not None:
        term = (
            await db.execute(select(Contract.payment_terms_id).where(Contract.id == contract_id))
        ).scalar_one_or_none()
        days = await _days(db, term)
        if days is not None:
            return days
    if client_id is not None:
        term = (
            await db.execute(
                select(BillingProfile.payment_terms_id).where(BillingProfile.client_id == client_id)
            )
        ).scalar_one_or_none()
        days = await _days(db, term)
        if days is not None:
            return days
    await ensure_seeded(db)
    default = (
        await db.execute(
            select(PaymentTerm.days).where(
                PaymentTerm.is_default.is_(True), PaymentTerm.archived_at.is_(None)
            )
        )
    ).scalar_one_or_none()
    return 30 if default is None else default


async def due_date(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID | None,
    contract_id: uuid.UUID | None,
    doc_date: date,
) -> date:
    return doc_date + timedelta(days=await payment_terms_days(db, client_id, contract_id))
