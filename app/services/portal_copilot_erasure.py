# ruff: file-ignore[line-too-long]
"""Erasure handler ``portal_copilot.candidate`` (Epic 77 A7, order 72).

* Candidate subject: deletes the Candidate's own portal Copilot conversations, messages and feedback (rows whose
  ``candidate_id`` is the subject, including merged ids).
* Client User / Vendor User subject (the external-user erasure path, Epic 35): deletes that user's conversations.
``agent_runs`` metadata is kept (P4 rule). ``verify()`` returns the remaining ids (``[]`` = clean). A top-level module
(``load_erasure_handlers``), so privacy never imports the portal Copilot."""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.portal_copilot import PortalCopilotConversation, PortalCopilotFeedback, PortalCopilotMessage
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

TABLES = ("portal_copilot_conversations", "portal_copilot_messages", "portal_copilot_feedback")


async def _conversation_ids(db: AsyncSession, ctx: ErasureContext) -> list[Any]:
    if ctx.subject_kind == "candidate":
        ids = await subject_ids(db, ctx.candidate_id)
        stmt = select(PortalCopilotConversation.id).where(PortalCopilotConversation.candidate_id.in_(ids))
    elif ctx.subject_kind in ("client_user", "vendor_user"):
        stmt = select(PortalCopilotConversation.id).where(
            PortalCopilotConversation.principal_type == ctx.subject_kind,
            PortalCopilotConversation.principal_id == ctx.subject_id,
        )
    else:
        return []
    return list((await db.execute(stmt)).scalars())


class PortalCopilotHandler:
    name = "portal_copilot.candidate"
    order = 72
    tables = TABLES

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        conv_ids = await _conversation_ids(db, ctx)
        counts = dict.fromkeys(TABLES, 0)
        if not conv_ids:
            return counts
        for model, table in ((PortalCopilotMessage, TABLES[1]), (PortalCopilotFeedback, TABLES[2])):
            counts[table] = len(
                list(
                    (await db.execute(select(model.id).where(model.conversation_id.in_(conv_ids)))).scalars()
                )
            )
        res = await db.execute(
            delete(PortalCopilotConversation).where(PortalCopilotConversation.id.in_(conv_ids))
        )  # messages / feedback cascade
        counts[TABLES[0]] = int(res.rowcount or 0)
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # ruff: ignore[unused-method-argument]  # NOSONAR
        # Sealed private assistant conversations; the Candidate reads them in the portal panel.
        return None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        left = await _conversation_ids(db, ctx)
        if ctx.subject_kind == "candidate":
            ids = await subject_ids(db, ctx.candidate_id)
            for model in (PortalCopilotMessage, PortalCopilotFeedback):
                left += list(
                    (await db.execute(select(model.id).where(model.candidate_id.in_(ids)))).scalars()
                )
        return [str(i) for i in left]


register_erasure_handler(PortalCopilotHandler())
