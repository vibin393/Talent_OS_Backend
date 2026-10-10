"""Story 22.9 (A10) erasure handler ``tasks.texts`` (58; FR-68 / AD-34).

Tasks about the candidate (``entity_type='candidate'``): title -> ``[erased]`` and notes cleared. The task row
(owner, status, dates) stays so team workload history is intact. Rows in ``ctx.retained["tasks"]`` are kept.
Only candidate subjects are handled.
"""

import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.tasks import Task
from app.services.candidates_erasure import subject_ids
from app.services.privacy.registry import ErasureContext, register_erasure_handler

ERASED = "[erased]"


def _about(ids: list[uuid.UUID]) -> Any:
    return select(Task).where(Task.entity_type == "candidate", Task.entity_id.in_(ids))


class TaskTextsHandler:
    name = "tasks.texts"
    order = 58
    tables = ("tasks",)

    async def erase(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, int]:
        if ctx.subject_kind != "candidate":
            return {"tasks": 0}
        keep = ctx.retained.get("tasks", set())
        ids = await subject_ids(db, ctx.candidate_id)
        rows = [t for t in (await db.execute(_about(ids))).scalars() if t.id not in keep]
        for t in rows:
            t.title, t.notes = ERASED, None
        await db.flush()
        return {"tasks": len(rows)}

    async def export(self, db: AsyncSession, ctx: ErasureContext) -> dict[str, Any] | None:
        if ctx.subject_kind != "candidate":
            return None
        rows = (await db.execute(_about([ctx.candidate_id]))).scalars()
        return {
            "tasks": [
                {"title": t.title, "notes": t.notes, "status": t.status, "due_at": t.due_at} for t in rows
            ]
        }

    async def verify(self, db: AsyncSession, ctx: ErasureContext) -> list[str]:
        if ctx.subject_kind != "candidate":
            return []
        keep = ctx.retained.get("tasks", set())
        ids = await subject_ids(db, ctx.candidate_id)
        return [
            f"task {t.id} keeps a title or notes about the candidate"
            for t in (await db.execute(_about(ids))).scalars()
            if t.id not in keep and (t.title != ERASED or t.notes is not None)
        ]


register_erasure_handler(TaskTextsHandler())
