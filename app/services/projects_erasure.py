# ruff: file-ignore[line-too-long]
"""Projects erasure handler ``projects.assignments`` (59), Epic 45 A6 (AD-34).

``project_assignments`` hold no personal data beyond the denormalised ``candidate_id``: the handler nulls it and
keeps the assignment (dates, allocation, placement link) for finance history. The Placement itself is handled by
the placements erasure.
"""

from typing import Any

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.projects import ProjectAssignment
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler


class ProjectsHandler:
    name = "projects.assignments"
    order = 59
    tables = ("project_assignments",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        ids = await subject_ids(db, ctx.candidate_id)
        result = await db.execute(
            update(ProjectAssignment)
            .where(ProjectAssignment.candidate_id.in_(ids))
            .values(candidate_id=None)
            .execution_options(synchronize_session=False)
        )
        await db.flush()
        return {"assignments": int(result.rowcount or 0)}  # type: ignore[attr-defined]

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        rows = list(
            (
                await db.execute(
                    select(ProjectAssignment).where(ProjectAssignment.candidate_id == ctx.candidate_id)
                )
            ).scalars()
        )
        if not rows:
            return None
        return {
            "project_assignments": [
                {
                    "project_id": str(a.project_id),
                    "allocation_pct": str(a.allocation_pct),
                    "start_date": a.start_date,
                    "end_date": a.end_date,
                    "status": a.status,
                }
                for a in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        ids = await subject_ids(db, ctx.candidate_id)
        left = (
            await db.execute(
                select(ProjectAssignment.id).where(ProjectAssignment.candidate_id.in_(ids)).limit(1)
            )
        ).first()
        return ["project assignment still names the candidate"] if left else []


register_erasure_handler(ProjectsHandler())
