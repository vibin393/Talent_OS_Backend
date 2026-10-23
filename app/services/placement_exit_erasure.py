"""Placement-exit erasure handler ``placements.exit`` (57), Epic 35 coverage gap for Epic 31 (AD-34).

For an erased candidate: free-text fields of their extensions (notes, reasons, client comments), offboarding
cases (reason / cancel notes), offboarding items (notes, waiver reasons, verifier details) and the exit-feedback
answers are cleared. Dates, statuses, rates and ids stay -- they back placement financials (rate segments,
invoices) and are kept for the ``financial`` retention class. Rows in ``ctx.retained`` are left untouched.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.placements_exit import Extension, OffboardingCase, OffboardingFeedback, OffboardingItem
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

_TEXT: dict[Any, dict[str, Any]] = {
    Extension: {"note": None, "exception_reason": None, "client_comment": None, "withdraw_reason": None},
    OffboardingCase: {"reason_note": None, "cancel_reason": None},
    OffboardingItem: {"note": None, "waived_reason": None, "verifier_details": None},
}


def _residual(model: Any) -> Any:
    cols = [getattr(model, c).is_not(None) for c in _TEXT[model]]
    out = cols[0]
    for c in cols[1:]:
        out = out | c
    return out


class PlacementExitHandler:
    name = "placements.exit"
    order = 57
    tables = ("extensions", "offboarding_cases", "offboarding_items", "offboarding_feedback")

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        counts: dict[str, int] = {}
        for model, values in _TEXT.items():
            where = model.candidate_id.in_(ids)
            retained = ctx.retained.get(model.__tablename__, set())
            if retained:
                where = where & model.id.not_in(retained)
            result = await db.execute(
                update(model).where(where).values(**values).execution_options(synchronize_session=False)
            )
            counts[model.__tablename__] = int(result.rowcount or 0)
        cases = select(OffboardingCase.id).where(OffboardingCase.candidate_id.in_(ids)).scalar_subquery()
        result = await db.execute(
            update(OffboardingFeedback)
            .where(OffboardingFeedback.case_id.in_(cases))
            .values(answers={})
            .execution_options(synchronize_session=False)
        )
        counts["offboarding_feedback"] = int(result.rowcount or 0)
        await db.flush()
        return counts

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        ext = list(
            (await db.execute(select(Extension).where(Extension.candidate_id == ctx.candidate_id))).scalars()
        )
        cases = list(
            (
                await db.execute(
                    select(OffboardingCase).where(OffboardingCase.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not ext and not cases:
            return None
        return {
            "extensions": [{"new_end_date": e.new_end_date, "status": e.status, "note": e.note} for e in ext],
            "offboarding_cases": [
                {"reason": c.reason, "last_working_day": c.last_working_day, "status": c.status}
                for c in cases
            ],
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        findings = []
        for model in _TEXT:
            where = model.candidate_id.in_(ids) & _residual(model)
            retained = ctx.retained.get(model.__tablename__, set())
            if retained:
                where = where & model.id.not_in(retained)
            if (await db.execute(select(model.id).where(where).limit(1))).first():
                findings.append(f"{model.__tablename__} free text remains")
        return findings


register_erasure_handler(PlacementExitHandler())
