# ruff: file-ignore[line-too-long]
"""Read contracts for the Phase 4 Collections Agent (Epic 63, additive; FR-156 stays human-driven).

* ``collectible_documents`` -- a Client's issued invoices / debit notes with a due date and their open and disputed
  amounts (the same ``settlement.balances`` the overdue queue uses; disputed amounts are excluded downstream).
* ``email_activity_since`` -- the latest P2 ``email`` collection activity per document since a date (the agent skips
  documents contacted in the last 3 days).
* ``open_promises`` -- open / recently broken promises to pay of a Client (dates and statuses, no bodies).

Read only: nothing here writes, and nothing here is a human-only transition.
"""

import dataclasses
import uuid
from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.receivables import CollectionActivity
from app.services.receivables import settlement
from app.services.receivables.common import documents, q2


@dataclasses.dataclass(frozen=True, slots=True)
class CollectibleDocument:
    kind: str
    id: uuid.UUID
    number: str | None
    client_id: uuid.UUID
    legal_entity_id: uuid.UUID
    currency: str
    doc_date: date | None
    due_date: date | None
    document_total: Decimal
    open_amount: Decimal
    disputed_amount: Decimal
    status: str
    written_off: bool


async def collectible_documents(
    db: AsyncSession, client_id: uuid.UUID, *, ids: list[uuid.UUID] | None = None
) -> list[CollectibleDocument]:
    docs = [d for d in await documents(db, client_id=client_id, ids=ids) if d.due_date is not None]
    bals = await settlement.balances(db, docs)
    out = []
    for b in bals.values():
        d = b.doc
        out.append(
            CollectibleDocument(
                kind=d.kind, id=d.id, number=d.number, client_id=d.client_id, legal_entity_id=d.legal_entity_id,
                currency=d.currency, doc_date=d.doc_date, due_date=d.due_date, document_total=q2(d.document_total),
                open_amount=q2(b.open), disputed_amount=q2(b.disputed), status=d.status, written_off=b.written_off,
            )
        )  # fmt: skip
    return sorted(out, key=lambda c: (c.due_date or date.max, c.number or ""))


async def email_activity_since(db: AsyncSession, client_id: uuid.UUID, since: datetime) -> dict[uuid.UUID, datetime]:
    rows = (
        await db.execute(
            select(CollectionActivity.document_id, func.max(CollectionActivity.occurred_at))
            .where(
                CollectionActivity.client_id == client_id, CollectionActivity.kind == "email",
                CollectionActivity.occurred_at >= since, CollectionActivity.document_id.is_not(None),
            )
            .group_by(CollectionActivity.document_id)
        )
    ).all()  # fmt: skip
    return {r[0]: r[1] for r in rows}


async def client_email_count_since(db: AsyncSession, client_id: uuid.UUID, since: datetime) -> int:
    return int(
        (
            await db.execute(
                select(func.count())
                .select_from(CollectionActivity)
                .where(
                    CollectionActivity.client_id == client_id, CollectionActivity.kind == "email",
                    CollectionActivity.occurred_at >= since,
                )
            )
        ).scalar_one()
    )  # fmt: skip


async def recent_activities(db: AsyncSession, client_id: uuid.UUID, limit: int = 10) -> list[CollectionActivity]:
    return list(
        (
            await db.execute(
                select(CollectionActivity)
                .where(CollectionActivity.client_id == client_id)
                .order_by(CollectionActivity.occurred_at.desc())
                .limit(limit)
            )
        ).scalars()
    )


__all__ = [
    "CollectibleDocument",
    "client_email_count_since",
    "collectible_documents",
    "email_activity_since",
    "recent_activities",
]
