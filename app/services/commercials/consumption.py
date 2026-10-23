"""PO consumption ledger (Epic 16 A7, AD-32): ``record_po_consumption`` is the only writer of ``po_consumptions``.

* Idempotent on ``(source_doc_kind, source_doc_id, po_id)``: a repeat returns the current balance.
* Locks the PO ``FOR UPDATE`` (callers already hold their document locks, AD-18 order) and checks the
  remaining balance inside the lock, so concurrent consumers never exceed the cap.
* Refuses another currency (409 ``po_currency_mismatch``) and cancelled POs (409 ``po_cancelled``); positive
  amounts / hours beyond the remaining balance are 409 ``po_insufficient`` unless an approved ``po_exception``
  exists for this PO and document. Credit notes record negative rows and are never refused.
* After the insert every newly crossed alert threshold (upwards) emits ``PoConsumptionThresholdReached`` once
  per crossing (deduped in ``po_alerts``); reaching zero remaining emits ``PoValidityChanged("exhausted")``.
No external I/O happens here.
"""  # ruff: ignore[line-too-long]

import uuid
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import ConflictError
from app.core.events import emit
from app.core.money import quantize_amount, quantize_hours
from app.events.commercials import PoConsumptionThresholdReached, PoValidityChanged
from app.models.approvals import ApprovalRequest
from app.models.commercials import PoAlert, PoConsumption, PurchaseOrder
from app.schemas.commercials import PoBalance
from app.services.commercials import settings as settings_mod
from app.services.commercials.common import conflict, tenant_of
from app.services.commercials.purchase_orders import ZERO, balance, consumed, get_row, pct_used, remaining

APPROVAL_SUBJECT = "po_exception"
_NAMESPACE = uuid.UUID("6f1c2d55-8a43-4f0e-9d1b-1c6c0e7d2a16")


def exception_subject_id(po_id: uuid.UUID, source_doc_kind: str, source_doc_id: uuid.UUID) -> uuid.UUID:
    """Deterministic approval subject of a PO exception (one per PO and document)."""
    return uuid.uuid5(_NAMESPACE, f"{po_id}:{source_doc_kind}:{source_doc_id}")


async def approved_exception(
    db: AsyncSession, po_id: uuid.UUID, source_doc_kind: str, source_doc_id: uuid.UUID
) -> uuid.UUID | None:
    return (
        await db.execute(
            select(ApprovalRequest.id)
            .where(
                ApprovalRequest.subject_type == APPROVAL_SUBJECT,
                ApprovalRequest.subject_id == exception_subject_id(po_id, source_doc_kind, source_doc_id),
                ApprovalRequest.status == "approved",
            )
            .order_by(ApprovalRequest.completed_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()


def _insufficient(po: PurchaseOrder, rem_a: Decimal | None, rem_h: Decimal | None) -> ConflictError:
    return conflict(
        "po_insufficient",
        "The PO does not have enough balance left",
        po_id=str(po.id),
        remaining_amount=str(rem_a) if rem_a is not None else None,
        remaining_hours=str(rem_h) if rem_h is not None else None,
    )


async def record_po_consumption(
    ctx: RequestContext,  # ruff: ignore[unused-function-argument]
    db: AsyncSession,
    po_id: uuid.UUID,
    source_doc_kind: str,
    source_doc_id: uuid.UUID,
    amount: Decimal,
    hours: Decimal | None,
    currency: str,
) -> PoBalance:
    if source_doc_kind not in ("invoice", "credit_note", "debit_note"):
        raise ValueError(f"unknown source_doc_kind {source_doc_kind!r}")
    po = await get_row(db, po_id, lock=True)
    existing = (
        await db.execute(
            select(PoConsumption.id).where(
                PoConsumption.po_id == po.id,
                PoConsumption.source_doc_kind == source_doc_kind,
                PoConsumption.source_doc_id == source_doc_id,
            )
        )
    ).first()
    before_a, before_h = await consumed(db, po.id)
    if existing is not None:
        return balance(po, before_a, before_h)
    if currency.upper() != po.currency:
        raise conflict(
            "po_currency_mismatch",
            "The document currency differs from the PO currency",
            po_currency=po.currency,
            currency=currency,
        )
    if po.status == "cancelled":
        raise conflict("po_cancelled", "This PO is cancelled", po_id=str(po.id))
    amt = quantize_amount(Decimal(amount), po.currency)
    hrs = quantize_hours(Decimal(hours)) if hours is not None else ZERO
    exception_id: uuid.UUID | None = None
    rem_a, rem_h = remaining(po, before_a, before_h)
    short = (amt > 0 and rem_a is not None and amt > rem_a) or (hrs > 0 and rem_h is not None and hrs > rem_h)
    if short:
        exception_id = await approved_exception(db, po.id, source_doc_kind, source_doc_id)
        if exception_id is None:
            raise _insufficient(po, rem_a, rem_h)
    db.add(
        PoConsumption(
            id=uuid.uuid4(),
            tenant_id=tenant_of(db),
            client_id=po.client_id,
            po_id=po.id,
            source_doc_kind=source_doc_kind,
            source_doc_id=source_doc_id,
            amount=amt,
            hours=hrs,
            currency=po.currency,
            exception_approval_id=exception_id,
        )
    )
    await db.flush()
    after_a, after_h = before_a + amt, before_h + hrs
    await _alerts(db, po, before_a, before_h, after_a, after_h)
    return balance(po, after_a, after_h)


async def _alerts(
    db: AsyncSession,
    po: PurchaseOrder,
    before_a: Decimal,
    before_h: Decimal,
    after_a: Decimal,
    after_h: Decimal,
) -> None:
    settings = await settings_mod.get_row(db)
    thresholds = sorted(set(po.alert_thresholds_pct or settings.po_alert_thresholds_pct))
    before = pct_used(po, before_a, before_h) or ZERO
    after = pct_used(po, after_a, after_h) or ZERO
    for threshold in thresholds:
        if not (before < threshold <= after):
            continue
        seq = (
            await db.execute(
                select(func.count())
                .select_from(PoAlert)
                .where(PoAlert.po_id == po.id, PoAlert.kind == "threshold", PoAlert.threshold == threshold)
            )
        ).scalar_one() + 1
        created = (
            await db.execute(
                insert(PoAlert)
                .values(
                    id=uuid.uuid4(),
                    tenant_id=po.tenant_id,
                    client_id=po.client_id,
                    po_id=po.id,
                    kind="threshold",
                    threshold=threshold,
                    crossing_seq=seq,
                )
                .on_conflict_do_nothing(index_elements=["po_id", "kind", "threshold", "crossing_seq"])
                .returning(PoAlert.id)
            )
        ).scalar_one_or_none()
        if created is not None:
            emit(
                db,
                PoConsumptionThresholdReached(
                    tenant_id=po.tenant_id, po_id=po.id, client_id=po.client_id, pct=threshold
                ),
            )
    rem_before = remaining(po, before_a, before_h)
    rem_after = remaining(po, after_a, after_h)

    def exhausted(rem: tuple[Decimal | None, Decimal | None]) -> bool:
        return any(r is not None and r <= 0 for r in rem)

    if exhausted(rem_after) and not exhausted(rem_before):
        emit(
            db,
            PoValidityChanged(
                tenant_id=po.tenant_id,
                po_id=po.id,
                client_id=po.client_id,
                status="exhausted",
                valid_from=po.valid_from,
                valid_to=po.valid_to,
                remaining_amount=str(rem_after[0]) if rem_after[0] is not None else None,
            ),
        )


async def request_po_exception(
    ctx: RequestContext,
    db: AsyncSession,
    po_id: uuid.UUID,
    source_doc_kind: str,
    source_doc_id: uuid.UUID,
    amount: Decimal,
    hours: Decimal | None,
    reason: str,
) -> uuid.UUID:
    """Start (human-only) approval of consuming beyond the PO balance for one document."""
    from app.services import approvals
    from app.services.approvals import StepSpec

    po = await get_row(db, po_id)
    settings = await settings_mod.get_row(db)
    steps = [
        StepSpec(
            name=s.get("name") or "Approval",
            approver_kind=s["approver_kind"],
            approver_ref=s.get("approver_ref") or {},
        )
        for s in settings.po_exception_steps
    ]
    used = await consumed(db, po.id)
    rem_a, rem_h = remaining(po, *used)
    facts = {
        "po_id": str(po.id),
        "po_number": po.po_number,
        "client_id": str(po.client_id),
        "source_doc_kind": source_doc_kind,
        "source_doc_id": str(source_doc_id),
        "amount": str(amount),
        "hours": str(hours) if hours is not None else None,
        "currency": po.currency,
        "remaining_amount": str(rem_a) if rem_a is not None else None,
        "remaining_hours": str(rem_h) if rem_h is not None else None,
        "reason": reason[:500],
    }
    result = await approvals.start(
        ctx,
        db,
        APPROVAL_SUBJECT,
        exception_subject_id(po.id, source_doc_kind, source_doc_id),
        facts,
        steps=steps,
    )
    if result.request_id is None:  # pragma: no cover - explicit steps always create a request
        raise conflict("approver_unresolved", "No approver could be resolved")
    return result.request_id


async def on_po_exception_decided(ctx: RequestContext, db: AsyncSession, decision: object) -> None:  # ruff: ignore[unused-function-argument]
    """Nothing to write: ``record_po_consumption`` reads the approved request itself."""
    return
