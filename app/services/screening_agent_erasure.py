# ruff: file-ignore[line-too-long]
"""Erasure handler ``screening_agent.conversations`` (Epic 57, order 66; FR-326).

For a candidate subject (+ merged ids): delete the conversation turns and the extracted answers (text and voice), and
null each conversation's ``summary`` / ``gaps`` / ``flags``. The conversation header (status, timestamps, ids) stays for
the audit trail. Transcripts are erased by the foundation's ``transcripts.records`` handler (order 63).
A top-level module (``load_erasure_handlers``), so privacy never imports the Screening Agent.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.screening_agent import ScreeningAnswer, ScreeningConversation, ScreeningTurn
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class ScreeningConversationsHandler:
    name = "screening_agent.conversations"
    order = 66
    tables = ("screening_conversations", "screening_turns", "screening_answers")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"screening_turns": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        turns = await db.execute(delete(ScreeningTurn).where(ScreeningTurn.candidate_id.in_(ids)))
        answers = await db.execute(delete(ScreeningAnswer).where(ScreeningAnswer.candidate_id.in_(ids)))
        convs = await db.execute(
            update(ScreeningConversation)
            .where(ScreeningConversation.candidate_id.in_(ids))
            .values(summary=None, gaps=[], flags=[])
        )
        await db.flush()
        return {
            "screening_turns": int(getattr(turns, "rowcount", 0) or 0),
            "screening_answers": int(getattr(answers, "rowcount", 0) or 0),
            "screening_conversations": int(getattr(convs, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        convs = (
            await db.execute(
                select(ScreeningConversation).where(ScreeningConversation.candidate_id == ctx.candidate_id)
            )
        ).scalars()
        out = []
        for c in convs:
            turns = (
                await db.execute(
                    select(ScreeningTurn)
                    .where(ScreeningTurn.conversation_id == c.id)
                    .order_by(ScreeningTurn.seq)
                )
            ).scalars()
            out.append(
                {
                    "conversation": c.human_id, "status": c.status, "channel": c.channel, "created_at": c.created_at,
                    "messages": [{"author": t.author, "body": t.body, "at": t.created_at} for t in turns],
                }
            )  # fmt: skip
        return {"screening_conversations": out}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        problems: list[str] = []
        if (
            await db.execute(select(ScreeningTurn.id).where(ScreeningTurn.candidate_id.in_(ids)).limit(1))
        ).first():
            problems.append("screening conversation turns remain")
        if (
            await db.execute(select(ScreeningAnswer.id).where(ScreeningAnswer.candidate_id.in_(ids)).limit(1))
        ).first():
            problems.append("screening answers remain")
        if (
            await db.execute(
                select(ScreeningConversation.id)
                .where(
                    ScreeningConversation.candidate_id.in_(ids), ScreeningConversation.summary.is_not(None)
                )
                .limit(1)
            )
        ).first():
            problems.append("screening conversation summaries remain")
        return problems


register_erasure_handler(ScreeningConversationsHandler())
