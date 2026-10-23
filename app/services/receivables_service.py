# ruff: file-ignore[line-too-long]
"""Facade of the ``receivables`` domain (Epic 13 contract layer; filled by Epic 30).

Epic 63 (additive): the Collections Agent's read contracts (``agent_reads``) and the human-driven P2
collections functions it wraps (client settings, activities, statements). Payments, write-offs, disputes
and period close stay human-only and are not exported here.

Epic 71 (spec A9, additive): ``record_payment`` -- the ★ confirm path of a ledger payment suggestion records the
Payment as the confirming User. Callers must ``require_human`` first (``ledger_sync.payments.confirm`` does).
"""

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.actors import actor_from_ctx, require_human
from app.core.context import RequestContext
from app.services.receivables.agent_reads import (
    CollectibleDocument,
    client_email_count_since,
    collectible_documents,
    email_activity_since,
    recent_activities,
)
from app.services.receivables.collections import get_client_settings, list_activities, log_activity
from app.services.receivables.service import aging_query, dso, open_balance
from app.services.receivables.statements import build as build_statement


async def record_payment(ctx: RequestContext, db: AsyncSession, data: object) -> object:
    """★ Record a client payment as the calling human (P2 ``receivables.payments.record``; ``PaymentIn``)."""
    require_human(actor_from_ctx(ctx))
    from app.services.receivables import payments

    return await payments.record(ctx, db, data)  # type: ignore[arg-type]


__all__ = [
    "CollectibleDocument",
    "aging_query",
    "build_statement",
    "client_email_count_since",
    "collectible_documents",
    "dso",
    "email_activity_since",
    "get_client_settings",
    "list_activities",
    "log_activity",
    "open_balance",
    "recent_activities",
    "record_payment",
]
