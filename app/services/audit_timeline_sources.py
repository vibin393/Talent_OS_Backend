"""Fallback record-timeline sources and actor resolvers (Epic 35 story 35.5, A6).

The owning domains (billing, receivables, external_auth) may register their own from ``hooks.py``; until they
do, this module registers them by reading the owners' **tables** only (models, never their services), so the
timeline and actor display work without new edges in the domain DAG. Registration is idempotent.

* ``invoice``: credit / debit notes on it, payments allocated to it (and the allocations), TDS deductions,
  write-off requests and write-offs, disputes and collection activities. Invoice lines, e-invoice details
  (IRN columns) and delivery status are columns / children audited on the invoice row itself.
* Actor display names: ``user`` (users.name), ``client_user`` / ``vendor_user`` (``display_name``).
"""

import uuid
from collections.abc import Awaitable, Callable, Iterable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit

Pair = tuple[str, Any]


async def _pairs(db: AsyncSession, entity_type: str, stmt: Any) -> list[Pair]:
    return [(entity_type, row_id) for row_id in (await db.execute(stmt)).scalars().all()]


async def invoice_billing_sources(db: AsyncSession, invoice_id: uuid.UUID) -> list[Pair]:
    try:
        from app.models.billing import CreditNote, DebitNote
    except ImportError:  # billing not merged
        return []
    out = await _pairs(
        db, "credit_note", select(CreditNote.id).where(CreditNote.original_invoice_id == invoice_id)
    )
    out += await _pairs(
        db, "debit_note", select(DebitNote.id).where(DebitNote.original_invoice_id == invoice_id)
    )
    return out


async def invoice_receivables_sources(db: AsyncSession, invoice_id: uuid.UUID) -> list[Pair]:
    try:
        from app.models.receivables import (
            Allocation,
            CollectionActivity,
            Dispute,
            TdsDeduction,
            WriteOff,
            WriteOffRequest,
        )
    except ImportError:  # receivables not merged
        return []
    allocations = (
        await db.execute(
            select(Allocation.id, Allocation.payment_id).where(Allocation.invoice_id == invoice_id)
        )
    ).all()
    out: list[Pair] = [("allocation", a.id) for a in allocations]
    out += [("payment", pid) for pid in dict.fromkeys(a.payment_id for a in allocations if a.payment_id)]
    out += await _pairs(
        db, "tds_deduction", select(TdsDeduction.id).where(TdsDeduction.invoice_id == invoice_id)
    )
    out += await _pairs(
        db, "write_off_request", select(WriteOffRequest.id).where(WriteOffRequest.invoice_id == invoice_id)
    )
    out += await _pairs(db, "write_off", select(WriteOff.id).where(WriteOff.invoice_id == invoice_id))
    out += await _pairs(db, "dispute", select(Dispute.id).where(Dispute.invoice_id == invoice_id))
    out += await _pairs(
        db,
        "collection_activity",
        select(CollectionActivity.id).where(
            CollectionActivity.document_kind == "invoice", CollectionActivity.document_id == invoice_id
        ),
    )
    return out


def _name_resolver(
    model_path: str, class_name: str, column: str
) -> Callable[..., Awaitable[dict[uuid.UUID, str]]]:
    async def resolve(db: AsyncSession, ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        import importlib

        try:
            model = getattr(importlib.import_module(model_path), class_name)
        except (ImportError, AttributeError):
            return {}
        rows: Iterable[Any] = (
            await db.execute(select(model.id, getattr(model, column)).where(model.id.in_(ids)))
        ).all()
        return {row[0]: str(row[1]) for row in rows if row[1]}

    resolve.__name__ = f"resolve_{class_name.lower()}_names"
    return resolve


def register() -> None:
    audit.register_timeline_source("invoice", invoice_billing_sources)
    audit.register_timeline_source("invoice", invoice_receivables_sources)
    for kind, (path, cls, col) in {
        "user": ("app.models.identity", "User", "name"),
        "client_user": ("app.models.external_auth", "ClientUser", "display_name"),
        "vendor_user": ("app.models.external_auth", "VendorUser", "display_name"),
    }.items():
        if audit.actor_resolver(kind) is None:
            audit.register_actor_resolver(kind, _name_resolver(path, cls, col))


register()
