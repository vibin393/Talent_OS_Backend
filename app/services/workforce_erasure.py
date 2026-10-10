# ruff: noqa: E501
"""Epic 44 erasure handler ``workforce.client_refs`` (FR-68; Epic 35 DSR subject ``client_user``).

Workforce plans hold no candidate personal data. When a Client User is erased, the reference to them on the
forecasts they submitted (``workforce_plans.submitted_by_client_user_id``) is cleared; the plans themselves are
business records and stay. Other subject kinds: no-op.
"""

from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.workforce import WorkforcePlan
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class WorkforceClientRefsHandler:
    name = "workforce.client_refs"
    order = 58
    tables: tuple[str, ...] = ("workforce_plans",)

    async def _rows(self, db: AsyncSession, ctx: ErasureContext) -> list[WorkforcePlan]:
        return list(
            (
                await db.execute(
                    select(WorkforcePlan).where(WorkforcePlan.submitted_by_client_user_id == ctx.subject_id)
                )
            ).scalars()
        )

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "client_user" or ctx.subject_id is None:
            return {}
        rows = await self._rows(db, ctx)
        for plan in rows:
            plan.submitted_by_client_user_id = None
        await db.flush()
        return {"workforce_plans": len(rows)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "client_user" or ctx.subject_id is None:
            return None
        return {
            "workforce_forecasts": [
                {"id": str(p.id), "name": p.name, "created_at": p.created_at}
                for p in await self._rows(db, ctx)
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "client_user" or ctx.subject_id is None:
            return []
        return ["workforce forecast still names the client user"] if await self._rows(db, ctx) else []


register_erasure_handler(WorkforceClientRefsHandler())
