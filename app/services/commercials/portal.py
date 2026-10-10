"""Client Portal read model (Epic 16 provides, Epic 18 mounts): the signed-in client's PO consumption.

Runs in the client-scoped session (org RLS ``client``), so only that client's POs are visible; the explicit
``client_id`` filter is a second guard.
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import Forbidden
from app.models.commercials import PurchaseOrder
from app.schemas.commercials_portal import ClientPoConsumptionOut
from app.services.commercials.purchase_orders import ZERO, consumed_many, remaining


async def client_po_consumption(ctx: RequestContext, db: AsyncSession) -> list[ClientPoConsumptionOut]:
    principal = ctx.principal
    client_id = getattr(principal, "org_id", None)
    if getattr(principal, "type", None) != "client_user" or client_id is None:
        raise Forbidden("Client portal only")
    rows = list(
        (
            await db.execute(
                select(PurchaseOrder)
                .where(PurchaseOrder.client_id == client_id, PurchaseOrder.status != "cancelled")
                .order_by(PurchaseOrder.valid_to.desc())
            )
        ).scalars()
    )
    used = await consumed_many(db, [r.id for r in rows])
    out: list[ClientPoConsumptionOut] = []
    for po in rows:
        amount, hours = used.get(po.id, (ZERO, ZERO))
        rem_a, rem_h = remaining(po, amount, hours)
        out.append(
            ClientPoConsumptionOut(
                po_number=po.po_number,
                currency=po.currency,
                value_cap=po.value_cap,
                hours_cap=po.hours_cap,
                consumed_amount=amount,
                consumed_hours=hours,
                remaining_amount=rem_a,
                remaining_hours=rem_h,
                valid_to=po.valid_to,
            )
        )
    return out
