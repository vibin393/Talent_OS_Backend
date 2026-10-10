"""Payroll export erasure handler ``payroll.exports`` (Epic 28 A8, AD-34).

Export rows / items are financial records (``financial`` retention class): they hold ids, amounts and only the
last 4 characters of PAN / bank account -- never full identity data. Erasure leaves them in place and logs the
exclusion; the Worker's name is not stored (files read it live, so an erased name no longer appears).
"""

from typing import Any

from loguru import logger
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.payroll import PayrollExportRow
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class PayrollExportsHandler:
    name = "payroll.exports"
    order = 59
    tables: tuple[str, ...] = ("payroll_export_rows",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        n = (
            await db.execute(
                select(func.count())
                .select_from(PayrollExportRow)
                .where(PayrollExportRow.candidate_id == ctx.candidate_id)
            )
        ).scalar_one()
        if n:
            logger.info("payroll export rows retained as financial records (erasure excluded): {}", n)
        return {"payroll_rows_retained": int(n)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = list(
            (
                await db.execute(
                    select(PayrollExportRow).where(PayrollExportRow.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not rows:
            return None
        return {
            "payroll_export_rows": [
                {
                    "currency": r.currency,
                    "gross_earnings_inputs": str(r.gross_earnings_inputs),
                    "reimbursements_total": str(r.reimbursements_total),
                }
                for r in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        del db, ctx
        return []


register_erasure_handler(PayrollExportsHandler())
