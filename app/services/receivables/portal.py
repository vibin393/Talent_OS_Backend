# ruff: file-ignore[line-too-long]
"""Client Portal view of receivables: the Client User's own statement and the Payment Status of its documents
(org RLS ``client`` + ``invoices:view``). Another client's id is a 404."""

import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.errors import NotFound
from app.schemas.receivables_portal import PortalPaymentStatus, PortalStatement
from app.services.receivables import settlement, statements
from app.services.receivables.common import documents


def _client_of(ctx: RequestContext, client_id: uuid.UUID | None = None) -> uuid.UUID:
    org = getattr(ctx.principal, "org_id", None)
    if org is None or (client_id is not None and client_id != org):
        raise NotFound("Statement not found")
    return org  # type: ignore[no-any-return]


async def statement(ctx: RequestContext, db: AsyncSession, currency: str, start: date, end: date, client_id: uuid.UUID | None = None) -> PortalStatement:
    cid = _client_of(ctx, client_id)
    out = await statements.build(ctx, db, cid, currency.upper(), start, end)
    return PortalStatement(**out.model_dump())


async def payment_statuses(ctx: RequestContext, db: AsyncSession) -> list[PortalPaymentStatus]:
    cid = _client_of(ctx)
    docs = [d for d in await documents(db, client_id=cid) if d.status == "issued"]
    bals = await settlement.balances(db, docs, with_disputes=False)
    return [PortalPaymentStatus(document_kind=b.doc.kind, document_id=b.doc.id, open_amount=b.open, payment_status=b.status, due_date=b.doc.due_date) for b in bals.values()]
