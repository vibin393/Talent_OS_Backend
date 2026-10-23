# ruff: file-ignore[non-imperative-mood, line-too-long, typing-only-standard-library-import]
"""Delete / purge Copilot conversations (Epic 54, FR-279): discard pending drafts (as the owner), purge the turns' run
content through the runtime facade (C-2), then delete the rows (messages cascade; feedback survives)."""

import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.context import RequestContext
from app.core.permissions import Principal
from app.models.copilot import CopilotConversation
from app.services.copilot_store import conversations, messages, runs_bridge


def owner_context(conv: CopilotConversation) -> RequestContext:
    """A human context of the conversation's owner (drafts are discarded as their owner; content only)."""
    return RequestContext(
        tenant_id=conv.tenant_id, principal=Principal(type="user", id=conv.user_id, tenant_id=conv.tenant_id)
    )


async def discard_conversation_drafts(db: AsyncSession, conv: CopilotConversation) -> int:
    ids = await messages.draft_ids(db, [conv.id])
    return await runs_bridge.discard_drafts(owner_context(conv), db, ids)


async def remove(db: AsyncSession, convs: Sequence[CopilotConversation]) -> dict[str, int]:
    """Discard drafts, purge run content, delete rows. Returns counts."""
    drafts = 0
    for conv in convs:
        drafts += await discard_conversation_drafts(db, conv)
    ids: list[uuid.UUID] = [c.id for c in convs]
    run_ids = await messages.run_ids(db, ids)
    # System context (no principal): allowed by ``purge_run_content`` for retention / deletion jobs.
    payloads = await runs_bridge.purge(
        RequestContext(tenant_id=convs[0].tenant_id) if convs else RequestContext(), db, run_ids
    )
    deleted = await conversations.delete_rows(db, ids)
    return {"conversations": deleted, "runs": len(run_ids), "payloads": payloads, "drafts": drafts}


async def purge_expired(db: AsyncSession, *, batch: int = 500) -> int:
    total = 0
    while True:
        rows = await conversations.expired(db, limit=batch)
        if not rows:
            return total
        out = await remove(db, rows)
        total += out["conversations"]
        if len(rows) < batch:
            return total


__all__ = ["discard_conversation_drafts", "owner_context", "purge_expired", "remove"]
