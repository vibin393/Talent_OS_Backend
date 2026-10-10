# ruff: noqa: E501
"""Erasure handler ``copilot.messages`` (Epic 54, order 66, after the foundation's 62-64; FR-279 / FR-326).

For a candidate subject: every Copilot message whose ``candidate_ids`` contains the candidate, or whose turn run's
``agent_runs.candidate_ids`` contains it, becomes ``{"redacted": true, "role": <role>}`` with ``redacted_at``; the
affected conversations' ``title`` becomes "Redacted conversation" and their ``context_summary`` / ``context_ref`` are
cleared. A top-level module (``load_erasure_handlers``), so privacy never imports the Copilot."""

import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent_runtime import AgentRun
from app.models.copilot import CopilotConversation, CopilotMessage
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

REDACTED_TITLE = "Redacted conversation"


async def _message_ids(db: AsyncSession, ids: list[uuid.UUID]) -> list[tuple[uuid.UUID, uuid.UUID]]:
    runs = select(AgentRun.id).where(AgentRun.origin == "copilot", AgentRun.candidate_ids.overlap(ids))
    rows = await db.execute(
        select(CopilotMessage.id, CopilotMessage.conversation_id).where(
            or_(CopilotMessage.candidate_ids.overlap(ids), CopilotMessage.run_id.in_(runs))
        )
    )
    return [(r[0], r[1]) for r in rows]


class CopilotMessagesHandler:
    name = "copilot.messages"
    order = 66
    tables = ("copilot_messages", "copilot_conversations")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"copilot_messages": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        found = await _message_ids(db, ids)
        if not found:
            return {"copilot_messages": 0, "copilot_conversations": 0}
        now = datetime.now(UTC)
        msg_rows = (
            await db.execute(select(CopilotMessage).where(CopilotMessage.id.in_([m for m, _ in found])))
        ).scalars()
        count = 0
        for msg in msg_rows:
            msg.content = {"redacted": True, "role": msg.role}
            msg.redacted_at = now
            count += 1
        conv_ids = sorted({c for _, c in found})
        await db.execute(
            update(CopilotConversation)
            .where(CopilotConversation.id.in_(conv_ids))
            .values(title=REDACTED_TITLE, title_locked=True, context_summary=None, context_ref=None)
        )
        await db.flush()
        return {"copilot_messages": count, "copilot_conversations": len(conv_ids)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:  # noqa: ARG002
        # Copilot conversations are the user's private working notes, not records about the candidate.
        return None

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        found = await _message_ids(db, ids)
        if not found:
            return []
        left = (
            await db.execute(
                select(CopilotMessage.id)
                .where(CopilotMessage.id.in_([m for m, _ in found]), CopilotMessage.redacted_at.is_(None))
                .limit(1)
            )
        ).first()
        return ["copilot messages about the candidate remain readable"] if left else []


register_erasure_handler(CopilotMessagesHandler())
