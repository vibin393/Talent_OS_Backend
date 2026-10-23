# ruff: file-ignore[line-too-long]
"""Erasure handler ``knowledge.onboarding_qa`` (Epic 62, order 66; FR-326).

For a candidate subject (+ merged ids): delete the Onboarding Q&A messages and null the thread references (consent,
run, task); the thread header (status, timestamps) stays for the audit trail. Knowledge Sources hold no candidate
data. A top-level module, so privacy never imports the ``knowledge`` package.
"""

from typing import Any

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.knowledge import OnboardingQaMessage, OnboardingQaThread
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class OnboardingQaHandler:
    name = "knowledge.onboarding_qa"
    order = 66
    tables = ("onboarding_qa_threads", "onboarding_qa_messages")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"onboarding_qa_messages": 0}
        ids = await subject_ids(db, ctx.candidate_id)
        msgs = await db.execute(delete(OnboardingQaMessage).where(OnboardingQaMessage.candidate_id.in_(ids)))
        threads = await db.execute(
            update(OnboardingQaThread)
            .where(OnboardingQaThread.candidate_id.in_(ids))
            .values(consent_id=None, agent_run_id=None, task_id=None, status="closed")
        )
        await db.flush()
        return {
            "onboarding_qa_messages": int(getattr(msgs, "rowcount", 0) or 0),
            "onboarding_qa_threads": int(getattr(threads, "rowcount", 0) or 0),
        }

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        from app.services.knowledge import (
            sealing,  # Epic 78: the export decrypts through the knowledge sealing helpers
        )

        rows = list(
            (
                await db.execute(
                    select(OnboardingQaMessage)
                    .where(OnboardingQaMessage.candidate_id == ctx.candidate_id)
                    .order_by(OnboardingQaMessage.thread_id, OnboardingQaMessage.seq)
                )
            ).scalars()
        )
        await sealing.open_messages(db, rows, notes=True)
        return {
            "onboarding_questions": [
                {"thread": str(m.thread_id), "role": m.role, "text": m.plain_text or "", "feedback_note": m.plain_feedback_note, "at": m.created_at}
                for m in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        ids = await subject_ids(db, ctx.candidate_id)
        found = (
            await db.execute(
                select(OnboardingQaMessage.id).where(OnboardingQaMessage.candidate_id.in_(ids)).limit(1)
            )
        ).first()
        return ["onboarding Q&A messages remain"] if found else []


register_erasure_handler(OnboardingQaHandler())
