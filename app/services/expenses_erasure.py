"""Expenses erasure handler ``expenses.claims`` (Epic 27 A6, AD-34).

Claims, lines and line decisions are financial records (``financial`` class): amounts, dates, ids and the
Worker's ``description`` / ``justification`` are kept with the record as financial evidence while referenced
by a posting. Unsubmitted drafts (never approved, never posted) are deleted; receipts follow the ``files``
retention class (``files.candidate``). Rows listed in ``ctx.retained`` are left alone.
"""

from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expenses import ExpenseClaim
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class ExpensesHandler:
    name = "expenses.claims"
    order = 58
    tables = ("expense_claims", "expense_lines", "expense_line_decisions")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        retained = ctx.retained.get("expense_claims", set())
        drafts = [
            c
            for c in (
                await db.execute(
                    select(ExpenseClaim.id).where(
                        ExpenseClaim.candidate_id.in_(ids), ExpenseClaim.status == "draft"
                    )
                )
            ).scalars()
            if c not in retained
        ]
        if drafts:
            await db.execute(
                delete(ExpenseClaim)
                .where(ExpenseClaim.id.in_(drafts))
                .execution_options(synchronize_session=False)
            )
        return {"draft_claims_deleted": len(drafts)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = list(
            (
                await db.execute(select(ExpenseClaim).where(ExpenseClaim.candidate_id == ctx.candidate_id))
            ).scalars()
        )
        if not rows:
            return None
        return {
            "expense_claims": [
                {
                    "reference": c.human_id,
                    "title": c.title,
                    "status": c.status,
                    "currency": c.currency,
                    "total_claimed": str(c.total_claimed),
                    "total_approved": str(c.total_approved),
                }
                for c in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        del db, ctx
        return []


register_erasure_handler(ExpensesHandler())
