"""Daily PO jobs (Epic 16 A7): expiry alerts at ``po_expiry_alert_days`` and validity events when a PO becomes
valid (``valid_from`` reached) or invalid (``valid_to`` passed). Deduped in ``po_alerts``."""

import uuid
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.events import emit
from app.core.fiscal import local_date
from app.events.commercials import PoExpiring, PoValidityChanged
from app.models.commercials import PoAlert, PurchaseOrder
from app.models.legal_entities import LegalEntity
from app.services.commercials import settings as settings_mod
from app.services.commercials.common import now
from app.services.commercials.purchase_orders import consumed, remaining


async def _once(db: AsyncSession, po: PurchaseOrder, kind: str, threshold: int, seq: int) -> bool:
    created = (
        await db.execute(
            insert(PoAlert)
            .values(
                id=uuid.uuid4(),
                tenant_id=po.tenant_id,
                client_id=po.client_id,
                po_id=po.id,
                kind=kind,
                threshold=threshold,
                crossing_seq=seq,
            )
            .on_conflict_do_nothing(index_elements=["po_id", "kind", "threshold", "crossing_seq"])
            .returning(PoAlert.id)
        )
    ).scalar_one_or_none()
    return created is not None


async def run_daily(ctx: RequestContext, db: AsyncSession, at: datetime | None = None) -> dict[str, int]:  # noqa: ARG001
    settings = await settings_mod.get_row(db)
    rows = (
        await db.execute(
            select(PurchaseOrder, LegalEntity.timezone)
            .join(LegalEntity, LegalEntity.id == PurchaseOrder.legal_entity_id)
            .where(PurchaseOrder.status == "active")
        )
    ).all()
    counts = {"expiring": 0, "valid": 0, "expired": 0}
    for po, tz in rows:
        today = local_date(at or now(), tz or "Asia/Kolkata")
        days_left = (po.valid_to - today).days
        if po.valid_from <= today <= po.valid_to and await _once(
            db, po, "validity", 1, po.valid_from.toordinal()
        ):
            counts["valid"] += 1
            await _validity(db, po, "valid")
        if today > po.valid_to and await _once(db, po, "validity", 0, po.valid_to.toordinal()):
            counts["expired"] += 1
            await _validity(db, po, "expired")
        if 0 <= days_left <= settings.po_expiry_alert_days and await _once(
            db, po, "expiring", settings.po_expiry_alert_days, po.valid_to.toordinal()
        ):
            counts["expiring"] += 1
            emit(
                db,
                PoExpiring(
                    tenant_id=po.tenant_id,
                    po_id=po.id,
                    client_id=po.client_id,
                    valid_to=po.valid_to,
                    days_left=days_left,
                ),
            )
    return counts


async def _validity(db: AsyncSession, po: PurchaseOrder, status: str) -> None:
    rem_a, _ = remaining(po, *(await consumed(db, po.id)))
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
