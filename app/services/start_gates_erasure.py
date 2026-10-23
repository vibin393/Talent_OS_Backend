"""Start-gate erasure handler ``placements.start_gates`` (57), Epic 35 coverage gap for Epic 19 (AD-34).

``start_gates`` keep kind / status / source ids of a placement's pre-start checks; ``detail`` may echo free
text from the source (BGV notes, document names). For an erased candidate, ``detail`` is cleared on every gate
of the candidate's placements, except gates in ``ctx.retained``. Status and ids stay (financial traceability of
the placement).
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.placements import Placement, StartGate
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


async def _where(db: AsyncSession, ctx: ErasureContext) -> Any:
    ids = await subject_ids(db, ctx.candidate_id)
    placements = select(Placement.id).where(Placement.candidate_id.in_(ids)).scalar_subquery()
    where = StartGate.placement_id.in_(placements)
    retained = ctx.retained.get("start_gates", set())
    if retained:
        where = where & StartGate.id.not_in(retained)  # type: ignore[assignment]
    return where


class StartGatesHandler:
    name = "placements.start_gates"
    order = 57
    tables = ("start_gates",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        where = await _where(db, ctx)
        result = await db.execute(
            update(StartGate)
            .where(where, StartGate.detail != {})
            .values(detail={})
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        return {"gates_cleared": int(result.rowcount or 0)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        where = await _where(db, ctx)
        rows = list((await db.execute(select(StartGate).where(where))).scalars())
        if not rows:
            return None
        return {"start_gates": [{"kind": g.kind, "status": g.status, "required": g.required} for g in rows]}

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        where = await _where(db, ctx)
        left = (await db.execute(select(StartGate.id).where(where, StartGate.detail != {}).limit(1))).first()
        return ["start gate detail remains"] if left else []


register_erasure_handler(StartGatesHandler())
