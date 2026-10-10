# ruff: noqa: E501
"""Invoice / Client collection timelines (Epic 63 A4, FR-315 on top of P2 FR-183).

``register_timeline_source("invoice", ...)`` and ``("client", ...)``: the subject's record timeline merges the audit rows
of its ``collection_agent_log`` entries ("Sent by Collections Agent", "Drafted by Collections Agent -- awaiting approval
(PRP-...)", "Approved by {user}", "Paused by {user}" ...). The labelled list itself is ``log.list_log``.
"""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import audit
from app.models.collection_rules import CollectionAgentLog

LIMIT = 500


async def invoice_sources(db: AsyncSession, invoice_id: uuid.UUID) -> list[tuple[str, uuid.UUID]]:
    rows = (
        await db.execute(
            select(CollectionAgentLog.id).where(CollectionAgentLog.document_ids.contains([invoice_id])).limit(LIMIT)
        )
    ).scalars()
    return [("collection_agent_log", r) for r in rows]


async def client_sources(db: AsyncSession, client_id: uuid.UUID) -> list[tuple[str, uuid.UUID]]:
    rows = (
        await db.execute(select(CollectionAgentLog.id).where(CollectionAgentLog.client_id == client_id).limit(LIMIT))
    ).scalars()
    return [("collection_agent_log", r) for r in rows]


def register() -> None:
    audit.register_timeline_source("invoice", invoice_sources)
    audit.register_timeline_source("debit_note", invoice_sources)
    audit.register_timeline_source("client", client_sources)


__all__ = ["client_sources", "invoice_sources", "register"]
