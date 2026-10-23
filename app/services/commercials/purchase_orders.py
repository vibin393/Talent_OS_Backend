"""Purchase Orders (Epic 16 A7, FR-92): CRUD, derived validity / balance, read API for placements / billing.

Validity (upcoming / valid / expired) is derived from the dates; ``status`` stores only ``active`` (open),
``closed`` and ``cancelled``. The balance is always cap - sum(ledger) (AD-32), never a stored counter.
"""

import uuid
from datetime import date, timedelta
from decimal import Decimal

from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.core.events import emit
from app.core.money import quantize_amount, quantize_hours
from app.events.commercials import PoValidityChanged
from app.models.commercials import PoConsumption, PurchaseOrder
from app.schemas.commercials import (
    PoBalance,
    PoConsumptionOut,
    PoPlacementOut,
    PoRef,
    PoValidity,
    PurchaseOrderIn,
    PurchaseOrderOut,
    PurchaseOrderUpdate,
)
from app.services.commercials import contracts
from app.services.commercials.common import (
    conflict,
    invalid,
    le_today,
    legal_entity,
    now,
    require_client,
    tenant_of,
    timeline,
)

ENTITY = "purchase_order"
ZERO = Decimal("0")
HUNDRED = Decimal("100")


async def get_row(db: AsyncSession, po_id: uuid.UUID, *, lock: bool = False) -> PurchaseOrder:
    stmt = select(PurchaseOrder).where(PurchaseOrder.id == po_id)
    if lock:
        stmt = stmt.with_for_update()
    row = (await db.execute(stmt)).scalar_one_or_none()
    if row is None:
        raise NotFound("Purchase order not found")
    return row


async def consumed(db: AsyncSession, po_id: uuid.UUID) -> tuple[Decimal, Decimal]:
    amount, hours = (
        await db.execute(
            select(
                func.coalesce(func.sum(PoConsumption.amount), 0),
                func.coalesce(func.sum(PoConsumption.hours), 0),
            ).where(PoConsumption.po_id == po_id)
        )
    ).one()
    return Decimal(amount), Decimal(hours)


async def consumed_many(db: AsyncSession, ids: list[uuid.UUID]) -> dict[uuid.UUID, tuple[Decimal, Decimal]]:
    if not ids:
        return {}
    rows = await db.execute(
        select(PoConsumption.po_id, func.sum(PoConsumption.amount), func.sum(PoConsumption.hours))
        .where(PoConsumption.po_id.in_(ids))
        .group_by(PoConsumption.po_id)
    )
    return {pid: (Decimal(a or 0), Decimal(h or 0)) for pid, a, h in rows.all()}


def remaining(po: PurchaseOrder, amount: Decimal, hours: Decimal) -> tuple[Decimal | None, Decimal | None]:
    return (
        None if po.value_cap is None else po.value_cap - amount,
        None if po.hours_cap is None else po.hours_cap - hours,
    )


def pct_used(po: PurchaseOrder, amount: Decimal, hours: Decimal) -> Decimal | None:
    pcts: list[Decimal] = []
    if po.value_cap:
        pcts.append(amount * HUNDRED / po.value_cap)
    if po.hours_cap:
        pcts.append(hours * HUNDRED / po.hours_cap)
    return max(pcts).quantize(Decimal("0.01")) if pcts else None


def validity_on(po: PurchaseOrder, today: date) -> str:
    if today < po.valid_from:
        return "upcoming"
    if today > po.valid_to:
        return "expired"
    return "valid"


def balance(po: PurchaseOrder, amount: Decimal, hours: Decimal) -> PoBalance:
    rem_a, rem_h = remaining(po, amount, hours)
    return PoBalance(
        po_id=po.id,
        currency=po.currency,
        value_cap=po.value_cap,
        consumed_amount=amount,
        remaining_amount=rem_a,
        hours_cap=po.hours_cap,
        consumed_hours=hours,
        remaining_hours=rem_h,
        valid_from=po.valid_from,
        valid_to=po.valid_to,
        status=po.status,
    )


async def _out(
    db: AsyncSession,
    po: PurchaseOrder,
    used: tuple[Decimal, Decimal] | None = None,
    today: date | None = None,
) -> PurchaseOrderOut:
    amount, hours = used if used is not None else await consumed(db, po.id)
    rem_a, rem_h = remaining(po, amount, hours)
    day = today or await le_today(db, po.legal_entity_id)
    return PurchaseOrderOut.model_validate(po).model_copy(
        update={
            "consumed_amount": amount,
            "consumed_hours": hours,
            "remaining_amount": rem_a,
            "remaining_hours": rem_h,
            "pct_used": pct_used(po, amount, hours),
            "validity": validity_on(po, day),
        }
    )


# ================================================================ CRUD
async def _check_contract(
    db: AsyncSession,
    ctx: RequestContext,
    contract_id: uuid.UUID | None,
    client_id: uuid.UUID,
    legal_entity_id: uuid.UUID,
) -> None:
    if contract_id is None:
        return
    today = await le_today(db, legal_entity_id)
    await contracts.assert_contract_active(ctx, db, contract_id, client_id, legal_entity_id, today)


def _number_taken(number: str) -> Exception:
    return conflict("po_number_exists", "This client already has a PO with that number", po_number=number)


async def create_po(ctx: RequestContext, db: AsyncSession, data: PurchaseOrderIn) -> PurchaseOrderOut:
    await require_client(db, data.client_id)
    await legal_entity(db, data.legal_entity_id)
    if data.value_cap is None and data.hours_cap is None:
        raise invalid("po_cap_required", "Give a value cap, an hours cap or both")
    if data.valid_to < data.valid_from:
        raise invalid("po_validity_invalid", "The PO must end on or after its start date")
    await _check_contract(db, ctx, data.contract_id, data.client_id, data.legal_entity_id)
    exists = (
        await db.execute(
            select(PurchaseOrder.id).where(
                PurchaseOrder.client_id == data.client_id,
                func.lower(PurchaseOrder.po_number) == data.po_number.lower(),
            )
        )
    ).first()
    if exists is not None:
        raise _number_taken(data.po_number)
    values = data.model_dump()
    if values["value_cap"] is not None:
        values["value_cap"] = quantize_amount(values["value_cap"], data.currency)
    if values["hours_cap"] is not None:
        values["hours_cap"] = quantize_hours(values["hours_cap"])
    if values["owner_id"] is None:
        values["owner_id"] = (
            getattr(ctx.principal, "id", None) if getattr(ctx.principal, "type", None) == "user" else None
        )
    po = PurchaseOrder(id=uuid.uuid4(), tenant_id=tenant_of(db), status="active", **values)
    try:
        async with db.begin_nested():
            db.add(po)
            await db.flush()
    except IntegrityError as exc:
        if "currency" in str(exc.orig):
            raise invalid("currency_not_supported", "Unknown currency", currency=data.currency) from exc
        raise _number_taken(data.po_number) from exc
    await timeline(ctx, db, "client", po.client_id, f"PO {po.po_number} created", {"po_id": str(po.id)})
    await db.refresh(po)
    return await _out(db, po, (ZERO, ZERO))


async def get_po(ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID) -> PurchaseOrderOut:  # ruff: ignore[unused-function-argument]
    return await _out(db, await get_row(db, po_id))


async def list_pos(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    *,
    client_id: uuid.UUID | None = None,
    status: str | None = None,
    expiring_within_days: int | None = None,
    over_pct: int | None = None,
) -> list[PurchaseOrderOut]:
    stmt = select(PurchaseOrder)
    if client_id is not None:
        stmt = stmt.where(PurchaseOrder.client_id == client_id)
    if status:
        stmt = stmt.where(PurchaseOrder.status == status)
    today = await le_today(db, None)
    if expiring_within_days is not None:
        stmt = stmt.where(
            PurchaseOrder.status == "active",
            PurchaseOrder.valid_to >= today,
            PurchaseOrder.valid_to <= today + timedelta(days=expiring_within_days),
        )
    rows = list((await db.execute(stmt.order_by(PurchaseOrder.valid_to.desc()).limit(500))).scalars())
    used = await consumed_many(db, [r.id for r in rows])
    out = [await _out(db, r, used.get(r.id, (ZERO, ZERO)), today) for r in rows]
    if over_pct is not None:
        out = [o for o in out if o.pct_used is not None and o.pct_used >= over_pct]
    return out


async def update_po(
    ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID, data: PurchaseOrderUpdate
) -> PurchaseOrderOut:
    po = await get_row(db, po_id, lock=True)
    if po.status == "cancelled":
        raise conflict("po_cancelled", "This PO is cancelled")
    changes = data.model_dump(exclude_unset=True)
    amount, hours = await consumed(db, po.id)
    if "value_cap" in changes and changes["value_cap"] is not None:
        changes["value_cap"] = quantize_amount(changes["value_cap"], po.currency)
        if changes["value_cap"] < amount:
            raise conflict(
                "po_cap_below_consumed",
                "The value cap cannot go below what is already consumed",
                consumed=str(amount),
            )
    if "hours_cap" in changes and changes["hours_cap"] is not None:
        changes["hours_cap"] = quantize_hours(changes["hours_cap"])
        if changes["hours_cap"] < hours:
            raise conflict(
                "po_cap_below_consumed",
                "The hours cap cannot go below what is already consumed",
                consumed=str(hours),
            )
    value_cap = changes.get("value_cap", po.value_cap)
    hours_cap = changes.get("hours_cap", po.hours_cap)
    if value_cap is None and hours_cap is None:
        raise invalid("po_cap_required", "Give a value cap, an hours cap or both")
    vf, vt = changes.get("valid_from") or po.valid_from, changes.get("valid_to") or po.valid_to
    if vt < vf:
        raise invalid("po_validity_invalid", "The PO must end on or after its start date")
    if "contract_id" in changes:
        await _check_contract(db, ctx, changes["contract_id"], po.client_id, po.legal_entity_id)
    for key, value in changes.items():
        if value is None and key in ("valid_from", "valid_to"):
            continue
        setattr(po, key, value)
    await db.flush()
    await db.refresh(po)
    return await _out(db, po)


async def _emit_validity(db: AsyncSession, po: PurchaseOrder, status: str) -> None:
    amount, hours = await consumed(db, po.id)
    rem_a, _ = remaining(po, amount, hours)
    emit(
        db,
        PoValidityChanged(
            tenant_id=po.tenant_id,
            po_id=po.id,
            client_id=po.client_id,
            status=status,
            valid_from=po.valid_from,
            valid_to=po.valid_to,
            remaining_amount=str(rem_a) if rem_a is not None else None,
        ),
    )


async def close_po(ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID) -> PurchaseOrderOut:
    po = await get_row(db, po_id, lock=True)
    if po.status != "active":
        raise conflict("po_not_open", "Only open POs can be closed", status=po.status)
    po.status = "closed"
    po.closed_at = now()
    await db.flush()
    await _emit_validity(db, po, "closed")
    await timeline(ctx, db, "client", po.client_id, f"PO {po.po_number} closed", {"po_id": str(po.id)})
    await db.refresh(po)
    return await _out(db, po)


async def cancel_po(ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID) -> PurchaseOrderOut:
    po = await get_row(db, po_id, lock=True)
    if po.status == "cancelled":
        return await _out(db, po)
    has_rows = (
        await db.execute(select(PoConsumption.id).where(PoConsumption.po_id == po.id).limit(1))
    ).first()
    if has_rows is not None:
        raise conflict("po_has_consumption", "A PO with consumption cannot be cancelled; close it instead")
    po.status = "cancelled"
    po.closed_at = now()
    await db.flush()
    await _emit_validity(db, po, "cancelled")
    await timeline(ctx, db, "client", po.client_id, f"PO {po.po_number} cancelled", {"po_id": str(po.id)})
    await db.refresh(po)
    return await _out(db, po)


async def attach_file(
    ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID, data: bytes, filename: str | None
) -> PurchaseOrderOut:
    from app.services import files_service

    po = await get_row(db, po_id, lock=True)
    stored = await files_service.store_upload(ctx, db, data, filename, {"pdf", "docx"}, ENTITY, po.id)
    po.file_id = stored.id
    await db.flush()
    await db.refresh(po)
    return await _out(db, po)


async def list_consumptions(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    po_id: uuid.UUID,
) -> list[PoConsumptionOut]:
    await get_row(db, po_id)
    rows = (
        await db.execute(
            select(PoConsumption)
            .where(PoConsumption.po_id == po_id)
            .order_by(PoConsumption.recorded_at.desc())
        )
    ).scalars()
    return [PoConsumptionOut.model_validate(r) for r in rows]


async def list_placements(ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID) -> list[PoPlacementOut]:  # ruff: ignore[unused-function-argument]
    """Read-only projection of ``placements.po_id`` (the column arrives with the placements epic)."""
    await get_row(db, po_id)
    try:
        async with db.begin_nested():
            rows = (
                await db.execute(
                    text(
                        "SELECT id, human_id, status FROM placements WHERE po_id = :po ORDER BY created_at DESC"  # ruff: ignore[line-too-long]
                    ),
                    {"po": po_id},
                )
            ).all()
    except Exception:
        return []
    return [PoPlacementOut(placement_id=r[0], human_id=r[1], status=r[2]) for r in rows]


# ================================================================ read API (placements / billing / extensions)  # ruff: ignore[line-too-long]
async def po_balance(ctx: RequestContext, db: AsyncSession, po_id: uuid.UUID) -> PoBalance:  # ruff: ignore[unused-function-argument]
    po = await get_row(db, po_id)
    amount, hours = await consumed(db, po.id)
    return balance(po, amount, hours)


def _check(
    po: PurchaseOrder,
    start: date,
    end: date,
    amount: Decimal | None,
    hours: Decimal | None,
    used: tuple[Decimal, Decimal],
) -> PoValidity:
    rem_a, rem_h = remaining(po, *used)

    def out(valid: bool, reason: str) -> PoValidity:
        return PoValidity(valid=valid, reason=reason, remaining_amount=rem_a, remaining_hours=rem_h)

    if po.status == "cancelled":
        return out(False, "po_cancelled")
    if po.status == "closed" or start < po.valid_from or end > po.valid_to:
        return out(False, "po_expired")
    if (rem_a is not None and rem_a <= 0) or (rem_h is not None and rem_h <= 0):
        return out(False, "po_exhausted")
    if (amount is not None and rem_a is not None and amount > rem_a) or (
        hours is not None and rem_h is not None and hours > rem_h
    ):
        return out(False, "po_insufficient")
    return out(True, "ok")


async def validate_po(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    po_id: uuid.UUID | None,
    on_date: date,
    amount: Decimal | None = None,
    hours: Decimal | None = None,
    period_end: date | None = None,
) -> PoValidity:
    if po_id is None:
        return PoValidity(valid=False, reason="po_missing", remaining_amount=None, remaining_hours=None)
    po = (await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))).scalar_one_or_none()
    if po is None:
        return PoValidity(valid=False, reason="po_missing", remaining_amount=None, remaining_hours=None)
    return _check(po, on_date, period_end or on_date, amount, hours, await consumed(db, po.id))


async def default_po(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    client_id: uuid.UUID,
    legal_entity_id: uuid.UUID,
    contract_id: uuid.UUID | None,
    on_date: date,
) -> PoRef | None:
    stmt = select(PurchaseOrder).where(
        PurchaseOrder.client_id == client_id,
        PurchaseOrder.legal_entity_id == legal_entity_id,
        PurchaseOrder.status == "active",
        PurchaseOrder.valid_from <= on_date,
        PurchaseOrder.valid_to >= on_date,
    )
    if contract_id is not None:
        stmt = stmt.where(PurchaseOrder.contract_id == contract_id)
    rows = list(
        (
            await db.execute(stmt.order_by(PurchaseOrder.valid_from.desc(), PurchaseOrder.created_at.desc()))
        ).scalars()
    )
    used = await consumed_many(db, [r.id for r in rows])
    for po in rows:
        rem_a, rem_h = remaining(po, *used.get(po.id, (ZERO, ZERO)))
        if (rem_a is None or rem_a > 0) and (rem_h is None or rem_h > 0):
            return PoRef(
                po_id=po.id,
                po_number=po.po_number,
                currency=po.currency,
                valid_from=po.valid_from,
                valid_to=po.valid_to,
                remaining_amount=rem_a,
                remaining_hours=rem_h,
            )
    return None


async def check_po_headroom(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    po_id: uuid.UUID,
    amount: Decimal,
    hours: Decimal | None,
    currency: str,
) -> PoValidity:
    """No lock (Invoice Run previews)."""
    po = (await db.execute(select(PurchaseOrder).where(PurchaseOrder.id == po_id))).scalar_one_or_none()
    if po is None:
        return PoValidity(valid=False, reason="po_missing", remaining_amount=None, remaining_hours=None)
    if currency.upper() != po.currency:
        raise conflict("po_currency_mismatch", "The PO is in another currency", po_currency=po.currency)
    used = await consumed(db, po.id)
    rem_a, rem_h = remaining(po, *used)
    if po.status == "cancelled":
        return PoValidity(valid=False, reason="po_cancelled", remaining_amount=rem_a, remaining_hours=rem_h)
    if (amount > 0 and rem_a is not None and amount > rem_a) or (
        hours and hours > 0 and rem_h is not None and hours > rem_h
    ):
        return PoValidity(
            valid=False, reason="po_insufficient", remaining_amount=rem_a, remaining_hours=rem_h
        )
    return PoValidity(valid=True, reason="ok", remaining_amount=rem_a, remaining_hours=rem_h)
